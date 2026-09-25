"""Step 7.5: a held action pauses its run, and the owner's decision resumes it.

Acceptance: an R4 action is held and approve or reject resumes or cancels it,
with the full trail in `events`. Like test_runners this commits, and each run
is advanced exactly as the scheduler's pokes would: a deepagents loop over the
gateway, the tool runtime, the checkpointer and the database's own triggers.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from pydantic import BaseModel

from app.agents.admin import AgentSpec, create_agent
from app.agents.runs import Runtime, advance_run
from app.agents.starter_prompts import STARTER_PROMPTS
from app.approvals import decide
from app.brain.embeddings import HashingEmbedder
from app.db import as_service_role, connect
from app.gateway import ModelResponse, ToolCall
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.tasks import order
from app.tools import REGISTRY, ToolSpec, register, seed_tools
from app.tracing import NullTracer
from tests.scripted_jev import ScriptedJev
from tests.test_judge import MODEL
from tests.test_runners import TIERS, status, tick


class PostArgs(BaseModel):
    text: str


@dataclass
class Publisher:
    """A worker that publishes a post, then reports what happened."""

    calls: list[str] = field(default_factory=list)
    explained: int = 0

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        if "waiting for the owner's approval" in (messages[0]["content"] or ""):
            self.explained += 1  # the decision desk's note, from the `explain` prompt
            return self._say("It publishes the launch post.", record=False)
        done = [m for m in messages if m["role"] == "tool"]
        if not done:
            return self._tool("publish_test", {"text": "Launch post"})
        if len(done) == 1:
            return self._tool("report_result", {"summary": f"Publishing: {done[0]['content']}"})
        return self._say("Done.")

    def _tool(self, name: str, args: dict[str, Any]) -> ModelResponse:
        self.calls.append(name)
        return ModelResponse(
            model="vendor/small",
            text="",
            tokens_in=100,
            tokens_out=20,
            cost_usd=0.001,
            latency_ms=1,
            provider="scripted",
            tool_calls=(ToolCall(f"call_{len(self.calls)}", name, json.dumps(args)),),
            finish_reason="tool_calls",
        )

    def _say(self, text: str, *, record: bool = True) -> ModelResponse:
        if record:
            self.calls.append("say")
        return ModelResponse(
            model="vendor/small",
            text=text,
            tokens_in=100,
            tokens_out=20,
            cost_usd=0.001,
            latency_ms=1,
            provider="scripted",
            finish_reason="stop",
        )


@dataclass(frozen=True)
class Shop:
    org_id: uuid.UUID
    user_id: uuid.UUID
    agent_id: uuid.UUID
    published: list[str]


@pytest.fixture
def shop(dsn: str) -> Iterator[Shop]:
    try:
        connection = connect(dsn)
    except psycopg.OperationalError as error:
        pytest.skip(f"No database at {dsn}: {error}")
    published: list[str] = []
    register(
        ToolSpec(
            name="publish_test",
            description="Publish a post on the website.",
            args=PostArgs,
            risk_class="R4",
            side_effect=True,
            approval="approval",
            handler=lambda ctx, a: published.append(a.text) or {"published": a.text},
        )
    )
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into auth.users (id, email) values (%s, %s)", (str(user_id), f"{user_id}@x.com")
        )
        cursor.execute("insert into public.orgs (id, name) values (%s, 'shop')", (str(org_id),))
        cursor.execute(
            "insert into public.org_members (org_id, user_id) values (%s, %s)",
            (str(org_id), str(user_id)),
        )
        cursor.execute(
            "insert into public.departments (org_id, name, daily_budget_usd) "
            "values (%s, 'content', 1)",
            (str(org_id),),
        )
        cursor.execute(
            "insert into public.model_prices (org_id, provider, model, input_usd_per_mtok) "
            "values (%s, 'typesafe', %s, 0.042)",
            (str(org_id), MODEL),
        )
    seed_tools(connection, user_id=user_id, org_id=org_id)
    seed_gates(connection, user_id=user_id, org_id=org_id, gates=STARTER_GATES)
    agent = create_agent(
        connection,
        user_id=user_id,
        org_id=org_id,
        spec=AgentSpec(
            name="writer",
            department="content",
            role="content",
            runner="deep",
            prompts=STARTER_PROMPTS["worker"],
            allowed_tools=["publish_test", "report_result"],
        ),
    )
    with as_service_role(connection) as conn:
        conn.execute("update public.agents set enabled = true where id = %s", (str(agent.id),))
    try:
        yield Shop(org_id, user_id, agent.id, published)
    finally:
        REGISTRY.pop("publish_test")
        with as_service_role(connection) as conn:
            conn.execute("delete from public.orgs where id = %s", (str(org_id),))
            conn.execute("delete from auth.users where id = %s", (str(user_id),))
        connection.close()


def runtime(dsn: str, model: Publisher) -> Runtime:
    return Runtime(
        dsn=dsn,
        transport=model,
        tiers=TIERS,
        embedder=HashingEmbedder(),
        tracer=NullTracer(),
        systemone=ScriptedJev(choices={"recommendation": "approve"}),
    )


def wakeups(dsn: str) -> list[uuid.UUID]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return [r["id"] for r in conn.execute("select public.pending_wakeups() as id").fetchall()]


def held_until_decided(dsn: str, shop: Shop, model: Publisher) -> tuple[Any, uuid.UUID, str]:
    """Order the post; the run holds at publish_test and pauses."""
    with connect(dsn) as connection:
        task = order(
            connection,
            user_id=shop.user_id,
            org_id=shop.org_id,
            agent="writer",
            title="Publish the launch post",
        )
    (run_id,) = tick(dsn)
    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)
    assert (result.status, result.stop_reason) == ("paused", "awaiting_approval"), result.error
    assert status(dsn, task.id) == "awaiting_approval"
    assert shop.published == []
    assert wakeups(dsn) == [], "nothing wakes it while the owner has not decided"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        approval = conn.execute(
            "select id, recommendation, task_id, run_id, explanation from public.approvals "
            "where org_id = %s",
            (str(shop.org_id),),
        ).fetchone()
    assert approval["task_id"] == task.id and approval["run_id"] == run_id
    assert approval["recommendation"] == "approve"
    assert model.explained == 1 and approval["explanation"] == "It publishes the launch post."
    return task, run_id, str(approval["id"])


def trail(dsn: str, org_id: uuid.UUID) -> list[str]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        rows = conn.execute(
            "select type, payload from public.events where org_id = %s and type = any(%s) "
            "order by created_at",
            (
                str(org_id),
                [
                    "tool_called",
                    "approval_requested",
                    "approval_decided",
                    "run_paused",
                    "task_awaiting_approval",
                    "task_running",
                    "task_done",
                    "task_cancelled",
                ],
            ),
        ).fetchall()
    return [
        r["type"] + (f":{r['payload']['tool']}" if r["type"] == "tool_called" else "") for r in rows
    ]


def test_approving_resumes_the_run_and_the_action_runs_once_as_edited(dsn: str, shop: Shop) -> None:
    model = Publisher()
    task, run_id, approval_id = held_until_decided(dsn, shop, model)

    with connect(dsn) as connection:
        decide(
            connection,
            user_id=shop.user_id,
            approval_id=approval_id,
            decision="approve",
            edited_arguments={"text": "Launch post, edited"},
        )
    assert status(dsn, task.id) == "running"
    assert wakeups(dsn) == [run_id]

    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert result.status == "succeeded", result.error
    assert status(dsn, task.id) == "done"
    assert shop.published == ["Launch post, edited"], "ran once, with the owner's edit"
    # The model was not asked again for the held call: the run resumed at it.
    assert model.calls == ["publish_test", "report_result", "say"]
    with connect(dsn) as connection, as_service_role(connection) as conn:
        result_row = conn.execute(
            "select result from public.tasks where id = %s", (str(task.id),)
        ).fetchone()
    assert "Launch post, edited" in result_row["result"]["summary"]
    events = trail(dsn, shop.org_id)
    # The hold is one transaction (one timestamp), so its three events come
    # in any order among themselves; the decision and what follows come after.
    assert events[0] == "task_running"
    assert sorted(events[1:4]) == [
        "approval_requested",
        "task_awaiting_approval",
        "tool_called:publish_test",
    ]
    assert events[4] == "run_paused"
    assert sorted(events[5:7]) == ["approval_decided", "task_running"]  # one transaction
    assert events[7:] == [
        "tool_called:publish_test",
        "tool_called:report_result",
        "task_done",
    ]


def test_rejecting_with_cancel_stops_the_task_and_the_run(dsn: str, shop: Shop) -> None:
    model = Publisher()
    task, run_id, approval_id = held_until_decided(dsn, shop, model)

    with connect(dsn) as connection:
        decide(
            connection,
            user_id=shop.user_id,
            approval_id=approval_id,
            decision="cancel",
            note="Not this week.",
        )

    assert status(dsn, task.id) == "cancelled"
    assert wakeups(dsn) == []
    with connect(dsn) as connection, as_service_role(connection) as conn:
        run = conn.execute(
            "select status, stop_reason from public.runs where id = %s", (str(run_id),)
        ).fetchone()
    assert (run["status"], run["stop_reason"]) == ("cancelled", "rejected")
    assert shop.published == []


def test_rejecting_with_a_redirect_lets_the_agent_carry_on_without_it(dsn: str, shop: Shop) -> None:
    model = Publisher()
    task, run_id, approval_id = held_until_decided(dsn, shop, model)

    with connect(dsn) as connection:
        decide(
            connection,
            user_id=shop.user_id,
            approval_id=approval_id,
            decision="redirect",
            note="Keep it as a draft; I will publish it myself.",
        )
    assert wakeups(dsn) == [run_id]
    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert result.status == "succeeded", result.error
    assert status(dsn, task.id) == "done"
    assert shop.published == []
    with connect(dsn) as connection, as_service_role(connection) as conn:
        summary = conn.execute(
            "select result from public.tasks where id = %s", (str(task.id),)
        ).fetchone()["result"]["summary"]
    assert "I will publish it myself" in summary, "the agent read the owner's note"


def test_a_run_still_pausing_when_the_owner_decides_is_woken(dsn: str, shop: Shop) -> None:
    model = Publisher()
    task, run_id, approval_id = held_until_decided(dsn, shop, model)
    # As if the owner decided before the run had recorded why it paused.
    with connect(dsn) as connection:
        with as_service_role(connection) as conn:
            conn.execute("update public.approvals set run_id = null where id = %s", (approval_id,))
        decide(connection, user_id=shop.user_id, approval_id=approval_id, decision="approve")
        with as_service_role(connection) as conn:
            conn.execute(
                "update public.approvals set run_id = %s where id = %s", (str(run_id), approval_id)
            )
            stop = conn.execute(
                "select stop_reason from public.runs where id = %s", (str(run_id),)
            ).fetchone()["stop_reason"]
    assert stop == "awaiting_approval", "the decision could not reach the run"

    assert wakeups(dsn) == [run_id]
    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)
    assert result.status == "succeeded" and shop.published == ["Launch post"]
    assert status(dsn, task.id) == "done"
