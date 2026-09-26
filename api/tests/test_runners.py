"""Step 7.4: a head and two workers complete a real task tree (ADR 020).

Acceptance: one head and two workers complete a task tree with the cost of
every level visible on the task, and a worker's temporary helper does not
appear as a persisted agent.

Like test_runs, this commits: each run is advanced by `advance_run` on its
own connections, exactly as the scheduler's pokes would. The model is
scripted (it plays the head, the workers and the helper by reading its
prompt); everything else is real: the deepagents loop, the gateway, the tool
runtime, the tasks table and its triggers, the checkpointer.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from app.agents.admin import AgentSpec, create_agent
from app.agents.runs import Runtime, advance_run
from app.agents.starter_prompts import STARTER_PROMPTS
from app.brain.embeddings import HashingEmbedder
from app.db import as_service_role, connect
from app.gateway import ModelResponse, TierMap, ToolCall
from app.tasks import order, tree
from app.tools import seed_tools
from app.tracing import NullTracer

TIERS = TierMap(
    models={"cheap": "vendor/small", "standard": "vendor/mid", "frontier": "vendor/big"}
)


@dataclass
class ScriptedTeam:
    """Plays every role by reading the conversation, as a model would."""

    cost_usd: float = 0.001
    calls: list[str] = field(default_factory=list)
    counter: int = 0

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        system = " ".join(m["content"] or "" for m in messages if m["role"] == "system")
        user = next(m["content"] for m in messages if m["role"] == "user")
        done = [m for m in messages if m["role"] == "tool"]
        tools = {t["function"]["name"] for t in kw.get("tools") or []}

        if user.startswith("HELPER:"):
            return self._say("helper", "The notes say the company was founded in 2019.")
        if "You lead a small team" in system:
            if "Results from your sub-tasks" in user:
                if not done:
                    return self._tool(
                        "head-2", "report_result", {"summary": "Acme: founded 2019, 40 people."}
                    )
                return self._say("head-2", "Reported to the owner.")
            if len(done) < 2:
                who, title = [("w1", "Find the founding date"), ("w2", "Find the headcount")][
                    len(done)
                ]
                return self._tool(
                    "head-1",
                    "create_task",
                    {"assign_to": who, "title": title, "instructions": f"{title} for Acme Corp."},
                )
            return self._say("head-1", "Delegated to two workers.")
        # Workers.
        if "founding date" in user:
            if not done:
                assert "task" in tools, "deepagents offers its temporary-helper tool"
                return self._tool(
                    "w1",
                    "task",
                    {"description": "HELPER: read the notes", "subagent_type": "general-purpose"},
                )
            if len(done) == 1:
                return self._tool("w1", "report_result", {"summary": "Founded in 2019."})
            return self._say("w1", "Done.")
        if not done:
            return self._tool("w2", "report_result", {"summary": "40 people."})
        return self._say("w2", "Done.")

    def _tool(self, who: str, name: str, args: dict[str, Any]) -> ModelResponse:
        self.counter += 1
        self.calls.append(f"{who}:{name}")
        return ModelResponse(
            model="vendor/small",
            text="",
            tokens_in=100,
            tokens_out=20,
            cost_usd=self.cost_usd,
            latency_ms=1,
            provider="scripted",
            tool_calls=(ToolCall(f"call_{self.counter}", name, json.dumps(args)),),
            finish_reason="tool_calls",
        )

    def _say(self, who: str, text: str) -> ModelResponse:
        self.calls.append(f"{who}:say")
        return ModelResponse(
            model="vendor/small",
            text=text,
            tokens_in=100,
            tokens_out=20,
            cost_usd=self.cost_usd,
            latency_ms=1,
            provider="scripted",
            finish_reason="stop",
        )


@dataclass(frozen=True)
class Team:
    org_id: uuid.UUID
    user_id: uuid.UUID
    agents: dict[str, uuid.UUID]


@pytest.fixture
def team(dsn: str) -> Iterator[Team]:
    try:
        connection = connect(dsn)
    except psycopg.OperationalError as error:
        pytest.skip(f"No database at {dsn}: {error}")
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into auth.users (id, email) values (%s, %s)", (str(user_id), f"{user_id}@x.com")
        )
        cursor.execute("insert into public.orgs (id, name) values (%s, 'team')", (str(org_id),))
        cursor.execute(
            "insert into public.org_members (org_id, user_id) values (%s, %s)",
            (str(org_id), str(user_id)),
        )
        cursor.execute(
            "insert into public.departments (org_id, name, daily_budget_usd) "
            "values (%s, 'research', 1)",
            (str(org_id),),
        )
    seed_tools(connection, user_id=user_id, org_id=org_id)
    base = {"department": "research", "role": "research", "runner": "deep"}
    specs = [
        AgentSpec(
            **base,
            name="lead",
            role_type="head",
            prompts=STARTER_PROMPTS["head"],
            allowed_tools=["create_task", "report_result"],
        ),
        AgentSpec(
            **base,
            name="w1",
            prompts=STARTER_PROMPTS["worker"],
            allowed_tools=["report_result", "brain_search"],
        ),
        AgentSpec(
            **base,
            name="w2",
            prompts=STARTER_PROMPTS["worker"],
            allowed_tools=["report_result", "brain_search"],
        ),
    ]
    agents = {
        s.name: create_agent(connection, user_id=user_id, org_id=org_id, spec=s).id for s in specs
    }
    with as_service_role(connection) as conn:
        conn.execute("update public.agents set enabled = true where org_id = %s", (str(org_id),))
    try:
        yield Team(org_id, user_id, agents)
    finally:
        with as_service_role(connection) as conn:
            conn.execute("delete from public.orgs where id = %s", (str(org_id),))
            conn.execute("delete from auth.users where id = %s", (str(user_id),))
        connection.close()


def runtime(dsn: str, model: ScriptedTeam) -> Runtime:
    return Runtime(
        dsn=dsn, transport=model, tiers=TIERS, embedder=HashingEmbedder(), tracer=NullTracer()
    )


def tick(dsn: str) -> list[uuid.UUID]:
    """What the scheduler does each minute: runs for queued tasks."""
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return [
            r["id"] for r in conn.execute("select public.dispatch_queued_tasks() as id").fetchall()
        ]


def status(dsn: str, task_id: uuid.UUID) -> str:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(
            "select status from public.tasks where id = %s", (str(task_id),)
        ).fetchone()["status"]


def test_a_head_and_two_workers_complete_a_task_tree(dsn: str, team: Team) -> None:
    model = ScriptedTeam()
    rt = runtime(dsn, model)
    with connect(dsn) as connection:
        task = order(
            connection,
            user_id=team.user_id,
            org_id=team.org_id,
            agent="lead",
            title="Brief on Acme Corp",
            instructions="Founding date and headcount.",
        )

    # Invocation 1: the head plans, delegates twice, and stops.
    (head_run,) = tick(dsn)
    result = advance_run(rt, head_run, deadline_seconds=60)
    assert (result.status, result.stop_reason) == ("succeeded", "completed"), result.error
    assert status(dsn, task.id) == "blocked"

    # Invocations 2 and 3: each worker does its task in its own run.
    worker_runs = tick(dsn)
    assert len(worker_runs) == 2
    for run_id in worker_runs:
        outcome = advance_run(rt, run_id, deadline_seconds=60)
        assert outcome.status == "succeeded", outcome.error
    assert status(dsn, task.id) == "queued", "the last worker woke the head"

    # Invocation 4: the head sees both results, reports, and finishes.
    (second,) = tick(dsn)
    final = advance_run(rt, second, deadline_seconds=60)
    assert final.status == "succeeded", final.error
    assert status(dsn, task.id) == "done"

    with connect(dsn) as connection:
        rows = {r["title"]: r for r in tree(connection, user_id=team.user_id, root_task_id=task.id)}
        with as_service_role(connection) as conn:
            result_row = conn.execute(
                "select result from public.tasks where id = %s", (str(task.id),)
            ).fetchone()
            agent_count = conn.execute(
                "select count(*) as n from public.agents where org_id = %s", (str(team.org_id),)
            ).fetchone()["n"]
            w1_calls = conn.execute(
                "select count(*) as n from public.model_calls where agent_id = %s",
                (str(team.agents["w1"]),),
            ).fetchone()["n"]

    assert result_row["result"] == {"summary": "Acme: founded 2019, 40 people."}
    assert {r["title"]: r["result"]["summary"] for r in rows.values() if r["depth"] == 1} == {
        "Find the founding date": "Founded in 2019.",
        "Find the headcount": "40 people.",
    }
    # Cost of every level is on the tree: the head's own two runs, and the whole tree.
    head = rows["Brief on Acme Corp"]
    assert head["own_cost_usd"] == Decimal("0.005")  # 3 turns planning + 2 reporting
    assert (
        head["tree_cost_usd"]
        == Decimal("0.005")
        + rows["Find the founding date"]["own_cost_usd"]
        + rows["Find the headcount"]["own_cost_usd"]
    )
    assert rows["Find the founding date"]["own_cost_usd"] == Decimal("0.004")  # incl. the helper
    # The helper ran inside w1's run and left no agent behind.
    assert "helper:say" in model.calls
    assert agent_count == 3
    assert w1_calls == 4


def test_a_digest_agent_without_its_brief_prompt_fails_clearly(dsn: str, team: Team) -> None:
    with connect(dsn) as connection:
        with as_service_role(connection) as conn:
            conn.execute(
                "update public.agents set runner = 'digest' where id = %s",
                (str(team.agents["w2"]),),
            )
        task = order(
            connection, user_id=team.user_id, org_id=team.org_id, agent="w2", title="Morning digest"
        )
    (run_id,) = tick(dsn)

    result = advance_run(runtime(dsn, ScriptedTeam()), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("failed", "prompt_missing")
    assert status(dsn, task.id) == "failed"
