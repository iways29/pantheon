"""The autonomy ladder: what each agent may do alone (Step 7.6, ADR 022).

Levels L0 (draft only) to L3 are set on `agents.autonomy_level`, only by the
owner. What a level means for each tool risk class is data
(`public.autonomy_rules`, defaults in `public.tool_mode`); the tool runtime
reads it before every call. R4 is always held.

The system suggests promotions from approval history, per kind of action,
when Jev's recommendation has matched the owner often enough on reversible
actions (right-hand idea 2). It never makes one.
"""

from typing import Any, Literal
from uuid import UUID

import psycopg

from app.agents.admin import AgentNotFound
from app.db import acting_as

Level = Literal["L0", "L1", "L2", "L3"]


def set_level(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    name: str,
    level: Level,
) -> str:
    """The owner sets an agent's level. The agent audit writes the event."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.agents set autonomy_level = %s where org_id = %s and name = %s "
            "returning autonomy_level",
            (level, str(org_id), name),
        )
        row = cursor.fetchone()
    if row is None:
        raise AgentNotFound(f"No agent {name!r}")
    return row["autonomy_level"]


def suggestions(
    connection: psycopg.Connection, *, user_id: UUID | str, eligible_only: bool = True
) -> list[dict[str, Any]]:
    """Promotions the approval history supports, per agent and kind of action."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select agent, action_key, risk_class, recommended, agreed, agreement,
                   current_level, suggested_level, eligible
            from public.autonomy_suggestions
            where eligible or not %s
            order by agent, action_key
            """,
            (eligible_only,),
        )
        return [dict(row) for row in cursor.fetchall()]


def resume_paused_runs(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    reason: str = "kill_switch",
) -> int:
    """Let runs paused by the kill switch (or a budget) carry on, once lifted."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute("select public.resume_paused_runs(%s, %s) as n", (str(org_id), reason))
        return int(cursor.fetchone()["n"])
