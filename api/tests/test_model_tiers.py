"""The tier-to-model mapping is data: changed in the database, used on the next call.

ADR 003. MODEL_TIERS remains the default; a row in model_tier_assignments
overrides it for an org, and a department row overrides that for one team.
"""

from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest

from app.db import acting_as, as_service_role
from app.gateway import (
    Gateway,
    OpenRouterCatalogue,
    UnknownModel,
    assign_model,
    clear_assignment,
)
from tests.conftest_db import Tenants
from tests.test_gateway import TIERS, RecordingTransport, make_agent, make_department

CATALOGUE_IDS = frozenset({"vendor/small-model", "vendor/trial-model", "vendor/replacement-model"})


class FixedCatalogue:
    def model_ids(self) -> frozenset[str]:
        return CATALOGUE_IDS


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


def call(db: psycopg.Connection, user: UUID, agent_id: UUID, transport: RecordingTransport) -> str:
    with acting_as(db, user_id=str(user)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
        )
    return transport.calls[-1]["model"]


def assign(db: psycopg.Connection, user: UUID, org: UUID, model: str, **kwargs: Any) -> None:
    with acting_as(db, user_id=str(user)) as connection:
        assign_model(
            connection,
            org_id=org,
            tier=kwargs.pop("tier", "cheap"),
            model=model,
            catalogue=FixedCatalogue(),
            **kwargs,
        )


def clear_assignment_as(db: psycopg.Connection, user: UUID, org: UUID, **kwargs: Any) -> None:
    with acting_as(db, user_id=str(user)) as connection:
        clear_assignment(connection, org_id=org, tier="cheap", **kwargs)


def tier_events(db: psycopg.Connection, user: UUID) -> list[dict[str, Any]]:
    with acting_as(db, user_id=str(user)) as connection, connection.cursor() as cursor:
        cursor.execute("select payload from public.events where type = 'model_tier_changed'")
        return [row["payload"] for row in cursor.fetchall()]


# --- Resolution ---------------------------------------------------------------


def test_without_a_row_the_environment_default_applies(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, tier="cheap")

    assert call(db, tenants.user_a, agent_id, transport) == "vendor/small-model"


def test_an_org_row_overrides_the_environment(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, tier="cheap")
    assign(db, tenants.user_a, tenants.org_a, "vendor/replacement-model")

    assert call(db, tenants.user_a, agent_id, transport) == "vendor/replacement-model"


