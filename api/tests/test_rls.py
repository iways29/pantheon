"""Row-level security holds between organisations.

This is the test CLAUDE.md asks for by name. It runs against a real database
as a non-superuser, because a superuser bypasses RLS and would make every
assertion here pass without proving anything.
"""

import psycopg
import pytest

from app.brain import Brain, HashingEmbedder
from app.db import acting_as, as_service_role
from tests.conftest_db import Tenants


def seed_fact(db: psycopg.Connection, org_id, claim: str) -> None:
    with as_service_role(db) as connection:
        Brain(connection, HashingEmbedder()).insert_fact(org_id=org_id, claim=claim)


def test_connection_is_subject_to_rls(db: psycopg.Connection) -> None:
    """Guard the guard: if this role could bypass RLS, the suite proves nothing."""
    with acting_as(db, user_id=None) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select current_user, rolbypassrls from pg_roles where rolname = current_user"
        )
        row = cursor.fetchone()

    assert row is not None
    assert row["rolbypassrls"] is False, "RLS tests are meaningless as a BYPASSRLS role"


def test_each_org_sees_only_its_own_facts(db: psycopg.Connection, tenants: Tenants) -> None:
    seed_fact(db, tenants.org_a, "Org A keeps its invoices in Stripe")
    seed_fact(db, tenants.org_b, "Org B keeps its invoices in Xero")

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        a_sees = [
            match.fact.claim for match in Brain(connection, HashingEmbedder()).search("invoices")
        ]

    with acting_as(db, user_id=str(tenants.user_b)) as connection:
        b_sees = [
            match.fact.claim for match in Brain(connection, HashingEmbedder()).search("invoices")
        ]

    assert a_sees == ["Org A keeps its invoices in Stripe"]
    assert b_sees == ["Org B keeps its invoices in Xero"]


def test_a_user_in_no_org_sees_nothing(db: psycopg.Connection, tenants: Tenants) -> None:
    seed_fact(db, tenants.org_a, "Org A keeps its invoices in Stripe")

    with acting_as(db, user_id=str(tenants.outsider)) as connection:
        assert Brain(connection, HashingEmbedder()).search("invoices") == []


def test_an_unauthenticated_caller_sees_nothing(db: psycopg.Connection, tenants: Tenants) -> None:
    seed_fact(db, tenants.org_a, "Org A keeps its invoices in Stripe")

    with acting_as(db, user_id=None) as connection:
        assert Brain(connection, HashingEmbedder()).search("invoices") == []


def test_a_member_cannot_write_into_another_org(db: psycopg.Connection, tenants: Tenants) -> None:
    """Reading is not the only boundary: the insert policy must hold too."""
    # pytest.raises wraps the transaction rather than sitting inside it: an
    # error caught within the block would leave the savepoint unwound and the
    # connection in a failed state for everything after.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Brain(connection, HashingEmbedder()).insert_fact(
                org_id=tenants.org_b, claim="Planted by org A"
            )


def test_a_member_cannot_read_another_orgs_fact_by_id(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    """A known id is not a way around the policy."""
    with as_service_role(db) as connection:
        planted = Brain(connection, HashingEmbedder()).insert_fact(
            org_id=tenants.org_b, claim="Org B's private roadmap"
        )

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        assert Brain(connection, HashingEmbedder()).get(planted.id) is None

    with acting_as(db, user_id=str(tenants.user_b)) as connection:
        found = Brain(connection, HashingEmbedder()).get(planted.id)
        assert found is not None
        assert found.claim == "Org B's private roadmap"
