"""Step 7.7: an agent uses an MCP tool in a real run (ADR 025).

Committed, like test_runners: a deep-runner worker is given an approved MCP
tool, the scripted model calls it by the server's own JSON Schema, and the
result reaches the task. The server is the in-memory one from test_mcp.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from app.agents.admin import AgentSpec, create_agent
from app.agents.runs import Runtime, advance_run
from app.agents.starter_prompts import STARTER_PROMPTS
from app.brain.embeddings import HashingEmbedder
from app.db import as_service_role, connect
from app.gateway import ModelResponse, ToolCall
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.mcp_servers.client import McpGateway
from app.mcp_servers.servers import add_server, approve_tool, refresh
from app.tasks import order
from app.tools import seed_tools
from app.tracing import NullTracer
from tests.test_judge import MODEL
from tests.test_mcp import Memory, jev, studio
from tests.test_runners import TIERS, status, tick


@dataclass
class Designer:
    calls: list[str] = field(default_factory=list)
    offered: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        done = [m for m in messages if m["role"] == "tool"]
        self.offered = kw.get("tools") or self.offered
        if not done:
            return self._tool("mcp_studio_search_styles", {"query": "marble"})
        if len(done) == 1:
            found = json.loads(done[0]["content"])["text"]
            return self._tool("report_result", {"summary": f"Styles: {found}"})
        return ModelResponse(
            model="m",
            text="Done.",
            tokens_in=10,
            tokens_out=2,
            cost_usd=0.0001,
            latency_ms=1,
            provider="scripted",
            finish_reason="stop",
        )

    def _tool(self, name: str, args: dict[str, Any]) -> ModelResponse:
        self.calls.append(name)
        return ModelResponse(
            model="m",
            text="",
            tokens_in=10,
            tokens_out=2,
            cost_usd=0.0001,
            latency_ms=1,
            provider="scripted",
            finish_reason="tool_calls",
            tool_calls=(ToolCall(f"c{len(self.calls)}", name, json.dumps(args)),),
        )


@pytest.fixture
def shop(dsn: str) -> Iterator[tuple[uuid.UUID, uuid.UUID, Memory]]:
    try:
        connection = connect(dsn)
    except psycopg.OperationalError as error:
        pytest.skip(f"No database at {dsn}: {error}")
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into auth.users (id, email) values (%s, %s)", (str(user_id), f"{user_id}@x.com")
        )
        cursor.execute("insert into public.orgs (id, name) values (%s, 'mcp')", (str(org_id),))
        cursor.execute(
            "insert into public.org_members (org_id, user_id) values (%s, %s)",
            (str(org_id), str(user_id)),
        )
        cursor.execute(
            "insert into public.departments (org_id, name, daily_budget_usd) "
            "values (%s, 'design', 1)",
            (str(org_id),),
        )
        cursor.execute(
            "insert into public.model_prices (org_id, provider, model, "
            "input_usd_per_mtok) values (%s, 'typesafe', %s, 0.042)",
            (str(org_id), MODEL),
        )
    seed_gates(connection, user_id=user_id, org_id=org_id, gates=STARTER_GATES)
    seed_tools(connection, user_id=user_id, org_id=org_id)
    memory = Memory(studio())
    add_server(
        connection,
        user_id=user_id,
        org_id=org_id,
        name="studio",
        url="https://studio.example/mcp",
        auth="none",
    )
    refresh(connection, user_id=user_id, name="studio", gateway=McpGateway(memory))
    approve_tool(connection, user_id=user_id, name="mcp_studio_search_styles")
    agent = create_agent(
        connection,
        user_id=user_id,
        org_id=org_id,
        spec=AgentSpec(
            name="designer",
            department="design",
            role="design",
            runner="deep",
            prompts=STARTER_PROMPTS["worker"],
            allowed_tools=["mcp_studio_search_styles", "report_result"],
        ),
    )
    with as_service_role(connection) as conn:
        conn.execute("update public.agents set enabled = true where id = %s", (str(agent.id),))
    try:
        yield org_id, user_id, memory
    finally:
        with as_service_role(connection) as conn:
            conn.execute("delete from public.orgs where id = %s", (str(org_id),))
            conn.execute("delete from auth.users where id = %s", (str(user_id),))
        connection.close()


def test_an_agent_calls_an_approved_mcp_tool_in_a_real_run(
    dsn: str, shop: tuple[uuid.UUID, uuid.UUID, Memory]
) -> None:
    org_id, user_id, memory = shop
    model = Designer()
    rt = Runtime(
        dsn=dsn,
        transport=model,
        tiers=TIERS,
        embedder=HashingEmbedder(),
        tracer=NullTracer(),
        systemone=jev(),
        services={"mcp": McpGateway(memory)},
    )
    with connect(dsn) as connection:
        task = order(
            connection, user_id=user_id, org_id=org_id, agent="designer", title="Find marble styles"
        )
    (run_id,) = tick(dsn)

    result = advance_run(rt, run_id, deadline_seconds=60)

    assert result.status == "succeeded", result.error
    assert status(dsn, task.id) == "done"
    offered = {t["function"]["name"]: t["function"] for t in model.offered}
    assert offered["mcp_studio_search_styles"]["parameters"]["required"] == ["query"]
    with connect(dsn) as connection, as_service_role(connection) as conn:
        summary = conn.execute(
            "select result from public.tasks where id = %s", (str(task.id),)
        ).fetchone()["result"]["summary"]
    assert summary == "Styles: 3 styles match marble: noir, marble, dusk."
