"""Step 8.2: the Chief of Staff routes orders and writes the morning brief (ADR 027).

Committed, like test_runners: charters are applied, orders are real tasks, and
runs are advanced as the scheduler would. Jev and the model are scripted.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from app.agents.runs import Runtime, advance_run
from app.approvals import decide, remember
from app.brain import Brain, HashingEmbedder
from app.brain.write_gate import BrainWriter
from app.db import acting_as, as_service_role, connect
from app.departments.apply import apply_charter, enable
from app.departments.charter import seed_charters
from app.departments.starter_charters import STARTER_CHARTERS
from app.gateway import Gateway, ModelResponse
from app.judge import Judge
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.owner_api import give_order
from app.tools import seed_tools
from app.tracing import NullTracer
from tests.scripted_jev import ScriptedJev, choice, noul, score
from tests.test_judge import MODEL
from tests.test_runners import TIERS, status, tick


@dataclass
class Writer:
    """The one model call the brief makes."""

    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(
            model=model,
            text="Brief: one approval waits for you.",
            tokens_in=300,
            tokens_out=20,
            cost_usd=0.0002,
            latency_ms=1,
            provider="scripted",
        )


def routing(
    department: str, p: float, complexity: float = 1.0, unless: str | None = None
) -> Callable[[Any, dict[str, Any]], dict[str, Any]]:
    def respond(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if "department" not in questions:
            return {}
        options = list(questions["department"].criteria)
        picked, prob = (department, p)
        if unless and unless in state["order"]:
            picked, prob = ("research", 0.95)
        return {"department": choice(picked, options, prob), "complexity": score(complexity)}

    return respond


@dataclass(frozen=True)
class Office:
    org_id: uuid.UUID
    user_id: uuid.UUID


@pytest.fixture
def office(dsn: str) -> Iterator[Office]:
    try:
        connection = connect(dsn)
    except psycopg.OperationalError as error:
        pytest.skip(f"No database at {dsn}: {error}")
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into auth.users (id, email) values (%s, %s)", (str(user_id), f"{user_id}@x.com")
        )
        cursor.execute("insert into public.orgs (id, name) values (%s, 'office')", (str(org_id),))
        cursor.execute(
            "insert into public.org_members (org_id, user_id) values (%s, %s)",
            (str(org_id), str(user_id)),
        )
        cursor.execute(
            "insert into public.model_prices (org_id, provider, model, "
            "input_usd_per_mtok) values (%s, 'typesafe', %s, 0.042)",
            (str(org_id), MODEL),
        )
    seed_gates(connection, user_id=user_id, org_id=org_id, gates=STARTER_GATES)
    seed_tools(connection, user_id=user_id, org_id=org_id)
    seed_charters(connection, user_id=user_id, org_id=org_id, charters=STARTER_CHARTERS)
    for department in ("research", "executive"):
        apply_charter(connection, user_id=user_id, org_id=org_id, department=department)
        enable(connection, user_id=user_id, org_id=org_id, department=department)
    try:
        yield Office(org_id, user_id)
    finally:
        with as_service_role(connection) as conn:
            conn.execute("delete from public.orgs where id = %s", (str(org_id),))
            conn.execute("delete from auth.users where id = %s", (str(user_id),))
        connection.close()


def runtime(dsn: str, jev: ScriptedJev, model: Writer | None = None) -> Runtime:
    return Runtime(
        dsn=dsn,
        transport=model or Writer(),
        tiers=TIERS,
        embedder=HashingEmbedder(),
        tracer=NullTracer(),
        systemone=jev,
    )


def order_now(dsn: str, office: Office, text: str) -> uuid.UUID:
    with connect(dsn) as connection:
        return give_order(connection, str(office.user_id), str(office.org_id), text).id


def run_next(dsn: str, rt: Runtime) -> Any:
    (run_id,) = tick(dsn)
    return advance_run(rt, run_id, deadline_seconds=60)


def subtasks(dsn: str, task_id: uuid.UUID) -> list[dict[str, Any]]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(
            "select t.id, t.status, t.input, a.name as agent from public.tasks t "
            "join public.agents a on a.id = t.assigned_agent_id where t.parent_task_id = %s",
            (str(task_id),),
        ).fetchall()


def approval(dsn: str, task_id: uuid.UUID) -> dict[str, Any]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(
            "select * from public.approvals where task_id = %s order by created_at desc",
            (str(task_id),),
        ).fetchone()


def finish_subtask(dsn: str, sub_id: uuid.UUID, summary: str) -> None:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        conn.execute(
            "update public.tasks set status = 'done', finished_at = now(), "
            "result = %s where id = %s",
            (json.dumps({"summary": summary}), str(sub_id)),
        )


def test_a_clear_order_goes_to_the_right_department_and_comes_back_done(
    dsn: str, office: Office
) -> None:
    rt = runtime(dsn, ScriptedJev(respond=routing("research", 0.9, complexity=1.2)))
    task = order_now(dsn, office, "Find three space-tech funds that raised money this month")

    routed = run_next(dsn, rt)

    assert (routed.status, routed.stop_reason) == ("succeeded", "routed"), routed.error
    assert status(dsn, task) == "blocked"
    (sub,) = subtasks(dsn, task)
    assert sub["agent"] == "research-lead"
    assert sub["input"]["suggested_tier"] == "standard"

    finish_subtask(dsn, sub["id"], "Found three funds.")
    done = run_next(dsn, rt)

    assert done.status == "succeeded" and status(dsn, task) == "done"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        result = conn.execute(
            "select result from public.tasks where id = %s", (str(task),)
        ).fetchone()["result"]
    assert result["summary"] == "research-lead: Found three funds."


def test_an_unclear_order_comes_back_as_a_question_and_the_owner_picks(
    dsn: str, office: Office
) -> None:
    rt = runtime(dsn, ScriptedJev(respond=routing("research", 0.45)))
    task = order_now(dsn, office, "Look into something vague")

    asked = run_next(dsn, rt)

    assert (asked.status, asked.stop_reason) == ("paused", "awaiting_approval")
    assert status(dsn, task) == "awaiting_approval"
    card = approval(dsn, task)
    assert card["action_type"] == "route_order" and card["payload"]["recommended"] == "research"
    assert card["payload"]["options"][0] == {"department": "research", "probability": 0.45}

    with connect(dsn) as connection:
        decide(
            connection,
            user_id=office.user_id,
            approval_id=card["id"],
            decision="approve",
            edited_arguments={"department": "research"},
        )
    with connect(dsn) as connection, as_service_role(connection) as conn:
        (woken,) = [r["id"] for r in conn.execute("select public.pending_wakeups() as id")]
    resumed = advance_run(rt, woken, deadline_seconds=60)

    assert (resumed.status, resumed.stop_reason) == ("succeeded", "routed")
    assert [s["agent"] for s in subtasks(dsn, task)] == ["research-lead"]


def test_an_order_against_an_earlier_owner_decision_is_questioned(dsn: str, office: Office) -> None:
    jev = ScriptedJev(respond=routing("research", 0.95), nouls={"conflicts": 0.9})
    with connect(dsn) as connection:
        with as_service_role(connection) as conn:
            chief = conn.execute(
                "select id from public.agents where org_id = %s and name = 'chief-of-staff'",
                (str(office.org_id),),
            ).fetchone()
        with acting_as(connection, user_id=str(office.user_id)) as conn:
            gateway = Gateway(conn, Writer(), TIERS, systemone=jev)
            remember(
                BrainWriter(conn, Brain(conn, HashingEmbedder()), Judge(conn, gateway)),
                org_id=office.org_id,
                agent_id=chief["id"],
                statement="The owner does not want research on crypto tokens.",
                ref="policy:crypto",
            )
    task = order_now(dsn, office, "Research crypto tokens for the brief")

    asked = run_next(dsn, runtime(dsn, jev))

    assert asked.stop_reason == "awaiting_approval", "a clear routing, but it conflicts"
    card = approval(dsn, task)
    assert [c["claim"] for c in card["conflicts"]] == [
        "The owner does not want research on crypto tokens."
    ]
    assert "goes against" in card["explanation"]


def test_a_redirect_is_routed_again_with_the_owners_note(dsn: str, office: Office) -> None:
    rt = runtime(dsn, ScriptedJev(respond=routing("owner", 0.8, unless="for research")))
    task = order_now(dsn, office, "Handle the thing we talked about")
    run_next(dsn, rt)
    card = approval(dsn, task)

    with connect(dsn) as connection:
        decide(
            connection,
            user_id=office.user_id,
            approval_id=card["id"],
            decision="redirect",
            note="This one is for research: space funds.",
        )
    with connect(dsn) as connection, as_service_role(connection) as conn:
        (woken,) = [r["id"] for r in conn.execute("select public.pending_wakeups() as id")]
    resumed = advance_run(rt, woken, deadline_seconds=60)

    assert (resumed.status, resumed.stop_reason) == ("succeeded", "routed")
    assert [s["agent"] for s in subtasks(dsn, task)] == ["research-lead"]


def test_the_brief_leads_with_what_needs_the_owner(dsn: str, office: Office) -> None:
    # Something waiting for the owner, and something that just finished.
    ask_rt = runtime(dsn, ScriptedJev(respond=routing("research", 0.3)))
    order_now(dsn, office, "An unclear order")
    run_next(dsn, ask_rt)

    def ranker(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if "needs_owner" not in questions:
            return {}
        waiting = state["item"]["kind"] == "approval"
        return {
            "needs_owner": noul(0.95 if waiting else 0.05),
            "urgency": score(2.0 if waiting else 0.2),
            "impact": score(1.0),
        }

    writer = Writer()
    rt = runtime(dsn, ScriptedJev(respond=ranker), writer)
    with connect(dsn) as connection:
        from app.tasks import order

        brief = order(
            connection,
            user_id=office.user_id,
            org_id=office.org_id,
            agent="brief-writer",
            title="Morning brief",
        )
    result = run_next(dsn, rt)

    assert result.status == "succeeded", result.error
    with connect(dsn) as connection, as_service_role(connection) as conn:
        output = conn.execute(
            "select result from public.tasks where id = %s", (str(brief.id),)
        ).fetchone()["result"]
    assert output["summary"] == "Brief: one approval waits for you."
    assert output["lead"][0]["kind"] == "approval"
    sent = json.loads(writer.calls[0][1]["content"])
    assert sent["lead"][0]["title"] == "Waiting for you: route_order"
    assert writer.calls[0][0]["content"].startswith("You write the owner's morning brief")
