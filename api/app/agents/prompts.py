"""Agent prompts, read from the database.

The text an agent sends to a model is configuration, not code (ADR 007): the
owner edits it and the next run uses it. This module is the only place that
reads or writes `agent_prompts`.

A run resolves its prompts once, on its first invocation, and pins the
versions on the run. A run resumed after a prompt edit therefore finishes on
the prompts it started with, and every run records exactly which text it ran
on. An agent with no active prompt for a slot cannot start: better a run that
pauses than one that guesses.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

import psycopg

from app.db import acting_as, as_service_role


class PromptMissing(LookupError):
    """The agent has no usable prompt for a slot the run needs."""


@dataclass(frozen=True)
class Prompt:
    slot: str
    version: int
    body: str
    note: str | None = None
    active: bool = False


def resolve_for_run(
    connection: psycopg.Connection,
    *,
    run_id: UUID | str,
    agent_id: UUID | str,
    slots: Iterable[str],
    pinned: dict[str, int],
) -> dict[str, Prompt]:
    """The prompts a run uses, pinning them on first call.

    `pinned` is what the run already recorded. Empty means this is its first
    invocation, so the active version of each slot is chosen and recorded.
    """
    slots = tuple(slots)
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select slot, version, body, note, active from public.agent_prompts "
            "where agent_id = %s and slot = any(%s)",
            (str(agent_id), list(slots)),
        )
        # Pinned: the recorded version of each slot. First call: the live one.
        found = {
            row["slot"]: Prompt(**row)
            for row in cursor.fetchall()
            if (row["version"] == pinned.get(row["slot"]) if pinned else row["active"])
        }
        missing = [slot for slot in slots if slot not in found]
        if missing:
            raise PromptMissing(f"Agent {agent_id} has no usable prompt for: {', '.join(missing)}")
        if not pinned:
            cursor.execute(
                "update public.runs set prompt_versions = %s where id = %s",
                (json.dumps({slot: found[slot].version for slot in slots}), str(run_id)),
            )
    return found


def publish(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    agent_id: UUID | str,
    slot: str,
    body: str,
    note: str | None = None,
) -> Prompt:
    """Publish a new version of a prompt and make it live."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select slot, version, body, note, active "
            "from public.publish_agent_prompt(%s, %s, %s, %s)",
            (str(agent_id), slot, body, note),
        )
        return Prompt(**cursor.fetchone())


def activate(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    agent_id: UUID | str,
    slot: str,
    version: int,
) -> Prompt:
    """Make an existing version live: the rollback."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select slot, version, body, note, active "
            "from public.activate_agent_prompt(%s, %s, %s)",
            (str(agent_id), slot, version),
        )
        return Prompt(**cursor.fetchone())


def history(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    agent_id: UUID | str,
    slot: str | None = None,
) -> list[Prompt]:
    """Every version of an agent's prompts, newest first, as the user sees them."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select slot, version, body, note, active from public.agent_prompts "
            "where agent_id = %s and (%s::text is null or slot = %s) "
            "order by slot, version desc",
            (str(agent_id), slot, slot),
        )
        return [Prompt(**row) for row in cursor.fetchall()]
