"""Fixtures for tests that need a real database.

These run against PostgreSQL with pgvector, not a stub. Row-level security is
the thing under test, and no fake reproduces it.
"""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest

from app.db import as_service_role, connect

DEFAULT_DSN = "postgresql://pantheon:pantheon@127.0.0.1:5432/pantheon_dev"

ORG_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
ORG_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
OUTSIDER = uuid.UUID("33333333-3333-3333-3333-333333333333")


@dataclass(frozen=True)
class Tenants:
    org_a: uuid.UUID = ORG_A
    org_b: uuid.UUID = ORG_B
    user_a: uuid.UUID = USER_A
    user_b: uuid.UUID = USER_B
    outsider: uuid.UUID = OUTSIDER


@pytest.fixture(scope="session")
def dsn() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DSN)


@pytest.fixture
def db(dsn: str) -> Iterator[psycopg.Connection]:
    """A connection whose work is rolled back when the test ends.

    Everything happens inside one transaction that is never committed, so
    tests leave no residue and need no truncation between runs.
    """
    try:
        connection = connect(dsn)
    except psycopg.OperationalError as error:
        # Skipping locally is a convenience. In CI it would hide the fact that
        # the RLS guarantee went unverified, so there it is a failure.
        if os.getenv("REQUIRE_DATABASE") == "1":
            pytest.fail(f"REQUIRE_DATABASE is set but no database at {dsn}: {error}")
        pytest.skip(f"No database at {dsn}: {error}")

    try:
        # An explicit outermost transaction, ended with psycopg.Rollback.
        # Without it, the nested `transaction()` blocks inside app.db would
        # each commit as they exit, and seeded rows would survive the test.
        with connection.transaction():
            yield connection
            raise psycopg.Rollback
    finally:
        connection.close()


@pytest.fixture
def tenants(db: psycopg.Connection) -> Tenants:
    """Two organisations that must never see each other's rows."""
    with as_service_role(db) as connection, connection.cursor() as cursor:
        cursor.executemany(
            "insert into auth.users (id, email) values (%s, %s)",
            [
                (str(USER_A), "a@example.com"),
                (str(USER_B), "b@example.com"),
                (str(OUTSIDER), "outsider@example.com"),
            ],
        )
        cursor.executemany(
            "insert into public.orgs (id, name) values (%s, %s)",
            [(str(ORG_A), "Org A"), (str(ORG_B), "Org B")],
        )
        cursor.executemany(
            "insert into public.org_members (org_id, user_id) values (%s, %s)",
            [(str(ORG_A), str(USER_A)), (str(ORG_B), str(USER_B))],
        )

    return Tenants()
