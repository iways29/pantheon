"""Tasks: durable work orders, split by heads, done by workers (ADR 019).

The database holds the rules (limits, lifecycle, wake-ups; see migration
20260926060000). This module is the way in from Python: the owner's orders,
an agent's `create_task` and `report_result` tools, and reading a tree.
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from app.db import acting_as


class TaskError(ValueError):
    status = 400


class TaskRefused(TaskError):
    """The database refused it: a limit, a role, or a budget."""

    status = 409


@dataclass(frozen=True)
class Task:
    id: UUID
    title: str
    status: str
    depth: int
    assigned_agent_id: UUID
    parent_task_id: UUID | None
    result: Any = None
    created: bool = True


def order(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    agent: str,
    title: str,
    instructions: str = "",
    input: dict[str, Any] | None = None,
    max_cost_usd: Decimal | None = None,
    idempotency_key: str | None = None,
) -> Task:
    """The owner gives an order: a root task for one agent.

    A repeated order is one task: the key defaults to a hash of who, what and
    for whom, so sending the same order twice returns the first.
    """
    key = idempotency_key or "order:" + _digest(agent, title, instructions, input or {})
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select id from public.agents where org_id = %s and name = %s", (str(org_id), agent)
        )
        row = cursor.fetchone()
        if row is None:
            raise TaskError(f"No agent {agent!r}")
        return _insert(
            cursor,
            org_id=org_id,
            parent=None,
            agent_id=row["id"],
            created_by="owner",
            created_by_agent_id=None,
            title=title,
            instructions=instructions,
            input=input or {},
            max_cost_usd=max_cost_usd,
            key=key,
            requested_by=user_id,
        )


def delegate(
    cursor: psycopg.Cursor,
    *,
    org_id: UUID | str,
    parent_task_id: UUID | str,
    by_agent_id: UUID | str,
    to_agent: str,
    title: str,
    instructions: str,
    input: dict[str, Any] | None = None,
    max_cost_usd: Decimal | None = None,
    idempotency_key: str | None = None,
) -> Task:
    """An agent splits the task it is working on. The database enforces who
    may, how deep, how many, and within what budget."""
    cursor.execute(
        "select id from public.agents where org_id = %s and name = %s", (str(org_id), to_agent)
    )
    row = cursor.fetchone()
    if row is None:
        raise TaskError(f"No agent {to_agent!r}")
    cursor.execute("select requested_by from public.tasks where id = %s", (str(parent_task_id),))
    parent = cursor.fetchone()
    if parent is None:
        raise TaskError(f"No task {parent_task_id}")
    key = idempotency_key or f"task:{parent_task_id}:" + _digest(to_agent, title, instructions)
    return _insert(
        cursor,
        org_id=org_id,
        parent=parent_task_id,
        agent_id=row["id"],
        created_by=f"agent:{by_agent_id}",
        created_by_agent_id=by_agent_id,
        title=title,
        instructions=instructions,
        input=input or {},
        max_cost_usd=max_cost_usd,
        key=key,
        requested_by=parent["requested_by"],
    )


def report_result(
    cursor: psycopg.Cursor, *, task_id: UUID | str, agent_id: UUID | str, result: dict[str, Any]
) -> None:
    """A worker records its short structured result on its own task."""
    cursor.execute(
        "update public.tasks set result = %s where id = %s and assigned_agent_id = %s "
        "and status in ('running', 'queued') returning id",
        (json.dumps(result), str(task_id), str(agent_id)),
    )
    if cursor.fetchone() is None:
        raise TaskError("Only the agent working on a running task may report its result")


def children(cursor: psycopg.Cursor, task_id: UUID | str) -> list[dict[str, Any]]:
    """What a head's workers produced, for its next run."""
    cursor.execute(
        """
        select t.id, t.title, t.status, t.result, t.error, a.name as agent
        from public.tasks t join public.agents a on a.id = t.assigned_agent_id
        where t.parent_task_id = %s order by t.created_at
        """,
        (str(task_id),),
    )
    return [dict(row) for row in cursor.fetchall()]


def tree(
    connection: psycopg.Connection, *, user_id: UUID | str, root_task_id: UUID | str
) -> list[dict[str, Any]]:
    """Every task under a root with its own and subtree cost."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select t.id, t.parent_task_id, t.depth, t.title, t.status, a.name as agent,
                   c.own_cost_usd, c.tree_cost_usd, t.result
            from public.tasks t
            join public.agents a on a.id = t.assigned_agent_id
            join public.task_costs c on c.task_id = t.id
            where t.root_task_id = %s
            order by t.depth, t.created_at
            """,
            (str(root_task_id),),
        )
        return [dict(row) for row in cursor.fetchall()]


def cancel(connection: psycopg.Connection, *, user_id: UUID | str, task_id: UUID | str) -> int:
    """Cancel a task and everything under it that has not finished."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            with recursive sub as (
              select id from public.tasks where id = %s
              union all
              select t.id from public.tasks t join sub on t.parent_task_id = sub.id
            )
            update public.tasks set status = 'cancelled', finished_at = now()
             where id in (select id from sub)
               and status not in ('done', 'failed', 'cancelled')
            returning id
            """,
            (str(task_id),),
        )
        return len(cursor.fetchall())


def _insert(
    cursor: psycopg.Cursor,
    *,
    org_id: UUID | str,
    parent: UUID | str | None,
    agent_id: UUID | str,
    created_by: str,
    created_by_agent_id: UUID | str | None,
    title: str,
    instructions: str,
    input: dict[str, Any],
    max_cost_usd: Decimal | None,
    key: str,
    requested_by: UUID | str,
) -> Task:
    try:
        cursor.execute("savepoint task_insert")
        cursor.execute(
            """
            insert into public.tasks
                (org_id, parent_task_id, assigned_agent_id, created_by, created_by_agent_id,
                 title, instructions, input, max_cost_usd, idempotency_key, requested_by)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (org_id, idempotency_key) do nothing
            returning id, title, status, depth, assigned_agent_id, parent_task_id, result
            """,
            (
                str(org_id),
                str(parent) if parent else None,
                str(agent_id),
                created_by,
                str(created_by_agent_id) if created_by_agent_id else None,
                title,
                instructions,
                json.dumps(input),
                max_cost_usd,
                key,
                str(requested_by),
            ),
        )
        row = cursor.fetchone()
        cursor.execute("release savepoint task_insert")
    except (psycopg.errors.CheckViolation, psycopg.errors.InsufficientPrivilege) as error:
        cursor.execute("rollback to savepoint task_insert")
        raise TaskRefused(str(error).splitlines()[0]) from error
    if row is not None:
        return Task(**row)
    cursor.execute(
        "select id, title, status, depth, assigned_agent_id, parent_task_id, result "
        "from public.tasks where org_id = %s and idempotency_key = %s",
        (str(org_id), key),
    )
    return Task(**cursor.fetchone(), created=False)


def _digest(*parts: object) -> str:
    body = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:24]
