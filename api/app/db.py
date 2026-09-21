"""Database connections, with row-level security actually in force.

RLS is only worth anything if the connection is subject to it. A superuser --
or any role with BYPASSRLS -- sees every organisation's rows regardless of
policy, so connecting as one would make the isolation silently decorative.

Supabase switches roles per request and exposes the verified JWT to SQL as the
`request.jwt.claims` setting, which policies read through `auth.uid()`. This
module does the same thing over a direct connection.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

# Roles a caller may act as. Interpolated into SQL because SET ROLE takes an
# identifier rather than a parameter, so the value must never come from input.
_ALLOWED_ROLES = frozenset({"authenticated", "service_role", "anon"})


def connect(dsn: str) -> psycopg.Connection:
    # prepare_threshold=None turns off psycopg's automatic prepared statements.
    # Supabase's transaction pooler, the only endpoint Vercel can reach over
    # IPv4, does not support them (ADR 005); a direct connection loses nothing.
    connection = psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None)
    register_vector(connection)
    return connection


@contextmanager
def acting_as(
    connection: psycopg.Connection,
    *,
    user_id: str | None,
    role: str = "authenticated",
) -> Iterator[psycopg.Connection]:
    """Run a transaction as `role`, with `user_id` as the RLS identity.

    Both settings are LOCAL, so they unwind with the transaction and cannot
    leak into the next caller to borrow this connection from a pool. Passing
    `user_id=None` models an unauthenticated caller, which every policy should
    reject.
    """
    if role not in _ALLOWED_ROLES:
        raise ValueError(f"Unknown role: {role!r}")

    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(f"set local role {role}")
            claims = json.dumps({"sub": user_id, "role": role}) if user_id else ""
            cursor.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
        yield connection


@contextmanager
def as_service_role(connection: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Run a transaction with RLS bypassed.

    For work with no authenticated caller behind it: migrations, scheduled
    triggers, backfills. Never for handling a user request, where the policies
    are the point.
    """
    with acting_as(connection, user_id=None, role="service_role") as conn:
        yield conn
