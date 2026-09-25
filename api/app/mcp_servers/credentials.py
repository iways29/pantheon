"""Credentials for MCP servers, in Supabase Vault (ADR 025).

Only the backend reads or writes them (the SQL functions refuse anyone
else), so every call here runs as service_role. Nothing returned from here
is ever put in a model's context, an event, or an API response.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from app.db import as_service_role


def put(
    connection: psycopg.Connection,
    *,
    org_id: UUID | str,
    server_id: UUID | str,
    kind: str,
    value: str | dict[str, Any],
    state: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    text = value if isinstance(value, str) else json.dumps(value)
    with as_service_role(connection) as conn:
        conn.execute(
            "select public.mcp_put_credential(%s, %s, %s, %s, %s, %s)",
            (str(org_id), str(server_id), kind, text, state, expires_at),
        )


def get(connection: psycopg.Connection, *, server_id: UUID | str, kind: str) -> str | None:
    with as_service_role(connection) as conn:
        row = conn.execute(
            "select public.mcp_get_credential(%s, %s) as value", (str(server_id), kind)
        ).fetchone()
    return row["value"] if row else None


def get_json(
    connection: psycopg.Connection, *, server_id: UUID | str, kind: str
) -> dict[str, Any] | None:
    value = get(connection, server_id=server_id, kind=kind)
    return json.loads(value) if value else None


def drop(connection: psycopg.Connection, *, server_id: UUID | str, kind: str) -> None:
    with as_service_role(connection) as conn:
        conn.execute("select public.mcp_drop_credential(%s, %s)", (str(server_id), kind))


def pending_by_state(connection: psycopg.Connection, state: str) -> dict[str, Any] | None:
    """A sign-in in progress, by the state the callback carries; None if expired."""
    with as_service_role(connection) as conn:
        row = conn.execute(
            "select server_id, org_id from public.mcp_credentials "
            "where kind = 'oauth_pending' and state = %s and expires_at > now()",
            (state,),
        ).fetchone()
    if row is None:
        return None
    value = get_json(connection, server_id=row["server_id"], kind="oauth_pending")
    return {"server_id": row["server_id"], "org_id": row["org_id"], **(value or {})}
