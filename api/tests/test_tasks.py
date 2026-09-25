"""Step 7.3: tasks and delegation, enforced in the database (ADR 019).

Acceptance: a head splits a task into two worker tasks and finishes when both
do, across separate invocations; the limits refuse the fifth child and the
fourth level; a repeated order is one task. (The head and worker *runners*
that drive this with real model calls are Step 7.4.)
"""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.db import acting_as, as_service_role
from app.tasks import TaskRefused, cancel, children, delegate, order, tree
from app.tools import ToolContext, ToolRuntime, seed_tools
from tests.conftest_db import Tenants
from tests.test_gateway import make_agent, make_department


@pytest.fixture
def org(db: psycopg.Connection, tenants: Tenants) -> dict[str, UUID]:
    dept = make_department(db, tenants.org_a, name="research", budget="5")
    ids = {
        name: make_agent(db, tenants.org_a, name=name, department_id=dept)
        for name in ("cos", "lead", "w1", "w2", "w3", "w4", "w5")
    }
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.agents set role_type = 'chief_of_staff' where id = %s",
            (str(ids["cos"]),),
        )
        cursor.execute(
            "update public.agents set role_type = 'head' where id = %s", (str(ids["lead"]),)
        )
    return ids


def root(db: psycopg.Connection, tenants: Tenants, agent: str = "lead", **kw: Any):
    return order(
        db,
        user_id=tenants.user_a,
        org_id=tenants.org_a,
        agent=agent,
        title=kw.pop("title", "Brief on Acme"),
        **kw,
    )


def split(
    db: psycopg.Connection,
    tenants: Tenants,
    org: dict[str, UUID],
    parent: UUID,
    by: str,
    to: str,
    title: str,
    **kw: Any,
):
    with (
        acting_as(db, user_id=str(tenants.user_a), agent_id=str(org[by])) as conn,
        conn.cursor() as cursor,
    ):
        return delegate(
            cursor,
            org_id=tenants.org_a,
            parent_task_id=parent,
            by_agent_id=org[by],
            to_agent=to,
            title=title,
            instructions=f"Do: {title}",
            **kw,
        )


def status(db: psycopg.Connection, task_id: UUID) -> str:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("select status from public.tasks where id = %s", (str(task_id),))
        return cursor.fetchone()["status"]


def start_runs(db: psycopg.Connection) -> dict[UUID, UUID]:
    """What the scheduler does each minute: a run per queued task."""
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("select public.dispatch_queued_tasks() as id")
        run_ids = [r["id"] for r in cursor.fetchall()]
        cursor.execute("select id, task_id from public.runs where id = any(%s)", (run_ids,))
        return {r["task_id"]: r["id"] for r in cursor.fetchall()}


def finish(db: psycopg.Connection, run_id: UUID, *, ok: bool = True, output: Any = None) -> None:
    """An invocation ending the run, as advance_run would."""
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.runs set status = %s, stop_reason = %s, output = %s, ended_at = now() "
            "where id = %s",
            (
                "succeeded" if ok else "failed",
                "completed" if ok else "error",
                json.dumps(output or {}),
                str(run_id),
            ),
        )


# --- Orders -----------------------------------------------------------------


