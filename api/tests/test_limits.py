"""Step 7.6: the autonomy ladder and the safety limits (ADR 022).

Acceptance: each limit has a test that trips it, and a task tree stops
cleanly mid-flight when the kill switch is switched on.

The ladder, suggestions, rate limit and reaper are checked inside one
rolled-back transaction; loop detection and the kill switch drive real runs
(committed, like test_runners).
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from pydantic import BaseModel

from app.agents.autonomy import resume_paused_runs, set_level, suggestions
from app.agents.runs import advance_run
from app.db import acting_as, as_service_role, connect
from app.gateway import ModelResponse, ToolCall
from app.tasks import TaskError, order, tree
from app.tools import REGISTRY, ToolSpec, register, seed_tools
from tests.conftest_db import Tenants
from tests.scripted_jev import ScriptedJev
from tests.test_approvals import Effects, runtime
from tests.test_gateway import make_agent
from tests.test_runners import ScriptedTeam, Team, status, team, tick  # noqa: F401
from tests.test_runners import runtime as team_runtime


class TextArgs(BaseModel):
    text: str


@pytest.fixture
def tools(db: psycopg.Connection, tenants: Tenants) -> Iterator[Effects]:
    done = Effects()
    specs = [
        ToolSpec(
            name=f"{risk.lower()}_test",
            description=f"A {risk} test tool.",
            args=TextArgs,
            risk_class=risk,
            side_effect=risk in ("R1", "R3", "R4"),
            approval="approval" if risk == "R4" else "auto",
            handler=lambda ctx, a, risk=risk: (
                done.noted.append(f"{risk}:{a.text}") or {"done": a.text, "screened": "clean"}
            ),
        )
        for risk in ("R0", "R1", "R2", "R3", "R4")
    ]
    for spec in specs:
        register(spec)
    try:
        yield done
    finally:
        for spec in specs:
            REGISTRY.pop(spec.name)


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants, tools: Effects) -> UUID:
    from app.judge.starter_gates import STARTER_GATES
    from app.judge.store import seed_gates
    from tests.test_judge import set_price

    agent_id = make_agent(db, tenants.org_a, name="ladder")
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=STARTER_GATES)
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    with as_service_role(db) as conn:
        conn.execute(
            "update public.agents set allowed_tools = %s where id = %s",
            ([f"r{i}_test" for i in range(5)], str(agent_id)),
        )
    return agent_id


def outcomes(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, jev: ScriptedJev
) -> dict[str, str]:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, jev)
        return {
            risk: rt.call(f"{risk.lower()}_test", {"text": str(uuid.uuid4())}).status
            for risk in ("R0", "R1", "R2", "R3", "R4")
        }


# --- The autonomy ladder -------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected", "checked"),
    [
        ("L0", {"R0": "ok", "R1": "held", "R2": "held", "R3": "held", "R4": "held"}, 0),
        ("L1", {"R0": "ok", "R1": "ok", "R2": "ok", "R3": "held", "R4": "held"}, 1),
        ("L2", {"R0": "ok", "R1": "ok", "R2": "ok", "R3": "ok", "R4": "held"}, 2),
        ("L3", {"R0": "ok", "R1": "ok", "R2": "ok", "R3": "ok", "R4": "held"}, 1),
    ],
)
def test_each_level_runs_checks_or_holds_by_risk_class(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,
    level: str,
    expected: dict[str, str],
    checked: int,
) -> None:
    set_level(db, user_id=tenants.user_a, org_id=tenants.org_a, name="ladder", level=level)
    jev = ScriptedJev()

    assert outcomes(db, tenants, agent, jev) == expected
    assert len(jev.calls_for("irreversible")) == checked, "calls the tool-risk gate checked"


def test_the_owner_can_change_what_a_level_means_but_not_for_r4(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        conn.execute(
            "insert into public.autonomy_rules (org_id, level, risk_class, mode) "
            "values (%s, 'L1', 'R3', 'gate'), (%s, 'L1', 'R1', 'hold')",
            (str(tenants.org_a), str(tenants.org_a)),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        with acting_as(db, user_id=str(tenants.user_a)) as conn:
            conn.execute(
                "insert into public.autonomy_rules (org_id, level, risk_class, mode) "
                "values (%s, 'L3', 'R4', 'run')",
                (str(tenants.org_a),),
            )

    result = outcomes(db, tenants, agent, ScriptedJev())

    assert (result["R1"], result["R3"], result["R4"]) == ("held", "ok", "held")
    with as_service_role(db) as conn:
        audited = conn.execute(
            "select count(*) as n from public.events where type = 'autonomy_rule_changed'"
        ).fetchone()["n"]
    assert audited == 2


def test_an_agent_cannot_change_its_own_autonomy(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
            conn.execute(
                "insert into public.autonomy_rules (org_id, level, risk_class, mode) "
                "values (%s, 'L1', 'R3', 'run')",
                (str(tenants.org_a),),
            )
    attempts = [
        "update public.agents set autonomy_level = 'L3' where id = '{agent}'",
        "update public.delegation_limits set loop_repeat_limit = 10",
    ]
    for sql in attempts:
        with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
            cursor = conn.execute(sql.format(org=tenants.org_a, agent=agent))
            assert cursor.rowcount == 0, sql
    with as_service_role(db) as conn:
        level = conn.execute(
            "select autonomy_level from public.agents where id = %s", (str(agent),)
        ).fetchone()["autonomy_level"]
    assert level == "L1"


# --- Promotion suggestions (right-hand idea 2) ------------------------------------------


def decisions(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, action: str, agreed: int, total: int
) -> None:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        for i in range(total):
            cursor.execute(
                "insert into public.approvals (org_id, agent_id, action_type, action_key, "
                "recommendation, status, decided_at) "
                "values (%s, %s, 'tool_call', %s, 'approve', %s, now())",
                (str(tenants.org_a), str(agent), action, "approved" if i < agreed else "rejected"),
            )


def test_a_promotion_is_suggested_only_on_enough_agreement_and_never_made(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    decisions(db, tenants, agent, "tool:r3_test", agreed=29, total=30)  # 96.7%
    decisions(db, tenants, agent, "tool:r2_test", agreed=28, total=30)  # 93.3%
    decisions(db, tenants, agent, "tool:r1_test", agreed=29, total=29)  # too few
    decisions(db, tenants, agent, "tool:r4_test", agreed=40, total=40)  # irreversible

    eligible = suggestions(db, user_id=tenants.user_a)
    everything = {
        r["action_key"]: r["eligible"]
        for r in suggestions(db, user_id=tenants.user_a, eligible_only=False)
    }

    assert [(r["action_key"], r["current_level"], r["suggested_level"]) for r in eligible] == [
        ("tool:r3_test", "L1", "L2")
    ]
    assert everything == {
        "tool:r1_test": False,
        "tool:r2_test": False,
        "tool:r3_test": True,
        "tool:r4_test": False,
    }
    with as_service_role(db) as conn:
        level = conn.execute(
            "select autonomy_level from public.agents where id = %s", (str(agent),)
        ).fetchone()["autonomy_level"]
    assert level == "L1", "suggested, never applied"


# --- Tasks per agent per hour -------------------------------------------------------------


def test_an_agent_cannot_be_given_more_tasks_an_hour_than_the_limit(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    with as_service_role(db) as conn:
        conn.execute(
            "insert into public.delegation_limits (org_id, max_tasks_per_agent_per_hour) "
            "values (%s, 2) on conflict (org_id) do update set max_tasks_per_agent_per_hour = 2",
            (str(tenants.org_a),),
        )
    for n in range(2):
        order(db, user_id=tenants.user_a, org_id=tenants.org_a, agent="ladder", title=f"Job {n}")

    with pytest.raises(TaskError, match="in the last hour"):
        order(db, user_id=tenants.user_a, org_id=tenants.org_a, agent="ladder", title="Job 3")


# --- The stuck-task reaper ------------------------------------------------------------------


def make_task_run(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,
    *,
    title: str,
    run_status: str,
    stop_reason: str | None = None,
    wake_count: int = 5,
    lease: str | None = None,
    task_status: str = "running",
    parent: UUID | None = None,
) -> UUID:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into public.tasks (org_id, assigned_agent_id, created_by, title, "
            "idempotency_key, requested_by, parent_task_id) "
            "values (%s, %s, 'owner', %s, %s, %s, %s) returning id",
            (
                str(tenants.org_a),
                str(agent),
                title,
                title,
                str(tenants.user_a),
                str(parent) if parent else None,
            ),
        )
        task_id = cursor.fetchone()["id"]
        cursor.execute(
            "insert into public.runs (org_id, agent_id, trigger, input, requested_by, task_id, "
            "status, stop_reason, wake_count, lease_expires_at, idempotency_key) "
            f"values (%s, %s, 'task', '{{}}', %s, %s, %s, %s, %s, {lease or 'null'}, %s)",
            (
                str(tenants.org_a),
                str(agent),
                str(tenants.user_a),
                str(task_id),
                run_status,
                stop_reason,
                wake_count,
                f"task:{task_id}:1",
            ),
        )
        cursor.execute(
            "update public.tasks set status = %s where id = %s", (task_status, str(task_id))
        )
    return task_id


def test_the_reaper_fails_only_tasks_that_can_no_longer_progress(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    head = make_task_run(
        db, tenants, agent, title="head", run_status="succeeded", task_status="blocked"
    )
    gave_up = make_task_run(
        db,
        tenants,
        agent,
        title="gave up",
        run_status="paused",
        stop_reason="deadline",
        parent=head,
    )
    died = make_task_run(
        db, tenants, agent, title="died", run_status="running", lease="now() - interval '1 hour'"
    )
    working = make_task_run(
        db,
        tenants,
        agent,
        title="working",
        run_status="running",
        lease="now() + interval '3 hours'",
    )
    waiting = make_task_run(
        db,
        tenants,
        agent,
        title="waiting on the owner",
        run_status="paused",
        stop_reason="awaiting_approval",
    )
    killed = make_task_run(
        db, tenants, agent, title="kill switch", run_status="paused", stop_reason="kill_switch"
    )
    fresh = make_task_run(
        db,
        tenants,
        agent,
        title="has wakes left",
        run_status="paused",
        stop_reason="deadline",
        wake_count=1,
    )

    with as_service_role(db) as conn:
        early = conn.execute("select public.reap_stuck_tasks() as id").fetchall()
        # Two hours on: past the 60-minute limit.
        reaped = [
            r["id"]
            for r in conn.execute(
                "select public.reap_stuck_tasks(20, now() + interval '2 hours') as id"
            )
        ]
        rows = {
            r["id"]: (r["status"], r["error"])
            for r in conn.execute("select id, status, error from public.tasks").fetchall()
        }
        runs = conn.execute(
            "select status, stop_reason from public.runs where task_id = %s", (str(gave_up),)
        ).fetchone()

    assert early == [], "nothing is stuck before the limit"
    assert sorted(reaped) == sorted([gave_up, died])
    for stuck in (gave_up, died):
        assert rows[stuck][0] == "failed" and "reaper" in rows[stuck][1]
    assert (runs["status"], runs["stop_reason"]) == ("failed", "stuck")
    for alive in (working, waiting, killed, fresh):
        assert rows[alive][0] == "running"
    assert rows[head][0] == "queued", "the parent wakes to see its sub-task failed"


# --- Loop detection (a real run) --------------------------------------------------------


@dataclass
class Looper:
    """A worker that asks for the same search over and over."""

    calls: list[str] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        self.calls.append("search")
        return ModelResponse(
            model="vendor/small",
            text="",
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.0001,
            latency_ms=1,
            provider="scripted",
            tool_calls=(
                ToolCall(
                    f"call_{len(self.calls)}", "brain_search", json.dumps({"query": "Acme Corp"})
                ),
            ),
            finish_reason="tool_calls",
        )


def test_the_same_tool_with_the_same_arguments_three_times_stops_the_run(
    dsn: str,
    team: Team,  # noqa: F811
) -> None:
    model = Looper()
    with connect(dsn) as connection:
        task = order(connection, user_id=team.user_id, org_id=team.org_id, agent="w1", title="Loop")
    (run_id,) = tick(dsn)

    result = advance_run(team_runtime(dsn, model), run_id, deadline_seconds=60)  # type: ignore[arg-type]

    assert (result.status, result.stop_reason) == ("failed", "loop_detected")
    assert status(dsn, task.id) == "failed"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        ran = conn.execute(
            "select count(*) as n from public.tool_calls where run_id = %s", (str(run_id),)
        ).fetchone()["n"]
    assert len(model.calls) == 3 and ran == 2, "the third repeat never ran"


# --- The kill switch stops a task tree mid-flight -------------------------------------------


def set_kill_switch(dsn: str, org_id: UUID, *, on: bool) -> None:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        conn.execute(
            "insert into public.system_flags (org_id, key, value) values (%s, 'kill_switch', %s) "
            "on conflict (org_id, key) do update set value = excluded.value",
            (str(org_id), json.dumps(on)),
        )


@dataclass
class SwitchedTeam(ScriptedTeam):
    """The team, but the owner hits the kill switch while w1 is working."""

    dsn: str = ""
    org_id: UUID | None = None
    tripped: bool = False

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        response = super().complete(model=model, messages=messages, **kw)
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "founding date" in user and not self.tripped:
            self.tripped = True
            set_kill_switch(self.dsn, self.org_id, on=True)  # type: ignore[arg-type]
        return response


def wakeups(dsn: str) -> list[UUID]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return [r["id"] for r in conn.execute("select public.pending_wakeups() as id").fetchall()]


def test_a_task_tree_stops_cleanly_mid_flight_and_resumes_when_the_owner_says(
    dsn: str,
    team: Team,  # noqa: F811
) -> None:
    model = SwitchedTeam(dsn=dsn, org_id=team.org_id)
    rt = team_runtime(dsn, model)
    with connect(dsn) as connection:
        root = order(
            connection,
            user_id=team.user_id,
            org_id=team.org_id,
            agent="lead",
            title="Brief on Acme Corp",
            instructions="Founding date and headcount.",
        )
    (head_run,) = tick(dsn)
    assert advance_run(rt, head_run, deadline_seconds=60).status == "succeeded"

    # w1 starts; the switch goes on during its first turn.
    w1_run, w2_run = tick(dsn)
    stopped = advance_run(rt, w1_run, deadline_seconds=60)
    assert (stopped.status, stopped.stop_reason) == ("paused", "kill_switch")

    # Nothing moves while it is on: w2's run is refused at once, nothing new
    # starts, nothing wakes.
    refused = advance_run(rt, w2_run, deadline_seconds=60)
    assert (refused.status, refused.stop_reason, refused.steps_taken) == (
        "paused",
        "kill_switch",
        0,
    )
    assert tick(dsn) == [] and wakeups(dsn) == []
    assert status(dsn, root.id) == "blocked"
    spent_while_stopped = len(model.calls)

    # Switched off, still nothing restarts by itself (ADR 008)...
    set_kill_switch(dsn, team.org_id, on=False)
    assert wakeups(dsn) == []
    assert len(model.calls) == spent_while_stopped

    # ...until the owner resumes. Each run carries on from its checkpoint.
    with connect(dsn) as connection:
        assert resume_paused_runs(connection, user_id=team.user_id, org_id=team.org_id) == 2
    assert sorted(wakeups(dsn)) == sorted([w1_run, w2_run])
    for run_id in (w1_run, w2_run):
        assert advance_run(rt, run_id, deadline_seconds=60).status == "succeeded"
    (head_again,) = tick(dsn)
    assert advance_run(rt, head_again, deadline_seconds=60).status == "succeeded"
    assert status(dsn, root.id) == "done"

    with connect(dsn) as connection:
        rows = tree(connection, user_id=team.user_id, root_task_id=root.id)
    head = next(r for r in rows if r["depth"] == 0)
    assert head["tree_cost_usd"] == sum(Decimal(r["own_cost_usd"]) for r in rows)
    # w1 was stopped after its first turn and asked the model only once more
    # for that turn's work: helper, report, say (4 turns in all, as unstopped).
    w1_calls = [c for c in model.calls if c.startswith(("w1:", "helper:"))]
    assert w1_calls == ["w1:task", "helper:say", "w1:report_result", "w1:say"]


# --- Through the API ------------------------------------------------------------------


@pytest.fixture
def api(db: psycopg.Connection, agent: UUID, settings: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.owner_api import get_connection
    from tests.test_health import auth, make_token

    def same_connection() -> Any:
        yield db

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_connection] = same_connection
    yield TestClient(app, headers=auth(make_token()))
    app.dependency_overrides.clear()


def test_the_owner_sets_levels_reads_suggestions_and_resumes_through_the_api(
    api: Any, db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    decisions(db, tenants, agent, "tool:r3_test", agreed=30, total=30)

    moved = api.post("/agents/ladder/autonomy", json={"level": "L2"})
    unknown = api.post("/agents/nobody/autonomy", json={"level": "L2"})
    invalid = api.post("/agents/ladder/autonomy", json={"level": "L9"})
    suggested = api.get("/autonomy/suggestions")

    assert moved.json() == {"name": "ladder", "autonomy_level": "L2"}
    assert (unknown.status_code, invalid.status_code) == (404, 422)
    assert [(s["action_key"], s["suggested_level"]) for s in suggested.json()] == [
        ("tool:r3_test", "L3")
    ]

    with as_service_role(db) as conn:
        conn.execute(
            "insert into public.system_flags (org_id, key, value) "
            "values (%s, 'kill_switch', 'true')",
            (str(tenants.org_a),),
        )
    assert api.post("/runs/resume", json={}).status_code == 409, "not while it is still on"
