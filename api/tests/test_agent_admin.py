"""Step 6: an agent can be created through the API and starts disabled.

The API's connection is the test's own, so every row it writes rolls back
with the test. The owner is org A's member (OWNER_ID is tenants.user_a).
"""

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.agents import prompts
from app.config import Settings, get_settings
from app.db import as_service_role
from app.main import app
from app.owner_api import get_connection
from tests.conftest_db import Tenants
from tests.test_gateway import make_department
from tests.test_health import auth, make_token

SPEC = {
    "department": "marketing",
    "name": "writer",
    "role": "writer",
    "tier": "standard",
    "daily_budget_usd": "0.10",
    "prompts": {"draft": "Write in the studio's voice.", "revise": "Tighten the draft."},
    "allowed_tools": ["search_brain", "save_draft"],
}


@pytest.fixture
def api(db: psycopg.Connection, tenants: Tenants, settings: Settings) -> Iterator[TestClient]:
    make_department(db, tenants.org_a, name="marketing", budget="0.50")

    def same_connection() -> Iterator[psycopg.Connection]:
        yield db

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_connection] = same_connection
    yield TestClient(app, headers=auth(make_token()))
    app.dependency_overrides.clear()


def events(db: psycopg.Connection, type_: str) -> list[dict[str, Any]]:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("select payload from public.events where type = %s", (type_,))
        return cursor.fetchall()


def test_an_agent_created_through_the_api_starts_disabled(
    api: TestClient, db: psycopg.Connection, tenants: Tenants
) -> None:
    response = api.post("/agents", json=SPEC)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["enabled"] is False
    assert (body["department"], body["tier"], body["daily_budget_usd"]) == (
        "marketing",
        "standard",
        "0.1000",
    )
    assert body["allowed_tools"] == ["search_brain", "save_draft"]

    created = events(db, "agent_created")
    assert created[0]["payload"]["name"] == "writer"
    assert created[0]["payload"]["enabled"] is False

    history = prompts.history(db, user_id=tenants.user_a, agent_id=body["id"])
    assert {(p.slot, p.version, p.active) for p in history} == {
        ("draft", 1, True),
        ("revise", 1, True),
    }


def test_sending_the_same_agent_twice_returns_the_same_agent(api: TestClient) -> None:
    first = api.post("/agents", json=SPEC).json()
    again = api.post("/agents", json=SPEC)

    assert again.status_code == 200
    assert again.json()["id"] == first["id"]
    assert again.json()["created"] is False


def test_a_different_agent_under_an_existing_name_is_refused(api: TestClient) -> None:
    api.post("/agents", json=SPEC)

    response = api.post("/agents", json={**SPEC, "tier": "frontier"})

    assert response.status_code == 409


def test_the_owner_enables_it_deliberately_and_it_is_audited(
    api: TestClient, db: psycopg.Connection
) -> None:
    api.post("/agents", json=SPEC)

    on = api.post("/agents/writer/enable")
    off = api.post("/agents/writer/disable")

    assert on.json()["enabled"] is True and off.json()["enabled"] is False
    assert len(events(db, "agent_enabled")) == 1
    assert len(events(db, "agent_disabled")) == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"department": "nowhere"}, 404),
        ({"name": "Bad Name"}, 422),
        ({"prompts": {"draft": "   "}}, 422),
        ({"allowed_tools": ["a", "a"]}, 422),
        ({"tier": "enormous"}, 422),
        ({"parent": "nobody"}, 404),
        ({"surprise": True}, 422),
    ],
)
def test_bad_descriptions_are_refused(api: TestClient, change: dict[str, Any], code: int) -> None:
    assert api.post("/agents", json={**SPEC, **change}).status_code == code


def test_a_worker_can_report_to_a_head(api: TestClient) -> None:
    api.post("/agents", json={**SPEC, "name": "content-lead", "role": "head", "prompts": {}})

    response = api.post("/agents", json={**SPEC, "parent": "content-lead"})

    assert response.status_code == 201


def test_listing_shows_every_agent_in_the_owners_org(api: TestClient) -> None:
    api.post("/agents", json=SPEC)

    listed = api.get("/agents").json()

    assert [a["name"] for a in listed] == ["writer"]


def test_only_the_owner_may_create_agents(api: TestClient) -> None:
    from tests.conftest import OTHER_ID

    response = api.post("/agents", json=SPEC, headers=auth(make_token(OTHER_ID)))

    assert response.status_code == 403
