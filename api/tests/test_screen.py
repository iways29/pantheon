"""The brain screen's reads (Step 10, ADR 036), through the API.

A small company is built in the database: an order from the owner routed by
the Chief of Staff to Research, split to a worker, with runs, spend, events,
an approval waiting and a held claim. Each endpoint is read as the owner, and
another org's owner sees none of it.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.brain import Brain, HashingEmbedder
from app.db import acting_as, as_service_role
from tests.conftest_db import Tenants, admit
from tests.test_gateway import make_department


@dataclass(frozen=True)
class World:
    order: UUID
    lead_task: UUID
    worker_task: UUID
    fact: UUID


def _agent(
    cursor: psycopg.Cursor,
    org: UUID,
    dept: UUID,
    name: str,
    role_type: str,
    runner: str = "deep",
    enabled: bool = True,
) -> UUID:
    cursor.execute(
        "insert into public.agents (org_id, department_id, name, role, role_type, runner, "
        "model_tier, enabled) values (%s, %s, %s, %s, %s, %s, 'cheap', %s) returning id",
        (str(org), str(dept), name, role_type, role_type, runner, enabled),
    )
    return cursor.fetchone()["id"]


def _task(
    cursor: psycopg.Cursor,
    tenants: Tenants,
    agent: UUID,
    title: str,
    status: str,
    parent: UUID | None = None,
    result: str | None = None,
) -> UUID:
    cursor.execute(
        "insert into public.tasks (org_id, assigned_agent_id, created_by, title, instructions, "
        "idempotency_key, requested_by, parent_task_id) "
        "values (%s, %s, 'owner', %s, %s, %s, %s, %s) returning id",
        (
            str(tenants.org_a),
            str(agent),
            title,
            f"{title}, please",
            str(uuid.uuid4()),
            str(tenants.user_a),
            str(parent) if parent else None,
        ),
    )
    task_id = cursor.fetchone()["id"]
    cursor.execute(
        "update public.tasks set status = %s, result = %s, "
        "finished_at = case when %s in ('done', 'failed') then now() end where id = %s",
        (
            status,
            None if result is None else psycopg.types.json.Jsonb({"summary": result}),
            status,
            str(task_id),
        ),
    )
    return task_id


def _run(
    cursor: psycopg.Cursor, tenants: Tenants, agent: UUID, task: UUID, status: str, cost: str
) -> None:
    cursor.execute(
        "insert into public.runs (org_id, agent_id, trigger, status, idempotency_key, "
        "requested_by, task_id) values (%s, %s, 'task', %s, %s, %s, %s) returning id",
        (str(tenants.org_a), str(agent), status, str(uuid.uuid4()), str(tenants.user_a), str(task)),
    )
    run_id = cursor.fetchone()["id"]
    cursor.execute(
        "insert into public.model_calls (org_id, run_id, agent_id, model, cost_usd) "
        "values (%s, %s, %s, 'test/model', %s)",
        (str(tenants.org_a), str(run_id), str(agent), cost),
    )


@pytest.fixture
def world(db: psycopg.Connection, tenants: Tenants) -> World:
    executive = make_department(db, tenants.org_a, name="executive", budget="0.1000")
    research = make_department(db, tenants.org_a, name="research", budget="0.5000")
    with as_service_role(db) as conn, conn.cursor() as cursor:
        chief = _agent(
            cursor, tenants.org_a, executive, "chief-of-staff", "chief_of_staff", "router"
        )
        lead = _agent(cursor, tenants.org_a, research, "research-lead", "head")
        worker = _agent(cursor, tenants.org_a, research, "web-researcher", "worker")
        _agent(cursor, tenants.org_a, research, "archivist", "worker", enabled=False)

        order = _task(cursor, tenants, chief, "Find three founders", "blocked")
        lead_task = _task(cursor, tenants, lead, "Find three founders", "done", order, "Found 3")
        worker_task = _task(cursor, tenants, worker, "Read Show HN", "running", lead_task)
        _run(cursor, tenants, chief, order, "succeeded", "0.010000")
        _run(cursor, tenants, lead, lead_task, "succeeded", "0.020000")
        _run(cursor, tenants, worker, worker_task, "running", "0.030000")

        events = [
            ("order_routed", chief, {"task_id": str(order), "department": "research"}),
            ("fact_write_decided", lead, {"outcome": "accepted"}),
            ("fact_write_decided", lead, {"outcome": "rejected"}),
            ("model_call", worker, {}),
            ("tool_called", worker, {"tool": "web_search"}),
        ]
        for kind, agent, payload in events:
            cursor.execute(
                "insert into public.events (org_id, agent_id, type, payload) "
                "values (%s, %s, %s, %s)",
                (str(tenants.org_a), str(agent), kind, psycopg.types.json.Jsonb(payload)),
            )
        cursor.execute(
            "insert into public.approvals (org_id, agent_id, task_id, action_type, payload) "
            "values (%s, %s, %s, 'send_email', '{}'), (%s, %s, null, 'fact_write', %s)",
            (
                str(tenants.org_a),
                str(lead),
                str(lead_task),
                str(tenants.org_a),
                str(lead),
                psycopg.types.json.Jsonb({"claim": "Vireo raised a seed round"}),
            ),
        )
        # Another org's work, which org A's owner must never see.
        other = make_department(db, tenants.org_b, name="other")
        _agent(cursor, tenants.org_b, other, "spy", "worker")

    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        fact = (
            Brain(conn, HashingEmbedder())
            .insert_fact(
                org_id=tenants.org_a,
                claim="The Unreal Lab backs founders early",
                admission=admit(db, tenants.org_a),
            )
            .id
        )
    return World(order, lead_task, worker_task, fact)


@pytest.fixture
def api(db: psycopg.Connection, settings: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.owner_api import get_connection
    from tests.test_health import auth, make_token

    def same_connection() -> Any:
        yield db

    def client(user: str | None = None) -> TestClient:
        # The API serves one owner; another org's owner is another deployment.
        chosen = settings.model_copy(update={"owner_user_id": user}) if user else settings
        app.dependency_overrides[get_settings] = lambda: chosen
        return TestClient(app, headers=auth(make_token(*([user] if user else []))))

    app.dependency_overrides[get_connection] = same_connection
    yield client
    app.dependency_overrides.clear()


def test_the_snapshot_shows_the_company(api: Any, world: World) -> None:
    body = api().get("/screen/snapshot").json()

    assert body["status"]["state"] == "running"
    states = {a["name"]: a["state"] for a in body["agents"]}
    assert states == {
        "archivist": "stopped",
        "chief-of-staff": "idle",
        "research-lead": "waiting",
        "web-researcher": "working",
    }
    depts = {d["name"]: d for d in body["departments"]}
    assert depts["research"]["head"] == "research-lead"
    assert depts["research"]["spent_today_usd"] == pytest.approx(0.05)
    assert [f["claim"] for f in body["facts"]] == ["The Unreal Lab backs founders early"]
    assert body["facts"][0]["x"] is None, "no projection yet"
    assert [h["claim"] for h in body["held"]] == ["Vireo raised a seed round"]


def test_agents_and_departments_alone(api: Any, world: World) -> None:
    body = api().get("/screen/agents").json()
    assert {a["name"] for a in body["agents"]} >= {"research-lead", "web-researcher"}
    assert {d["name"] for d in body["departments"]} == {"executive", "research"}


def test_the_pulse_counts_today(api: Any, world: World) -> None:
    pulse = api().get("/screen/pulse").json()

    assert pulse["spend_usd"] == pytest.approx(0.06)
    assert pulse["budget_usd"] == pytest.approx(0.6)
    assert (pulse["facts_added"], pulse["facts_rejected"]) == (1, 1)
    assert pulse["tasks_done"] == 1
    assert pulse["tasks_open"] == 2, "the order and the worker's task"
    assert pulse["needs_you"] == {
        "questions": 0,
        "held_facts": 1,
        "approvals": 1,
        "changed_tools": 0,
    }
    assert pulse["needs_you_total"] == 2


def test_pausing_shows_everywhere(db: psycopg.Connection, api: Any, world: World) -> None:
    assert api().post("/pause", json={"on": True}).json() == {"paused": True}
    body = api().get("/screen/snapshot").json()
    assert body["status"]["state"] == "paused" and body["status"]["since"]
    assert {a["name"]: a["state"] for a in body["agents"]}["web-researcher"] == "paused"
    assert api().get("/screen/status").json()["state"] == "paused"


def test_the_chat_lists_orders_with_their_route_and_cost(api: Any, world: World) -> None:
    orders = api().get("/orders").json()

    assert len(orders) == 1
    order = orders[0]
    assert order["text"] == "Find three founders, please"
    assert order["routed"][0]["department"] == "research"
    assert order["cost_usd"] == pytest.approx(0.06)


def test_an_orders_path_is_its_whole_tree(api: Any, world: World) -> None:
    path = api().get(f"/orders/{world.order}/path").json()

    assert [(s["agent"], s["depth"]) for s in path["steps"]] == [
        ("chief-of-staff", 0),
        ("research-lead", 1),
        ("web-researcher", 2),
    ]
    assert [s["status"] for s in path["steps"][1:]] == ["done", "running"]
    assert path["steps"][1]["summary"] == "Found 3"
    assert path["cost_usd"] == pytest.approx(0.06)
    from_a_step = api().get(f"/orders/{world.worker_task}/path").json()
    assert from_a_step["id"] == str(world.order), "a step shows its whole order"
    assert api().get(f"/orders/{uuid.uuid4()}/path").status_code == 404


def test_an_agents_detail(api: Any, world: World) -> None:
    lead = api().get("/agents/research-lead/detail").json()

    assert lead["state"] == "waiting"
    assert lead["last_results"][0]["summary"] == "Found 3"
    assert lead["spent_today_usd"] == pytest.approx(0.02)
    assert api().get("/agents/nobody/detail").status_code == 404


def test_a_facts_detail(api: Any, world: World) -> None:
    fact = api().get(f"/facts/{world.fact}").json()

    assert fact["claim"] == "The Unreal Lab backs founders early"
    assert fact["check"]["gate"] == "brain_claim"
    assert fact["replaces"] == [] and fact["contradicts"] == []
    assert api().get(f"/facts/{uuid.uuid4()}").status_code == 404


def test_replay_leaves_out_the_noise_unless_asked(api: Any, world: World) -> None:
    since = "2000-01-01T00:00:00Z"
    quiet = [e["type"] for e in api().get("/screen/events", params={"since": since}).json()]
    loud = [
        e["type"]
        for e in api().get("/screen/events", params={"since": since, "quiet": False}).json()
    ]

    assert "model_call" not in quiet and "tool_called" in quiet
    assert "model_call" in loud


def test_another_org_sees_nothing(
    db: psycopg.Connection,
    api: Any,
    world: World,
    tenants: Tenants,
) -> None:
    other = api(str(tenants.user_b))
    body = other.get("/screen/snapshot").json()

    assert [a["name"] for a in body["agents"]] == ["spy"]
    assert body["facts"] == [] and body["held"] == []
    assert other.get("/orders").json() == []
    assert other.get(f"/facts/{world.fact}").status_code == 404
    assert other.get(f"/orders/{world.order}/path").status_code == 404
    assert all(
        e["type"] not in ("order_routed", "tool_called")
        for e in other.get("/screen/events", params={"since": "2000-01-01T00:00:00Z"}).json()
    )