def test_a_change_applies_to_the_very_next_call(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    """No redeploy and no cache: the next call reads the new row."""
    agent_id = make_agent(db, tenants.org_a, tier="cheap")

    assert call(db, tenants.user_a, agent_id, transport) == "vendor/small-model"
    assign(db, tenants.user_a, tenants.org_a, "vendor/replacement-model")
    assert call(db, tenants.user_a, agent_id, transport) == "vendor/replacement-model"
    clear_assignment_as(db, tenants.user_a, tenants.org_a)
    assert call(db, tenants.user_a, agent_id, transport) == "vendor/small-model"


def test_a_department_override_applies_to_that_department_only(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    trial = make_department(db, tenants.org_a, name="trial")
    other = make_department(db, tenants.org_a, name="other")
    trialling = make_agent(db, tenants.org_a, name="trialling", department_id=trial)
    unaffected = make_agent(db, tenants.org_a, name="unaffected", department_id=other)

    assign(db, tenants.user_a, tenants.org_a, "vendor/replacement-model")
    assign(db, tenants.user_a, tenants.org_a, "vendor/trial-model", department_id=trial)

    assert call(db, tenants.user_a, trialling, transport) == "vendor/trial-model"
    assert call(db, tenants.user_a, unaffected, transport) == "vendor/replacement-model"

    clear_assignment_as(db, tenants.user_a, tenants.org_a, department_id=trial)
    assert call(db, tenants.user_a, trialling, transport) == "vendor/replacement-model"


def test_another_orgs_mapping_is_never_used(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, tier="cheap")
    assign(db, tenants.user_b, tenants.org_b, "vendor/replacement-model")

    assert call(db, tenants.user_a, agent_id, transport) == "vendor/small-model"


def test_the_model_call_event_records_tier_and_requested_model(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, tier="cheap")
    assign(db, tenants.user_a, tenants.org_a, "vendor/replacement-model")
    call(db, tenants.user_a, agent_id, transport)

    with acting_as(db, user_id=str(tenants.user_a)) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select payload from public.events where type = 'model_call' and agent_id = %s",
            (str(agent_id),),
        )
        payload = cursor.fetchone()["payload"]

    assert payload["tier"] == "cheap"
    assert payload["requested_model"] == "vendor/replacement-model"


# --- Audit --------------------------------------------------------------------


def test_every_change_is_written_to_events(db: psycopg.Connection, tenants: Tenants) -> None:
    assign(db, tenants.user_a, tenants.org_a, "vendor/small-model")
    assign(db, tenants.user_a, tenants.org_a, "vendor/small-model")  # no-op: no event
    assign(db, tenants.user_a, tenants.org_a, "vendor/replacement-model")
    clear_assignment_as(db, tenants.user_a, tenants.org_a)

    # Compared unordered: the test runs in one transaction, so every event has
    # the same now() and nothing orders them. Each carries its own from/to.
    events = tier_events(db, tenants.user_a)
    assert sorted((e["operation"], e["from"] or "", e["to"] or "") for e in events) == [
        ("delete", "vendor/replacement-model", ""),
        ("insert", "", "vendor/small-model"),
        ("update", "vendor/small-model", "vendor/replacement-model"),
    ]
    assert all(e["changed_by"] == str(tenants.user_a) for e in events)
    assert all(e["tier"] == "cheap" for e in events)


def test_a_change_made_directly_in_sql_is_audited_too(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    with as_service_role(db) as connection, connection.cursor() as cursor:
        cursor.execute(
            "insert into public.model_tier_assignments (org_id, tier, model) "
            "values (%s, 'frontier', 'vendor/large-model')",
            (str(tenants.org_a),),
        )

    events = tier_events(db, tenants.user_a)
    assert [(e["tier"], e["to"], e["db_role"]) for e in events] == [
        ("frontier", "vendor/large-model", "service_role")
    ]


# --- Validation ---------------------------------------------------------------


def test_a_slug_missing_from_the_catalogue_is_refused(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    with pytest.raises(UnknownModel):
        assign(db, tenants.user_a, tenants.org_a, "vendor/typo-model")

    assert tier_events(db, tenants.user_a) == []


def test_an_unknown_tier_is_refused(db: psycopg.Connection, tenants: Tenants) -> None:
    with pytest.raises(ValueError, match="Unknown tier"):
        assign(db, tenants.user_a, tenants.org_a, "vendor/small-model", tier="premium")


@pytest.mark.parametrize("slug", ["~vendor/family-latest", "", " vendor/small-model"])
def test_the_database_refuses_aliases_and_malformed_slugs(
    db: psycopg.Connection, tenants: Tenants, slug: str
) -> None:
    """The constraint holds even for a write that skips the catalogue check."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with as_service_role(db) as connection, connection.cursor() as cursor:
            cursor.execute(
                "insert into public.model_tier_assignments (org_id, tier, model) "
                "values (%s, 'cheap', %s)",
                (str(tenants.org_a), slug),
            )


# --- Isolation ----------------------------------------------------------------


def test_a_member_cannot_set_another_orgs_mapping(db: psycopg.Connection, tenants: Tenants) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        assign(db, tenants.user_a, tenants.org_b, "vendor/replacement-model")


def test_a_member_cannot_read_another_orgs_mapping(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    assign(db, tenants.user_b, tenants.org_b, "vendor/replacement-model")

    with acting_as(db, user_id=str(tenants.user_a)) as connection, connection.cursor() as cursor:
        cursor.execute("select count(*) as n from public.model_tier_assignments")
        assert cursor.fetchone()["n"] == 0


def test_a_mapping_cannot_name_another_orgs_department(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    theirs = make_department(db, tenants.org_b, name="theirs")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with as_service_role(db) as connection, connection.cursor() as cursor:
            cursor.execute(
                "insert into public.model_tier_assignments (org_id, department_id, tier, model) "
                "values (%s, %s, 'cheap', 'vendor/small-model')",
                (str(tenants.org_a), str(theirs)),
            )


# --- Catalogue ----------------------------------------------------------------


def test_the_catalogue_reads_ids_from_openrouters_model_list() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "a/one"}, {"id": "b/two"}]})

    catalogue = OpenRouterCatalogue(client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert catalogue.model_ids() == {"a/one", "b/two"}
    assert catalogue.model_ids() == {"a/one", "b/two"}
    assert len(requests) == 1, "the list is fetched once per instance"
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/models"
    assert "authorization" not in requests[0].headers
