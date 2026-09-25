"""What tools exist: implemented in code, configured in the database.

Each `ToolSpec` is a typed function an agent may call. Its starting
description, risk class and approval policy are seed data for the `tools`
table (`seed_tools`), which the owner then edits; the runtime reads the
table, never these defaults, when deciding whether a call may run.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
from pydantic import BaseModel

from app.db import acting_as

if TYPE_CHECKING:
    from app.tools.runtime import ToolContext

Handler = Callable[["ToolContext", Any], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    risk_class: str
    handler: Handler
    #: Changes something (internal or outside). Such calls need an
    #: idempotency key and act at most once per key.
    side_effect: bool = False
    approval: str = "auto"
    timeout_seconds: int = 30
    max_output_chars: int = 8000
    #: Starting knobs and daily cap, seeded into the tool's row; the owner
    #: changes them there (ADR 026).
    settings: dict[str, Any] | None = None
    max_calls_per_day: int | None = None


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.risk_class not in ("R0", "R1", "R2", "R3", "R4"):
        raise ValueError(f"{spec.name}: risk class {spec.risk_class} cannot be built")
    REGISTRY[spec.name] = spec
    return spec


def seed_tools(
    connection: psycopg.Connection, *, user_id: UUID | str, org_id: UUID | str
) -> list[str]:
    """Add a `tools` row for every registered tool the org does not have yet.

    Existing rows are left alone: the owner may have changed them.
    """
    import app.tools.builtin  # noqa: F401 - registers the built-in tools

    seeded = []
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        for spec in REGISTRY.values():
            cursor.execute(
                """
                insert into public.tools
                    (org_id, name, description, risk_class, approval, timeout_seconds,
                     max_output_chars, settings, max_calls_per_day)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (org_id, name) do nothing
                returning name
                """,
                (
                    str(org_id),
                    spec.name,
                    spec.description,
                    spec.risk_class,
                    "approval" if spec.risk_class == "R4" else spec.approval,
                    spec.timeout_seconds,
                    spec.max_output_chars,
                    json.dumps(spec.settings or {}),
                    spec.max_calls_per_day,
                ),
            )
            if cursor.fetchone():
                seeded.append(spec.name)
    return seeded
