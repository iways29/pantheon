"""Agent prompts are versioned data with an audit trail (ADR 007).

Every published version is history: it is never edited or deleted, one version
per slot is live, and each change of which one is live is written to events.
"""

import uuid
from typing import Any

import psycopg
import pytest

from app.agents import prompts
from app.db import acting_as, as_service_role
from tests.conftest_db import Tenants
from tests.test_gateway import make_agent


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants) -> uuid.UUID:
    return make_agent(db, tenants.org_a, name="researcher")


def publish(db: psycopg.Connection, user: uuid.UUID, agent_id: uuid.UUID, body: str) -> Any:
    return prompts.publish(db, user_id=user, agent_id=agent_id, slot="answer", body=body)


def test_publishing_creates_the_next_version_and_makes_it_the_only_live_one(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    first = publish(db, tenants.user_a, agent, "one")
    second = publish(db, tenants.user_a, agent, "two")

    assert (first.version, second.version) == (1, 2)
    history = prompts.history(db, user_id=tenants.user_a, agent_id=agent)
    assert [(p.version, p.active) for p in history] == [(2, True), (1, False)]


def test_slots_are_versioned_independently(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    publish(db, tenants.user_a, agent, "answer text")
    other = prompts.publish(
        db, user_id=tenants.user_a, agent_id=agent, slot="extract", body="extract text"
    )
    assert other.version == 1


def test_rolling_back_reactivates_an_old_version_without_losing_the_new_one(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    publish(db, tenants.user_a, agent, "one")
    publish(db, tenants.user_a, agent, "two")

    back = prompts.activate(db, user_id=tenants.user_a, agent_id=agent, slot="answer", version=1)

    assert back.body == "one"
    history = prompts.history(db, user_id=tenants.user_a, agent_id=agent)
    assert [(p.version, p.active) for p in history] == [(2, False), (1, True)]


def test_activating_a_version_that_does_not_exist_is_refused(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    publish(db, tenants.user_a, agent, "one")
    with pytest.raises(psycopg.errors.NoDataFound):
        prompts.activate(db, user_id=tenants.user_a, agent_id=agent, slot="answer", version=9)


@pytest.mark.parametrize("role", ["authenticated", "service_role"])
def test_the_app_roles_may_only_flip_the_active_flag(
    db: psycopg.Connection, tenants: Tenants, agent: uuid.UUID, role: str
) -> None:
    publish(db, tenants.user_a, agent, "original")
    with acting_as(db, user_id=str(tenants.user_a), role=role) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("update public.agent_prompts set body = 'rewritten'")


def test_history_is_frozen_by_a_trigger_that_binds_the_table_owner_too(
    db: psycopg.Connection,
) -> None:
    """Grants stop the app roles; the trigger stops a hand edit in the SQL
    editor, which runs as the owner. The test role is not the owner, so this
    checks the trigger is installed and live rather than firing it."""
    with as_service_role(db) as conn:
        row = conn.execute(
            "select tgenabled from pg_trigger where tgname = 'agent_prompts_freeze'"
        ).fetchone()
    assert row is not None and row["tgenabled"] == "O"


def test_a_published_version_cannot_be_deleted(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    publish(db, tenants.user_a, agent, "original")
    with (
        acting_as(db, user_id=str(tenants.user_a)) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        with conn.transaction():
            conn.execute("delete from public.agent_prompts")


def test_two_live_versions_of_one_slot_are_impossible(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    publish(db, tenants.user_a, agent, "one")
    with as_service_role(db) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            conn.execute(
                "insert into public.agent_prompts (org_id, agent_id, slot, version, body, active) "
                "values (%s, %s, 'answer', 2, 'sneaky', true)",
                (str(tenants.org_a), str(agent)),
            )


def test_a_blank_prompt_is_refused(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        publish(db, tenants.user_a, agent, "   ")


def test_every_change_of_the_live_version_is_written_to_events(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    publish(db, tenants.user_a, agent, "one")
    publish(db, tenants.user_a, agent, "two")
    prompts.activate(db, user_id=tenants.user_a, agent_id=agent, slot="answer", version=1)

    with as_service_role(db) as conn:
        events = conn.execute(
            "select type, payload from public.events where agent_id = %s "
            "and type like 'agent_prompt_%%' order by created_at, id",
            (str(agent),),
        ).fetchall()

    # Events written in one transaction share a timestamp, so compare the set
    # of changes rather than their order.
    assert sorted((e["type"], e["payload"]["version"]) for e in events) == [
        ("agent_prompt_activated", 1),
        ("agent_prompt_activated", 1),
        ("agent_prompt_activated", 2),
        ("agent_prompt_deactivated", 1),
        ("agent_prompt_deactivated", 2),
    ]
    assert {e["payload"]["changed_by"] for e in events} == {str(tenants.user_a)}


def test_a_member_of_another_org_can_neither_read_nor_publish(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: uuid.UUID,
) -> None:
    publish(db, tenants.user_a, agent, "secret instructions")

    assert prompts.history(db, user_id=tenants.user_b, agent_id=agent) == []
    with pytest.raises(psycopg.errors.NoDataFound):
        publish(db, tenants.user_b, agent, "hijack")
