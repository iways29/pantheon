"""Scheduled triggers: fixed tasks an agent does at a fixed time (ADR 008).

A trigger is a row the owner edits; the database's scheduler does the firing.
This module is the only place that reads or writes `triggers`, and it acts as
the signed-in user, so RLS decides what they may touch. Triggers are always
created switched off.
"""

from dataclasses import dataclass
from datetime import time
from typing import Any
from uuid import UUID

import psycopg

from app.db import acting_as

#: Cron's numbering, which is also Postgres's `extract(dow ...)`.
_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
WEEKDAYS = [1, 2, 3, 4, 5]


class TriggerNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Trigger:
    id: UUID
    name: str
    agent_id: UUID
    task: dict[str, Any]
    time_of_day: time
    days_of_week: list[int]
    timezone: str
    enabled: bool
    grace_minutes: int
    max_steps: int
    max_tokens: int
    last_slot: Any = None


_COLUMNS = (
    "id, name, agent_id, task, time_of_day, days_of_week, timezone, enabled, "
    "grace_minutes, max_steps, max_tokens, last_slot"
)


def parse_days(text: str) -> list[int]:
    """Weekdays from "mon-fri", "all", or a list like "mon,wed,fri"."""
    text = text.strip().lower()
    if text in ("all", "daily", "every"):
        return list(range(7))
    if text in ("weekdays", "mon-fri"):
        return list(WEEKDAYS)
    days: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            start, _, end = part.partition("-")
            first, last = _day(start), _day(end)
            if first > last:
                raise ValueError(f"Day range {part!r} runs backwards; write it as two ranges")
            days.update(range(first, last + 1))
        else:
            days.add(_day(part))
    if not days:
        raise ValueError("Give at least one day")
    return sorted(days)


def _day(name: str) -> int:
    try:
        return _DAYS[name.strip()[:3]]
    except KeyError:
        raise ValueError(f"Unknown day {name!r}; use sun, mon, tue, wed, thu, fri, sat") from None


def parse_time(text: str) -> time:
    try:
        return time.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"Time {text!r} must look like 08:30 (24-hour)") from None


def create(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    agent_id: UUID | str,
    name: str,
    task: dict[str, Any],
    time_of_day: time,
    timezone: str,
    days_of_week: list[int] | None = None,
    grace_minutes: int | None = None,
    max_steps: int | None = None,
    max_tokens: int | None = None,
) -> Trigger:
    """Create a trigger. It starts disabled and fires nothing until enabled."""
    columns: dict[str, Any] = {
        "org_id": str(org_id),
        "agent_id": str(agent_id),
        "name": name,
        "task": psycopg.types.json.Jsonb(task),
        "time_of_day": time_of_day,
        "timezone": timezone,
        "days_of_week": days_of_week if days_of_week is not None else WEEKDAYS,
    }
    # Omitted rather than NULL, so the table's defaults apply.
    for column, value in (
        ("grace_minutes", grace_minutes),
        ("max_steps", max_steps),
        ("max_tokens", max_tokens),
    ):
        if value is not None:
            columns[column] = value
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"insert into public.triggers ({', '.join(columns)}) "
            f"values ({', '.join(['%s'] * len(columns))}) returning {_COLUMNS}",
            tuple(columns.values()),
        )
        return Trigger(**cursor.fetchone())


def list_triggers(connection: psycopg.Connection, *, user_id: UUID | str) -> list[Trigger]:
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(f"select {_COLUMNS} from public.triggers order by time_of_day, name")
        return [Trigger(**row) for row in cursor.fetchall()]


def set_enabled(
    connection: psycopg.Connection, *, user_id: UUID | str, name: str, enabled: bool
) -> Trigger:
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"update public.triggers set enabled = %s where name = %s returning {_COLUMNS}",
            (enabled, name),
        )
        row = cursor.fetchone()
    if row is None:
        raise TriggerNotFound(name)
    return Trigger(**row)


def delete(connection: psycopg.Connection, *, user_id: UUID | str, name: str) -> None:
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute("delete from public.triggers where name = %s returning id", (name,))
        if cursor.fetchone() is None:
            raise TriggerNotFound(name)
