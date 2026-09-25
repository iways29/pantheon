"""Scheduled triggers: fixed tasks at fixed times, once a day (ADR 008).

Step 4 acceptance: sending the same trigger twice produces one run and one
side effect, and a failed run can be retried without duplicating actions.
The scheduler's decision (`dispatch_due_triggers`) is pure SQL and takes the
current time as an argument, so these tests set the clock instead of waiting.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.agents import triggers
from app.agents.runs import RunBusy, advance_run, report, retry_run
from app.config import Settings, get_settings
from app.db import as_service_role, connect
from app.internal import get_runtime
from app.main import app
from tests.conftest_db import Tenants
from tests.test_gateway import make_agent
from tests.test_runs import (  # noqa: F401  (`live` is a fixture)
    LiveOrg,
    ScriptedModel,
    live,
    new_run,
    runtime,
    set_kill_switch,
    sql,
)

IST = ZoneInfo("Asia/Kolkata")
TASK = {"question": "What changed in AI agents overnight?"}


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in September 2026, in the owner's (India) time."""
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


MONDAY, TUESDAY, SATURDAY = 28, 29, 26  # September 2026


def test_the_calendar_used_below_is_right() -> None:
    assert [ist(d, 0).weekday() for d in (MONDAY, TUESDAY, SATURDAY)] == [0, 1, 5]


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants) -> uuid.UUID:
    return make_agent(db, tenants.org_a, name="researcher")


def add(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
    *,
    name: str = "morning-brief",
    enabled: bool = True,
    **kwargs: Any,
) -> triggers.Trigger:
    options: dict[str, Any] = {"time_of_day": time(8, 30), "timezone": "Asia/Kolkata"} | kwargs
    trigger = triggers.create(
        db,
        user_id=tenants.user_a,
        org_id=tenants.org_a,
        agent_id=agent,
        name=name,
        task=TASK,
        **options,
    )
    if enabled:
        trigger = triggers.set_enabled(db, user_id=tenants.user_a, name=name, enabled=True)
    return trigger


def dispatch(db: psycopg.Connection, at: datetime) -> list[uuid.UUID]:
    with as_service_role(db) as conn:
        rows = conn.execute("select public.dispatch_due_triggers(%s) as id", (at,)).fetchall()
    return [row["id"] for row in rows]


def tasks(db: psycopg.Connection, org_id: uuid.UUID) -> list[dict[str, Any]]:
    with as_service_role(db) as conn:
        return conn.execute(
            "select * from public.tasks where org_id = %s order by created_at", (str(org_id),)
        ).fetchall()


def runs(db: psycopg.Connection, org_id: uuid.UUID) -> list[dict[str, Any]]:
    with as_service_role(db) as conn:
        return conn.execute(
            "select * from public.runs where org_id = %s order by created_at", (str(org_id),)
        ).fetchall()


# --- When a trigger is due ----------------------------------------------------