def test_a_repeated_order_is_one_task(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    first = root(db, tenants)
    again = root(db, tenants)

    assert again.id == first.id and again.created is False
    assert first.depth == 0 and first.status == "queued"


# --- A head splits, workers finish, the head wakes ------------------------------


def test_a_head_splits_its_task_and_wakes_when_both_workers_finish(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    task = root(db, tenants)

    # Invocation 1: the head's run plans and creates two worker tasks.
    head_run = start_runs(db)[task.id]
    a = split(db, tenants, org, task.id, "lead", "w1", "Find the founding date")
    b = split(db, tenants, org, task.id, "lead", "w2", "Find the headcount")
    finish(db, head_run, output={"plan": "two lookups"})
    assert status(db, task.id) == "blocked", "waiting on its workers, not in memory"

    # Invocations 2 and 3: each worker runs and reports.
    worker_runs = start_runs(db)
    assert set(worker_runs) == {a.id, b.id}
    finish(db, worker_runs[a.id], output={"summary": "2019"})
    assert status(db, task.id) == "blocked"
    finish(db, worker_runs[b.id], output={"summary": "40 people"})
    assert status(db, task.id) == "queued", "the last child woke the head"

    # Invocation 4: the head's next run sees both results and finishes.
    second = start_runs(db)[task.id]
    with (
        acting_as(db, user_id=str(tenants.user_a), agent_id=str(org["lead"])) as conn,
        conn.cursor() as cursor,
    ):
        results = {c["title"]: c["result"]["summary"] for c in children(cursor, task.id)}
    assert results == {"Find the founding date": "2019", "Find the headcount": "40 people"}
    finish(db, second, output={"summary": "Founded 2019, 40 people"})
    assert status(db, task.id) == "done"

    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("select attempts from public.tasks where id = %s", (str(task.id),))
        assert cursor.fetchone()["attempts"] == 2
        cursor.execute(
            "select type from public.events where payload->>'task_id' = %s order by created_at",
            (str(task.id),),
        )
        assert [r["type"] for r in cursor.fetchall()] == [
            "task_queued",
            "task_running",
            "task_blocked",
            "task_queued",
            "task_running",
            "task_done",
        ]


def test_a_failed_run_fails_its_task_and_the_parent_still_wakes(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    task = root(db, tenants)
    head_run = start_runs(db)[task.id]
    child = split(db, tenants, org, task.id, "lead", "w1", "Look something up")
    finish(db, head_run)

    finish(db, start_runs(db)[child.id], ok=False)

    assert status(db, child.id) == "failed"
    assert status(db, task.id) == "queued"


# --- Limits, enforced by the database --------------------------------------------


def test_the_fifth_child_is_refused(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    task = root(db, tenants)
    for worker in ("w1", "w2", "w3", "w4"):
        split(db, tenants, org, task.id, "lead", worker, f"Part {worker}")

    with pytest.raises(TaskRefused, match="4 sub-tasks"):
        split(db, tenants, org, task.id, "lead", "w5", "One too many")


def test_the_fourth_level_is_refused(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    top = root(db, tenants, agent="cos", title="Company question")
    level1 = split(db, tenants, org, top.id, "cos", "lead", "Research it")
    level2 = split(db, tenants, org, level1.id, "lead", "w1", "Look it up")
    assert (top.depth, level1.depth, level2.depth) == (0, 1, 2)

    with acting_as(db, user_id=str(tenants.user_a)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.agents set role_type = 'head' where id = %s", (str(org["w1"]),)
        )
    with pytest.raises(TaskRefused, match="too deep"):
        split(db, tenants, org, level2.id, "w1", "w2", "A fourth level")


def test_a_worker_may_not_create_tasks(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    task = root(db, tenants, agent="w1")

    with pytest.raises(TaskRefused, match="Chief of Staff and heads"):
        split(db, tenants, org, task.id, "w1", "w2", "Pass it on")


def test_a_head_may_only_split_its_own_task(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    someone_elses = root(db, tenants, agent="cos")

    with pytest.raises(TaskRefused, match="its own task"):
        split(db, tenants, org, someone_elses.id, "lead", "w1", "Hijack")


def test_the_daily_department_limit_is_owner_data(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into public.delegation_limits (org_id, max_tasks_per_department_per_day) "
            "values (%s, 2) on conflict (org_id) "
            "do update set max_tasks_per_department_per_day = 2",
            (str(tenants.org_a),),
        )
    root(db, tenants, title="One")
    root(db, tenants, title="Two")

    with pytest.raises(TaskRefused, match="2 tasks today"):
        root(db, tenants, title="Three")


def test_a_childs_budget_must_fit_what_its_parent_has_left(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    task = root(db, tenants, max_cost_usd=Decimal("0.10"))
    split(db, tenants, org, task.id, "lead", "w1", "Half", max_cost_usd=Decimal("0.06"))

    with pytest.raises(TaskRefused, match="exceeds what task"):
        split(db, tenants, org, task.id, "lead", "w2", "Too much", max_cost_usd=Decimal("0.05"))
    with pytest.raises(TaskRefused, match="needs its own budget"):
        split(db, tenants, org, task.id, "lead", "w3", "Unbudgeted")
    split(db, tenants, org, task.id, "lead", "w2", "The rest", max_cost_usd=Decimal("0.04"))


def test_an_agent_session_cannot_create_agents_or_schedules(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with (
            acting_as(db, user_id=str(tenants.user_a), agent_id=str(org["cos"])) as conn,
            conn.cursor() as cursor,
        ):
            cursor.execute(
                "insert into public.agents (org_id, department_id, name, role) "
                "select org_id, department_id, 'clone', 'worker' from public.agents where id = %s",
                (str(org["cos"]),),
            )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with (
            acting_as(db, user_id=str(tenants.user_a), agent_id=str(org["cos"])) as conn,
            conn.cursor() as cursor,
        ):
            cursor.execute(
                "insert into public.triggers "
                "(org_id, agent_id, name, task, time_of_day, timezone, run_as) "
                "values (%s, %s, 'self', '{}', '08:00', 'UTC', %s)",
                (str(tenants.org_a), str(org["cos"]), str(tenants.user_a)),
            )
    # An update the policy does not allow touches no rows rather than raising.
    with as_service_role(db) as conn:
        conn.execute("update public.agents set enabled = false where id = %s", (str(org["w1"]),))
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(org["cos"])) as conn:
        changed = conn.execute(
            "update public.agents set enabled = true where id = %s", (str(org["w1"]),)
        ).rowcount
    assert changed == 0


# --- Cost, cancelling, the tools -------------------------------------------------------


def test_every_level_of_the_tree_shows_its_cost(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    task = root(db, tenants)
    head_run = start_runs(db)[task.id]
    child = split(db, tenants, org, task.id, "lead", "w1", "Look up")
    child_run = start_runs(db)[child.id]
    with as_service_role(db) as conn, conn.cursor() as cursor:
        for run, agent, cost in ((head_run, org["lead"], "0.002"), (child_run, org["w1"], "0.001")):
            cursor.execute(
                "insert into public.model_calls (org_id, run_id, agent_id, model, cost_usd) "
                "values (%s, %s, %s, 'm', %s)",
                (str(tenants.org_a), str(run), str(agent), cost),
            )

    rows = {r["title"]: r for r in tree(db, user_id=tenants.user_a, root_task_id=task.id)}

    assert rows["Brief on Acme"]["own_cost_usd"] == Decimal("0.002")
    assert rows["Brief on Acme"]["tree_cost_usd"] == Decimal("0.003")
    assert rows["Look up"]["tree_cost_usd"] == Decimal("0.001")


def test_cancelling_a_task_cancels_its_unfinished_subtree(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    task = root(db, tenants)
    a = split(db, tenants, org, task.id, "lead", "w1", "One")
    b = split(db, tenants, org, task.id, "lead", "w2", "Two")

    assert cancel(db, user_id=tenants.user_a, task_id=task.id) == 3
    assert {status(db, t) for t in (task.id, a.id, b.id)} == {"cancelled"}


def test_the_task_tools_respect_roles(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID]
) -> None:
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.agents set allowed_tools = '{create_task,report_result}' "
            "where org_id = %s",
            (str(tenants.org_a),),
        )
    task = root(db, tenants)
    worker_task = root(db, tenants, agent="w1", title="Worker job")

    def runtime_for(agent: str, task_id: UUID) -> ToolRuntime:
        conn = db
        return ToolRuntime(
            ToolContext(conn, tenants.org_a, org[agent], agent, extras={"task_id": str(task_id)})
        )

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(org["lead"])):
        made = runtime_for("lead", task.id).call(
            "create_task", {"assign_to": "w1", "title": "Find it", "instructions": "Find the date."}
        )
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(org["w1"])):
        refused = runtime_for("w1", worker_task.id).call(
            "create_task", {"assign_to": "w2", "title": "Pass", "instructions": "Do my job."}
        )
        reported = runtime_for("w1", worker_task.id).call(
            "report_result", {"summary": "Founded in 2019."}
        )

    assert made.status == "ok" and made.output["new"] is True
    assert refused.status == "error" and "Chief of Staff and heads" in refused.output["error"]
    assert reported.status == "ok"
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("select result from public.tasks where id = %s", (str(worker_task.id),))
        assert cursor.fetchone()["result"] == {"summary": "Founded in 2019."}


def test_the_owner_orders_and_reads_a_tree_through_the_api(
    db: psycopg.Connection, tenants: Tenants, org: dict[str, UUID], settings: Any
) -> None:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.owner_api import get_connection
    from tests.test_health import auth, make_token

    def same() -> Any:
        yield db

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_connection] = same
    try:
        api = TestClient(app, headers=auth(make_token()))
        body = {"agent": "lead", "title": "Brief on Acme", "instructions": "Two facts."}
        first = api.post("/tasks", json=body)
        again = api.post("/tasks", json=body)
        tree_rows = api.get(f"/tasks/{first.json()['id']}")
        cancelled = api.post(f"/tasks/{first.json()['id']}/cancel")
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert tree_rows.json()[0]["title"] == "Brief on Acme"
    assert cancelled.json() == {"cancelled": 1}
