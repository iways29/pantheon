"""Creating and switching agents from data (Step 6, ADR 014).

An agent is a row, its prompts are rows (ADR 007), and its model is a tier
the gateway resolves (ADR 003), so a new agent needs no code change: the
owner sends a structured description and it exists. It always starts
switched off; the owner reviews it and enables it deliberately.

Creation is idempotent on the agent's name within the org: sending the same
description twice returns the same agent, and a different description under
an existing name is refused rather than silently changing a live agent.
Every change is written to `events` by a trigger on `agents`.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db import acting_as

_NAME = r"^[a-z][a-z0-9_-]{1,62}$"
_SLOT = r"^[a-z][a-z0-9_]*$"


class AgentSpec(BaseModel):
    """What the owner sends to create an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    department: str = Field(min_length=1, description="The department's name")
    name: str = Field(pattern=_NAME)
    role: str = Field(pattern=_NAME, description="What the agent's code runs as, e.g. research")
    tier: Literal["cheap", "standard", "frontier"] = "cheap"
    #: Optional cap inside the department's budget. None: the department's
    #: budget alone governs (ADR 002).
    daily_budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=4)
    #: Starting prompt text by slot, e.g. {"answer": "...", "extract": "..."}.
    prompts: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    #: The department head this agent reports to, by name.
    parent: str | None = None
    #: Step 7.4 (ADR 020): who may delegate, which code runs it, how much it
    #: may do alone, and an optional tighter cap on its sub-tasks.
    role_type: Literal["chief_of_staff", "head", "worker"] = "worker"
    runner: Literal["pipeline", "deep", "router", "digest"] = "pipeline"
    autonomy_level: Literal["L0", "L1", "L2", "L3"] = "L1"
    max_children: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def _check(self) -> "AgentSpec":
        for slot, text in self.prompts.items():
            if not re.match(_SLOT, slot):
                raise ValueError(f"prompt slot {slot!r} must be lower_snake_case")
            if not text.strip():
                raise ValueError(f"prompt {slot!r} is empty")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools has duplicates")
        return self


@dataclass(frozen=True)
class AgentSummary:
    id: UUID
    name: str
    role: str
    department: str
    tier: str
    daily_budget_usd: Decimal | None
    allowed_tools: tuple[str, ...]
    enabled: bool
    role_type: str = "worker"
    runner: str = "pipeline"
    autonomy_level: str = "L1"
    max_children: int | None = None
    #: False when an identical agent already existed and was returned.
    created: bool = True


class AgentAdminError(ValueError):
    status = 400


class DepartmentNotFound(AgentAdminError):
    status = 404


class AgentNotFound(AgentAdminError):
    status = 404


class AgentConflict(AgentAdminError):
    """An agent with this name exists and differs from the description."""

    status = 409


def create_agent(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    spec: AgentSpec,
) -> AgentSummary:
    """Create an agent, switched off, with its starting prompts."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select id from public.departments where org_id = %s and name = %s",
            (str(org_id), spec.department),
        )
        department = cursor.fetchone()
        if department is None:
            raise DepartmentNotFound(f"No department {spec.department!r}")
        parent_id = None
        if spec.parent is not None:
            cursor.execute(
                "select id from public.agents where org_id = %s and name = %s",
                (str(org_id), spec.parent),
            )
            parent = cursor.fetchone()
            if parent is None:
                raise AgentNotFound(f"No agent {spec.parent!r} to report to")
            parent_id = parent["id"]

        existing = _summary(cursor, org_id, spec.name)
        if existing is not None:
            if _same(cursor, existing, spec, parent_id):
                return AgentSummary(**{**existing.__dict__, "created": False})
            raise AgentConflict(
                f"An agent named {spec.name!r} already exists with a different description"
            )

        cursor.execute(
            """
            insert into public.agents
                (org_id, department_id, parent_agent_id, name, role, model_tier,
                 daily_budget_usd, allowed_tools, role_type, runner, autonomy_level,
                 max_children, enabled)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)
            returning id
            """,
            (
                str(org_id),
                str(department["id"]),
                str(parent_id) if parent_id else None,
                spec.name,
                spec.role,
                spec.tier,
                spec.daily_budget_usd,
                list(spec.allowed_tools),
                spec.role_type,
                spec.runner,
                spec.autonomy_level,
                spec.max_children,
            ),
        )
        agent_id = cursor.fetchone()["id"]
        for slot, text in sorted(spec.prompts.items()):
            cursor.execute(
                "select 1 from public.publish_agent_prompt(%s, %s, %s, 'Starting prompt')",
                (str(agent_id), slot, text),
            )
        created = _summary(cursor, org_id, spec.name)
    assert created is not None
    return created


def set_enabled(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    name: str,
    enabled: bool,
) -> AgentSummary:
    """Switch an agent on or off. The trigger writes the event."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.agents set enabled = %s where org_id = %s and name = %s",
            (enabled, str(org_id), name),
        )
        summary = _summary(cursor, org_id, name)
    if summary is None:
        raise AgentNotFound(f"No agent {name!r}")
    return summary


def list_agents(
    connection: psycopg.Connection, *, user_id: UUID | str, org_id: UUID | str
) -> list[AgentSummary]:
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select name from public.agents where org_id = %s order by name", (str(org_id),)
        )
        names = [row["name"] for row in cursor.fetchall()]
        return [s for name in names if (s := _summary(cursor, org_id, name)) is not None]


def _summary(cursor: psycopg.Cursor, org_id: UUID | str, name: str) -> AgentSummary | None:
    cursor.execute(
        """
        select a.id, a.name, a.role, d.name as department, a.model_tier as tier,
               a.daily_budget_usd, a.allowed_tools, a.enabled, a.role_type, a.runner,
               a.autonomy_level, a.max_children
        from public.agents a join public.departments d on d.id = a.department_id
        where a.org_id = %s and a.name = %s
        """,
        (str(org_id), name),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return AgentSummary(
        id=row["id"],
        name=row["name"],
        role=row["role"],
        department=row["department"],
        tier=row["tier"],
        daily_budget_usd=row["daily_budget_usd"],
        allowed_tools=tuple(row["allowed_tools"]),
        enabled=row["enabled"],
        role_type=row["role_type"],
        runner=row["runner"],
        autonomy_level=row["autonomy_level"],
        max_children=row["max_children"],
    )


def _same(
    cursor: psycopg.Cursor, existing: AgentSummary, spec: AgentSpec, parent_id: UUID | None
) -> bool:
    cursor.execute("select parent_agent_id from public.agents where id = %s", (str(existing.id),))
    if cursor.fetchone()["parent_agent_id"] != parent_id:
        return False
    budget = None if spec.daily_budget_usd is None else Decimal(spec.daily_budget_usd)
    if (
        existing.department,
        existing.role,
        existing.tier,
        existing.daily_budget_usd,
        existing.allowed_tools,
        existing.role_type,
        existing.runner,
        existing.autonomy_level,
        existing.max_children,
    ) != (
        spec.department,
        spec.role,
        spec.tier,
        budget,
        tuple(spec.allowed_tools),
        spec.role_type,
        spec.runner,
        spec.autonomy_level,
        spec.max_children,
    ):
        return False
    cursor.execute(
        "select slot, body from public.agent_prompts where agent_id = %s and version = 1",
        (str(existing.id),),
    )
    return {r["slot"]: r["body"] for r in cursor.fetchall()} == spec.prompts