def test_a_new_trigger_is_off_and_fires_nothing_until_enabled(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    trigger = add(db, tenants, agent, enabled=False)

    assert trigger.enabled is False
    assert dispatch(db, ist(MONDAY, 9)) == []
    assert runs(db, tenants.org_a) == []


def test_it_does_not_fire_before_its_time(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    add(db, tenants, agent)
    assert dispatch(db, ist(MONDAY, 8, 29)) == []


def test_it_fires_at_its_local_time_with_the_task_and_the_caps(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    trigger = add(db, tenants, agent)

    # 08:30 in India is 03:00 UTC: the trigger is read in its own time zone.
    created = dispatch(db, ist(MONDAY, 8, 30).astimezone(UTC))

    # Step 7.3: a trigger creates a task from its template (ADR 019).
    assert len(created) == 1
    (task,) = tasks(db, tenants.org_a)
    assert task["id"] == created[0]
    assert task["created_by"] == f"trigger:{trigger.id}"
    assert task["status"] == "queued"
    assert task["input"] == TASK
    assert task["requested_by"] == tenants.user_a
    assert (task["max_steps"], task["max_tokens"]) == (25, 50000)
    assert task["idempotency_key"] == f"trigger:{trigger.id}:2026-09-28"
    with as_service_role(db) as conn:
        fired = conn.execute(
            "select payload from public.events where type = 'trigger_fired' and agent_id = %s",
            (str(agent),),
        ).fetchall()
    assert [f["payload"]["slot"] for f in fired] == ["2026-09-28"]
    assert fired[0]["payload"]["task_id"] == str(task["id"])

    # The scheduler then gives the queued task a run.
    with as_service_role(db) as conn:
        started = conn.execute("select public.dispatch_queued_tasks() as id").fetchall()
    (run,) = runs(db, tenants.org_a)
    assert [s["id"] for s in started] == [run["id"]]
    assert run["trigger"] == "task" and run["status"] == "pending"
    assert run["input"] == {
        **TASK,
        "task_id": str(task["id"]),
        "title": "morning-brief",
        "instructions": TASK["question"],
    }
    assert (run["max_steps"], run["max_tokens"]) == (25, 50000)
    assert tasks(db, tenants.org_a)[0]["status"] == "running"


def test_the_same_slot_produces_one_run_however_often_it_is_asked(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    """Acceptance: the same trigger twice is one task, so one run."""
    trigger = add(db, tenants, agent)

    first = dispatch(db, ist(MONDAY, 8, 31))
    second = dispatch(db, ist(MONDAY, 8, 32))
    assert (len(first), second) == (1, [])

    # Even if the "already fired today" marker were lost, the run's own
    # idempotency key stops a second run for the same slot.
    with as_service_role(db) as conn:
        conn.execute(
            "update public.triggers set last_slot = null where id = %s", (str(trigger.id),)
        )
    assert dispatch(db, ist(MONDAY, 8, 33)) == []
    assert len(tasks(db, tenants.org_a)) == 1


def test_it_fires_again_the_next_day(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    add(db, tenants, agent)
    dispatch(db, ist(MONDAY, 8, 30))
    assert len(dispatch(db, ist(TUESDAY, 8, 30))) == 1
    assert len(tasks(db, tenants.org_a)) == 2


def test_it_skips_days_it_is_not_set_for(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    add(db, tenants, agent)  # weekdays by default
    assert dispatch(db, ist(SATURDAY, 8, 30)) == []

    add(db, tenants, agent, name="weekend", days_of_week=triggers.parse_days("sat"))
    assert len(dispatch(db, ist(SATURDAY, 8, 30))) == 1


def test_a_missed_tick_is_made_up_but_only_within_the_grace_period(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    add(db, tenants, agent, grace_minutes=120)

    assert dispatch(db, ist(MONDAY, 10, 31)) == [], "121 minutes late is a skipped slot"
    assert len(dispatch(db, ist(MONDAY, 10, 29))) == 1, "119 minutes late still runs"


@pytest.mark.parametrize("blocker", ["agent", "department", "kill_switch"])
def test_nothing_fires_while_the_agent_department_or_kill_switch_is_off(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID, blocker: str
) -> None:
    add(db, tenants, agent)
    with as_service_role(db) as conn:
        if blocker == "agent":
            conn.execute("update public.agents set enabled = false where id = %s", (str(agent),))
        elif blocker == "department":
            conn.execute("update public.departments set enabled = false")
        else:
            conn.execute(
                "insert into public.system_flags (org_id, key, value) "
                "values (%s, 'kill_switch', 'true')",
                (str(tenants.org_a),),
            )

    assert dispatch(db, ist(MONDAY, 9)) == []
    assert runs(db, tenants.org_a) == []


def test_a_slot_missed_to_the_kill_switch_still_runs_once_it_is_lifted_in_time(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    add(db, tenants, agent)
    with as_service_role(db) as conn:
        conn.execute(
            "insert into public.system_flags (org_id, key, value) "
            "values (%s, 'kill_switch', 'true')",
            (str(tenants.org_a),),
        )
    assert dispatch(db, ist(MONDAY, 8, 45)) == []

    with as_service_role(db) as conn:
        conn.execute("update public.system_flags set value = 'false'")
    assert len(dispatch(db, ist(MONDAY, 9, 15))) == 1


# --- Changes to a trigger are audited, firing is not --------------------------


def test_creating_changing_and_deleting_a_trigger_are_events_and_firing_is_not(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    add(db, tenants, agent)
    dispatch(db, ist(MONDAY, 8, 30))
    triggers.set_enabled(db, user_id=tenants.user_a, name="morning-brief", enabled=False)
    triggers.delete(db, user_id=tenants.user_a, name="morning-brief")

    with as_service_role(db) as conn:
        events = conn.execute(
            "select type from public.events where org_id = %s and type like 'trigger_%%' "
            "order by created_at, id",
            (str(tenants.org_a),),
        ).fetchall()
    assert sorted(e["type"] for e in events) == [
        "trigger_created",
        "trigger_deleted",
        "trigger_fired",
        "trigger_updated",  # enabling
        "trigger_updated",  # disabling
    ]


# --- The morning routine is checked at the door -------------------------------


def test_the_database_refuses_a_bad_zone_day_or_task(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    for bad in (
        {"timezone": "Mars/Olympus"},
        {"days_of_week": [7]},
        {"days_of_week": []},
        {"grace_minutes": 0},
    ):
        with pytest.raises(psycopg.errors.Error), db.transaction():
            add(db, tenants, agent, enabled=False, **bad)


def test_days_and_times_are_read_the_way_a_person_writes_them() -> None:
    assert triggers.parse_days("mon-fri") == [1, 2, 3, 4, 5]
    assert triggers.parse_days("all") == list(range(7))
    assert triggers.parse_days("Mon, wed,FRI") == [1, 3, 5]
    assert triggers.parse_days("sat,sun") == [0, 6]
    with pytest.raises(ValueError, match="backwards"):
        triggers.parse_days("sat-sun")  # a range may not wrap round the week
    assert triggers.parse_time("08:30") == time(8, 30)
    with pytest.raises(ValueError):
        triggers.parse_days("someday")
    with pytest.raises(ValueError):
        triggers.parse_time("half past eight")


def test_another_orgs_member_cannot_see_or_create_triggers_on_my_agent(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    add(db, tenants, agent)

    assert triggers.list_triggers(db, user_id=tenants.user_b) == []
    with pytest.raises(psycopg.errors.Error), db.transaction():
        triggers.create(
            db,
            user_id=tenants.user_b,
            org_id=tenants.org_a,
            agent_id=agent,
            name="sneaky",
            task=TASK,
            time_of_day=time(1),
            timezone="UTC",
        )


# --- Waking runs: who gets poked, and never forever ---------------------------


def make_run(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID, *, trigger: str, **columns: Any
) -> uuid.UUID:
    fields = {
        "org_id": str(tenants.org_a),
        "agent_id": str(agent),
        "trigger": trigger,
        "idempotency_key": str(uuid.uuid4()),
        "requested_by": str(tenants.user_a),
    } | columns
    with as_service_role(db) as conn:
        return conn.execute(
            f"insert into public.runs ({', '.join(fields)}) "
            f"values ({', '.join(['%s'] * len(fields))}) returning id",
            tuple(fields.values()),
        ).fetchone()["id"]


def wakeups(db: psycopg.Connection) -> list[uuid.UUID]:
    with as_service_role(db) as conn:
        return [r["id"] for r in conn.execute("select public.pending_wakeups() as id").fetchall()]


def test_a_fresh_scheduled_run_is_poked_once_then_left_alone_for_a_while(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    run = make_run(db, tenants, agent, trigger="schedule")

    assert wakeups(db) == [run]
    assert wakeups(db) == [], "poked again only after the spacing has passed"

    with as_service_role(db) as conn:
        conn.execute("update public.runs set last_wake_at = now() - interval '1 minute'")
    assert wakeups(db) == [run]


def test_a_run_is_poked_at_most_five_times(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    run = make_run(db, tenants, agent, trigger="schedule")
    for _ in range(5):
        assert wakeups(db) == [run]
        with as_service_role(db) as conn:
            conn.execute("update public.runs set last_wake_at = now() - interval '1 minute'")

    assert wakeups(db) == [], "a run that will not start is not retried forever"


def test_only_runs_that_need_the_api_are_poked(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID
) -> None:
    make_run(db, tenants, agent, trigger="manual")  # started by hand, driven by hand
    make_run(db, tenants, agent, trigger="schedule", status="succeeded")
    make_run(db, tenants, agent, trigger="schedule", status="failed", stop_reason="error")
    make_run(db, tenants, agent, trigger="schedule", status="paused", stop_reason="budget_exceeded")
    make_run(db, tenants, agent, trigger="schedule", status="paused", stop_reason="kill_switch")
    lease = "now() + interval '5 minutes'"
    with as_service_role(db) as conn:  # a run some invocation is advancing right now
        held = make_run(db, tenants, agent, trigger="schedule", status="running")
        conn.execute(
            f"update public.runs set lease_expires_at = {lease} where id = %s", (str(held),)
        )
    assert wakeups(db) == []

    ran_out_of_time = make_run(
        db, tenants, agent, trigger="schedule", status="paused", stop_reason="deadline"
    )
    died = make_run(db, tenants, agent, trigger="schedule", status="running")
    with as_service_role(db) as conn:
        conn.execute(
            "update public.runs set lease_expires_at = now() - interval '1 minute' where id = %s",
            (str(died),),
        )
    assert sorted(wakeups(db)) == sorted([ran_out_of_time, died])


# --- The API endpoint the scheduler calls -------------------------------------

SECRET = "trigger-secret-long-enough-for-tests"


@pytest.fixture
def api(dsn: str, live: LiveOrg) -> Iterator[TestClient]:  # noqa: F811
    model = ScriptedModel()
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="test", trigger_secret=SECRET
    )
    app.dependency_overrides[get_runtime] = lambda: runtime(dsn, model)
    yield TestClient(app)
    app.dependency_overrides.clear()


def advance(api: TestClient, run_id: uuid.UUID, secret: str | None = SECRET) -> Any:
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    return api.post(f"/internal/runs/{run_id}/advance", headers=headers)


def test_the_endpoint_advances_a_run_and_a_repeat_changes_nothing(
    api: TestClient,
    dsn: str,
    live: LiveOrg,  # noqa: F811
) -> None:
    run_id = new_run(dsn, live)

    first = advance(api, run_id)
    assert first.status_code == 200
    assert (first.json()["status"], first.json()["stop_reason"]) == ("succeeded", "completed")

    # Serverless delivery can repeat a request. The second is a no-op.
    again = advance(api, run_id)
    assert (again.status_code, again.json()["status"]) == (200, "busy")
    assert (
        len(sql(dsn, "select id from public.facts where created_by_run_id = %s", str(run_id))) == 2
    )


def test_the_endpoint_refuses_callers_without_the_shared_secret(
    api: TestClient,
    dsn: str,
    live: LiveOrg,  # noqa: F811
) -> None:
    run_id = new_run(dsn, live)

    assert advance(api, run_id, secret=None).status_code == 401
    assert advance(api, run_id, secret="wrong").status_code == 401
    assert report(connect(dsn), run_id).status == "pending"


def test_the_endpoint_is_closed_when_no_secret_is_configured(
    api: TestClient,
    dsn: str,
    live: LiveOrg,  # noqa: F811
) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(environment="test")
    assert advance(api, new_run(dsn, live), secret="anything").status_code == 503


def test_the_endpoint_says_so_for_a_run_that_does_not_exist(api: TestClient) -> None:
    assert advance(api, uuid.uuid4()).status_code == 404


# --- Acceptance: a failed run can be retried without repeating what it did ----


def test_a_run_that_failed_on_an_error_retries_without_duplicating_facts(
    dsn: str,
    live: LiveOrg,  # noqa: F811
) -> None:
    def break_extract(kind: str) -> None:
        if kind == "extract":
            raise ValueError("the model returned nonsense")

    run_id = new_run(dsn, live)
    failed = advance_run(
        runtime(dsn, ScriptedModel(on_call=break_extract)), run_id, deadline_seconds=60
    )
    assert (failed.status, failed.stop_reason) == ("failed", "error")
    assert sql(dsn, "select id from public.facts where created_by_run_id = %s", str(run_id)) == []

    with connect(dsn) as connection:
        retry_run(connection, run_id)
    model = ScriptedModel()
    done = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert (done.status, done.stop_reason) == ("succeeded", "completed")
    assert model.calls == ["extract"], "only the step that failed ran again"
    facts = sql(dsn, "select id from public.facts where created_by_run_id = %s", str(run_id))
    assert len(facts) == 2

    # And retrying the finished run again cannot store anything twice.
    with pytest.raises(RunBusy):
        with connect(dsn) as connection:
            retry_run(connection, run_id)


def test_a_run_stopped_by_a_cap_cannot_be_retried(dsn: str, live: LiveOrg) -> None:  # noqa: F811
    run_id = new_run(dsn, live, max_steps=1)
    advance_run(runtime(dsn, ScriptedModel()), run_id, deadline_seconds=60)

    with pytest.raises(RunBusy, match="only a run that failed on an error"):
        with connect(dsn) as connection:
            retry_run(connection, run_id)


def test_the_scheduler_to_finished_run_path_end_to_end(
    dsn: str,
    live: LiveOrg,  # noqa: F811
) -> None:
    """A trigger becomes a run through the scheduler's SQL, and the run
    finishes through the same code the endpoint calls."""
    with connect(dsn) as connection:
        trigger = triggers.create(
            connection,
            user_id=live.user_id,
            org_id=live.org_id,
            agent_id=live.agent_id,
            name="morning",
            task={"question": "What is the boiling point of water at sea level?"},
            time_of_day=time(8, 30),
            timezone="Asia/Kolkata",
        )
        triggers.set_enabled(connection, user_id=live.user_id, name="morning", enabled=True)
        with as_service_role(connection) as conn:
            queued = conn.execute(
                "select public.dispatch_due_triggers(%s) as id", (ist(MONDAY, 8, 30),)
            ).fetchall()
            created = conn.execute("select public.dispatch_queued_tasks() as id").fetchall()
            woken = conn.execute("select public.pending_wakeups() as id").fetchall()

    assert len(queued) == 1 and len(created) == 1
    assert [w["id"] for w in woken] == [created[0]["id"]]
    result = advance_run(runtime(dsn, ScriptedModel()), created[0]["id"], deadline_seconds=60)
    assert (result.status, result.stop_reason) == ("succeeded", "completed")
    row = sql(
        dsn, "select trigger, idempotency_key from public.runs where id = %s", str(created[0]["id"])
    )
    assert row[0]["trigger"] == "task"
    assert row[0]["idempotency_key"] == f"task:{queued[0]['id']}:1"
    task = sql(
        dsn, "select status, idempotency_key from public.tasks where id = %s", str(queued[0]["id"])
    )
    assert task[0]["status"] == "done", "the task finished with its run"
    assert task[0]["idempotency_key"].startswith(f"trigger:{trigger.id}:")
