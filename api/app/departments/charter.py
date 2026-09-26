"""Department charters: what a department is, as data (Step 8.0, ADR 024).

A charter is one JSON document per department, versioned in
`department_charters`. This module is its shape and its storage; `apply.py`
makes the department, its agents and its morning routine match it.
"""

import json
from datetime import time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db import acting_as

_NAME = r"^[a-z][a-z0-9_-]{1,62}$"


class AgentPlan(BaseModel):
    """One agent the department has, and how it starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME)
    role: str = Field(pattern=_NAME)
    tier: Literal["cheap", "standard", "frontier"] = "cheap"
    runner: Literal["pipeline", "deep", "router", "digest"] = "deep"
    allowed_tools: list[str] = Field(default_factory=list)
    #: Starting prompt text by slot. Published as a new version only when it
    #: differs from the live one, so the owner's later edits are not undone
    #: unless the charter itself changes them.
    prompts: dict[str, str] = Field(default_factory=dict)
    #: None: the charter's own level.
    autonomy_level: Literal["L0", "L1", "L2", "L3"] | None = None
    max_children: int | None = Field(default=None, ge=1, le=20)
    daily_budget_usd: Decimal | None = Field(default=None, ge=0)
    #: None: head for the charter's head, worker for the rest. The Executive
    #: Office's head is the Chief of Staff.
    role_type: Literal["chief_of_staff", "head", "worker"] | None = None


class RoutineItem(BaseModel):
    """One fixed piece of the morning routine: a task for one agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    agent: str
    title: str = Field(min_length=3, max_length=200)
    instructions: str = ""
    #: Extra input for the task (sources, topics, limits).
    input: dict[str, Any] = Field(default_factory=dict)
    time: str = Field(description="Local time, 24-hour, e.g. 06:30")
    days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    timezone: str = "America/New_York"
    max_steps: int = Field(default=25, gt=0)
    max_tokens: int = Field(default=50000, gt=0)

    @field_validator("time")
    @classmethod
    def _time(cls, value: str) -> str:
        time.fromisoformat(value)
        return value

    @field_validator("days")
    @classmethod
    def _days(cls, value: list[int]) -> list[int]:
        if not value or any(d not in range(7) for d in value):
            raise ValueError("days are 0 (Sunday) to 6 (Saturday), at least one")
        return sorted(set(value))


class Charter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: str = Field(min_length=10)
    daily_budget_usd: Decimal = Field(ge=0, le=100)
    autonomy_level: Literal["L0", "L1", "L2", "L3"] = "L1"
    data_sensitivity: Literal["normal", "sensitive"] = "normal"
    head: AgentPlan
    workers: list[AgentPlan] = Field(default_factory=list)
    routine: list[RoutineItem] = Field(default_factory=list)
    #: Plain-English rules; the enforced ones live in tools and the ladder.
    approval_rules: list[str] = Field(default_factory=list)
    #: Jev gates the department relies on; each must exist when applied.
    gates: list[str] = Field(default_factory=list)
    #: What "working" means, in plain English.
    metrics: list[str] = Field(default_factory=list)
    #: A charter can be kept as a draft for review; apply refuses a draft.
    draft: bool = False

    @model_validator(mode="after")
    def _check(self) -> "Charter":
        names = [a.name for a in self.agents]
        if len(set(names)) != len(names):
            raise ValueError("agent names repeat")
        known = set(names)
        for item in self.routine:
            if item.agent not in known:
                raise ValueError(f"routine {item.key!r} names {item.agent!r}, not in this charter")
        if len({i.key for i in self.routine}) != len(self.routine):
            raise ValueError("routine keys repeat")
        return self

    @property
    def agents(self) -> list[AgentPlan]:
        return [self.head, *self.workers]


class CharterError(ValueError):
    status = 400


class CharterNotFound(CharterError):
    status = 404


def publish(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    department: str,
    charter: Charter,
    note: str | None = None,
) -> int:
    """Publish a new version and make it live. Returns the version."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select version from public.publish_department_charter(%s, %s, %s, %s)",
            (str(org_id), department, charter.model_dump_json(), note),
        )
        return int(cursor.fetchone()["version"])


def load(
    connection: psycopg.Connection, *, org_id: UUID | str, department: str
) -> tuple[int, Charter]:
    """The live charter and its version."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select version, charter from public.department_charters "
            "where org_id = %s and department = %s and active",
            (str(org_id), department),
        )
        row = cursor.fetchone()
    if row is None:
        raise CharterNotFound(f"No charter for {department!r}")
    return int(row["version"]), Charter.model_validate(row["charter"])


def seed_charters(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    charters: dict[str, Charter],
) -> list[str]:
    """Publish starter charters a department has never had. Existing ones stay."""
    seeded = []
    for department, charter in charters.items():
        with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
            cursor.execute(
                "select 1 from public.department_charters where org_id = %s and department = %s",
                (str(org_id), department),
            )
            if cursor.fetchone() is not None:
                continue
        publish(
            connection,
            user_id=user_id,
            org_id=org_id,
            department=department,
            charter=charter,
            note="Starting charter",
        )
        seeded.append(department)
    return seeded


def to_json(charter: Charter) -> str:
    return json.dumps(json.loads(charter.model_dump_json()), indent=2, ensure_ascii=False)
