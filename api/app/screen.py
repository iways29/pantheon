"""What the brain screen reads (Step 10, ADR 036).

Read-only endpoints for the owner's web app. Each runs as the owner, so RLS
decides what is visible. The live part is not here: the screen subscribes to
`events` through Supabase Realtime and uses these reads for the picture at
load time, the detail panels and replay.

"Today" is the budget day (from midnight UTC), the same day the gateway
enforces budgets on, so spend and budget always agree.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, HTTPException, Query, status

from app.auth import OwnerPrincipal
from app.brain import view
from app.db import acting_as
from app.owner_api import Connection, owner_org

router = APIRouter(tags=["screen"])

#: Events the screen does not animate: too many, and each says little alone.
#: Spend still reaches the screen through the pulse.
QUIET = ("model_call", "judgment_made", "run_step", "run_invoked")
#: A paused run carries on by itself for these reasons, so its agent is working.
CARRIES_ON = ("deadline", "upstream_error", "approved", "redirected")
DAY_START = "date_trunc('day', now() at time zone 'utc') at time zone 'utc'"


def _day(cursor: psycopg.Cursor) -> datetime:
    cursor.execute(f"select {DAY_START} as day")
    return cursor.fetchone()["day"]


def _money(value: Any) -> float:  # noqa: ANN401 - Decimal or None
    return round(float(value or 0), 6)


# --- Status and pulse ---------------------------------------------------------------


def pause_state(cursor: psycopg.Cursor) -> dict[str, Any]:
    cursor.execute("select value from public.system_flags where key = 'kill_switch'")
    row = cursor.fetchone()
    paused = bool(row and row["value"] is True)
    cursor.execute(
        "select type, created_at from public.events where type in ('paused', 'unpaused', 'killed') "
        "order by created_at desc limit 20"
    )
    rows = cursor.fetchall()
    since = next((r["created_at"] for r in rows if r["type"] in ("paused", "unpaused")), None)
    killed = next((r["created_at"] for r in rows if r["type"] == "killed"), None)
    return {
        "state": "paused" if paused else "running",
        "since": since.isoformat() if since else None,
        "last_kill_at": killed.isoformat() if killed else None,
    }


def pulse(cursor: psycopg.Cursor) -> dict[str, Any]:
    day = _day(cursor)
    cursor.execute(
        "select coalesce(sum(cost_usd), 0) as spent from public.model_calls where created_at >= %s",
        (day,),
    )
    spent = cursor.fetchone()["spent"]
    cursor.execute(
        "select coalesce(sum(daily_budget_usd), 0) as budget from public.departments where enabled"
    )
    budget = cursor.fetchone()["budget"]
    cursor.execute(
        """
        select count(*) filter (
                 where payload ->> 'outcome' in ('accepted', 'superseded', 'disputed')) as added,
               count(*) filter (where payload ->> 'outcome' = 'rejected') as rejected
          from public.events where type = 'fact_write_decided' and created_at >= %s
        """,
        (day,),
    )
    facts = cursor.fetchone()
    cursor.execute(
        "select count(*) filter (where status = 'done') as done, "
        "count(*) filter (where status = 'failed') as failed "
        "from public.tasks where finished_at >= %s",
        (day,),
    )
    tasks = cursor.fetchone()
    # Unfinished, whatever the day: what a kill would cancel.
    cursor.execute(
        "select count(*) as n from public.tasks "
        "where status in ('queued', 'running', 'blocked', 'awaiting_approval')"
    )
    tasks_open = cursor.fetchone()["n"]
    cursor.execute(
        """
        select count(*) filter (where action_type = 'route_order') as questions,
               count(*) filter (where action_type = 'fact_write') as held_facts,
               count(*) filter (where action_type not in ('route_order', 'fact_write'))
                 as approvals
          from public.approvals where status = 'pending'
        """
    )
    waiting = cursor.fetchone()
    cursor.execute(
        "select count(*) as n from public.tools where source = 'mcp' "
        "and approved_sha is not null and approved_sha <> definition_sha"
    )
    changed = cursor.fetchone()["n"]
    needs = dict(waiting) | {"changed_tools": changed}
    return {
        "day_starts_at": day.isoformat(),
        "spend_usd": _money(spent),
        "budget_usd": _money(budget),
        "facts_added": facts["added"],
        "facts_rejected": facts["rejected"],
        "tasks_done": tasks["done"],
        "tasks_failed": tasks["failed"],
        "tasks_open": tasks_open,
        "needs_you": needs,
        "needs_you_total": sum(needs.values()),
    }


# --- Agents and departments ---------------------------------------------------------


def agents(cursor: psycopg.Cursor, paused: bool, name: str | None = None) -> list[dict[str, Any]]:
    """Every agent with the state the screen shows: idle, working, waiting
    (for the owner), paused or stopped (switched off)."""
    day = _day(cursor)
    cursor.execute(
        """
        select a.id, a.name, a.role_type, a.runner, a.model_tier, a.autonomy_level, a.enabled,
               a.daily_budget_usd, a.allowed_tools, d.id as department_id,
               d.name as department, d.enabled as department_enabled,
               (select coalesce(sum(mc.cost_usd), 0) from public.model_calls mc
                 where mc.agent_id = a.id and mc.created_at >= %(day)s) as spent_today,
               exists (select 1 from public.runs r where r.agent_id = a.id
                        and (r.status in ('running', 'pending')
                             or (r.status = 'paused' and r.stop_reason = any(%(carries_on)s))))
                 as busy,
               exists (select 1 from public.runs r where r.agent_id = a.id
                        and r.status = 'paused' and r.stop_reason <> all(%(carries_on)s)
                        and r.stop_reason <> 'awaiting_approval') as held_up,
               exists (select 1 from public.approvals ap where ap.agent_id = a.id
                        and ap.status = 'pending') as waiting,
               (select jsonb_build_object('id', t.id, 'title', t.title, 'status', t.status)
                  from public.tasks t where t.assigned_agent_id = a.id
                   and t.status in ('running', 'blocked', 'awaiting_approval', 'queued')
                 order by t.updated_at desc limit 1) as current_task
          from public.agents a
          left join public.departments d on d.id = a.department_id
         where %(name)s::text is null or a.name = %(name)s::text
         order by d.name nulls last, a.name
        """,
        {"day": day, "carries_on": list(CARRIES_ON), "name": name},
    )
    out = []
    for r in cursor.fetchall():
        if not r["enabled"] or r["department_enabled"] is False:
            state = "stopped"
        elif paused or r["held_up"]:
            state = "paused"
        elif r["waiting"]:
            state = "waiting"
        elif r["busy"]:
            state = "working"
        else:
            state = "idle"
        out.append(
            {
                "id": str(r["id"]),
                "name": r["name"],
                "role": r["role_type"],
                "runner": r["runner"],
                "tier": r["model_tier"],
                "level": r["autonomy_level"],
                "enabled": r["enabled"],
                "department_id": str(r["department_id"]) if r["department_id"] else None,
                "department": r["department"],
                "state": state,
                "current_task": r["current_task"],
                "budget_usd": None
                if r["daily_budget_usd"] is None
                else _money(r["daily_budget_usd"]),
                "spent_today_usd": _money(r["spent_today"]),
                "tools": list(r["allowed_tools"] or []),
            }
        )
    return out


def departments(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    day = _day(cursor)
    cursor.execute(
        """
        select d.id, d.name, d.enabled, d.daily_budget_usd,
               (select coalesce(sum(mc.cost_usd), 0) from public.model_calls mc
                  join public.agents a on a.id = mc.agent_id
                 where a.department_id = d.id and mc.created_at >= %s) as spent_today,
               (select a.name from public.agents a where a.department_id = d.id
                  and a.role_type in ('head', 'chief_of_staff')
                 order by a.role_type = 'chief_of_staff' desc, a.name limit 1) as head
          from public.departments d order by d.name
        """,
        (day,),
    )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "enabled": r["enabled"],
            "head": r["head"],
            "budget_usd": _money(r["daily_budget_usd"]),
            "spent_today_usd": _money(r["spent_today"]),
        }
        for r in cursor.fetchall()
    ]


def agent_detail(cursor: psycopg.Cursor, name: str, paused: bool) -> dict[str, Any] | None:
    found = agents(cursor, paused, name=name)
    if not found:
        return None
    agent = found[0]
    cursor.execute(
        """
        select t.id, t.title, t.status, t.result ->> 'summary' as summary, t.error,
               t.finished_at,
               (select coalesce(sum(mc.cost_usd), 0) from public.runs r
                  join public.model_calls mc on mc.run_id = r.id where r.task_id = t.id) as cost
          from public.tasks t
         where t.assigned_agent_id = %s and t.status in ('done', 'failed', 'cancelled')
         order by t.finished_at desc nulls last limit 5
        """,
        (agent["id"],),
    )
    agent["last_results"] = [
        {
            "task_id": str(r["id"]),
            "title": r["title"],
            "status": r["status"],
            "summary": (r["summary"] or r["error"] or "")[:600],
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            "cost_usd": _money(r["cost"]),
        }
        for r in cursor.fetchall()
    ]
    cursor.execute(
        "select coalesce(sum(cost_usd), 0) as spent from public.model_calls "
        "where agent_id = %s and created_at >= now() - interval '7 days'",
        (agent["id"],),
    )
    agent["spent_7d_usd"] = _money(cursor.fetchone()["spent"])
    return agent


# --- Orders: the chat and the path ----------------------------------------------------


def _tree_cost(cursor: psycopg.Cursor, root_id: UUID | str) -> float:
    cursor.execute(
        """
        select coalesce(sum(mc.cost_usd), 0) as cost
          from public.tasks t
          join public.runs r on r.task_id = t.id
          join public.model_calls mc on mc.run_id = r.id
         where t.id = %s or t.root_task_id = %s
        """,
        (str(root_id), str(root_id)),
    )
    return _money(cursor.fetchone()["cost"])


_ORDER_COLUMNS = (
    "t.id, t.title, t.instructions, t.status, t.result, t.error, t.created_at, t.finished_at"
)


def orders(cursor: psycopg.Cursor, limit: int) -> list[dict[str, Any]]:
    """The owner's orders to the Chief of Staff, newest first, each with where
    it went, the questions that came back, its result and what it cost."""
    cursor.execute(
        f"""
        select {_ORDER_COLUMNS}
          from public.tasks t join public.agents a on a.id = t.assigned_agent_id
         where t.parent_task_id is null and a.role_type = 'chief_of_staff'
         order by t.created_at desc limit %s
        """,
        (limit,),
    )
    return [_card(cursor, t) for t in cursor.fetchall()]


def order_card(cursor: psycopg.Cursor, task_id: UUID | str) -> dict[str, Any] | None:
    """One order as the chat shows it: its route, questions, result and cost."""
    cursor.execute(f"select {_ORDER_COLUMNS} from public.tasks t where t.id = %s", (str(task_id),))
    row = cursor.fetchone()
    return None if row is None else _card(cursor, row)


def _card(cursor: psycopg.Cursor, t: dict[str, Any]) -> dict[str, Any]:
    cursor.execute(
        "select payload, created_at from public.events where type = 'order_routed' "
        "and payload ->> 'task_id' = %s order by created_at",
        (str(t["id"]),),
    )
    routed = [
        {
            "department": r["payload"].get("department"),
            "head": r["payload"].get("head"),
            "suggested_tier": r["payload"].get("suggested_tier"),
            "why": r["payload"].get("why"),
            "at": r["created_at"].isoformat(),
        }
        for r in cursor.fetchall()
    ]
    cursor.execute(
        "select id, status, explanation, payload, verdict, created_at, decided_at "
        "from public.approvals where task_id = %s and action_type = 'route_order' "
        "order by created_at",
        (str(t["id"]),),
    )
    questions = [
        {
            "approval_id": str(r["id"]),
            "status": r["status"],
            "text": r["explanation"],
            "recommended": (r["payload"] or {}).get("recommended"),
            "options": (r["payload"] or {}).get("options", []),
            "verdict": r["verdict"],
            "at": r["created_at"].isoformat(),
            "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
        }
        for r in cursor.fetchall()
    ]
    # What the work is waiting on the owner for (a draft, an email, an action),
    # anywhere in its tree: the outcome the owner acts on.
    cursor.execute(
        """
        select ap.id, ap.action_type, coalesce(ap.payload ->> 'title', ap.payload ->> 'subject',
               tk.title) as title
          from public.approvals ap join public.tasks tk on tk.id = ap.task_id
         where ap.status = 'pending' and ap.action_type <> 'route_order'
           and (tk.id = %s or tk.root_task_id = %s)
         order by ap.created_at
        """,
        (str(t["id"]), str(t["id"])),
    )
    waiting = [
        {"approval_id": str(r["id"]), "kind": r["action_type"], "title": r["title"]}
        for r in cursor.fetchall()
    ]
    return {
        "id": str(t["id"]),
        "title": t["title"],
        "text": t["instructions"] or t["title"],
        "waiting": waiting,
        "status": t["status"],
        "result": ((t["result"] or {}).get("summary") or "")[:4000] or None,
        "error": t["error"],
        "at": t["created_at"].isoformat(),
        "finished_at": t["finished_at"].isoformat() if t["finished_at"] else None,
        "routed": routed,
        "questions": questions,
        "cost_usd": _tree_cost(cursor, t["id"]),
    }


def order_path(cursor: psycopg.Cursor, task_id: UUID) -> dict[str, Any] | None:
    """One order's whole tree: who did each part, how it ended, what it cost.
    Asked for any task in it, the answer is the whole order's tree."""
    cursor.execute(
        "select coalesce(root_task_id, id) as root from public.tasks where id = %s",
        (str(task_id),),
    )
    found = cursor.fetchone()
    if found is None:
        return None
    task_id = found["root"]
    cursor.execute(
        """
        select t.id, t.parent_task_id, t.depth, t.title, t.status, t.created_at, t.finished_at,
               t.result ->> 'summary' as summary, t.error, a.id as agent_id, a.name as agent,
               d.name as department,
               (select coalesce(sum(mc.cost_usd), 0) from public.runs r
                  join public.model_calls mc on mc.run_id = r.id where r.task_id = t.id) as cost
          from public.tasks t
          left join public.agents a on a.id = t.assigned_agent_id
          left join public.departments d on d.id = t.department_id
         where t.id = %s or t.root_task_id = %s
         order by t.depth, t.created_at, t.id
        """,
        (str(task_id), str(task_id)),
    )
    rows = cursor.fetchall()
    steps = [
        {
            "id": str(r["id"]),
            "parent_id": str(r["parent_task_id"]) if r["parent_task_id"] else None,
            "depth": r["depth"],
            "title": r["title"],
            "status": r["status"],
            "agent_id": str(r["agent_id"]) if r["agent_id"] else None,
            "agent": r["agent"],
            "department": r["department"],
            "summary": (r["summary"] or r["error"] or "")[:600] or None,
            "at": r["created_at"].isoformat(),
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            "cost_usd": _money(r["cost"]),
        }
        for r in rows
    ]
    return {
        "id": str(task_id),
        "steps": steps,
        "cost_usd": round(sum(s["cost_usd"] for s in steps), 6),
    }


# --- Events for replay ------------------------------------------------------------------


def events(
    cursor: psycopg.Cursor, since: datetime, until: datetime | None, limit: int, quiet: bool
) -> list[dict[str, Any]]:
    cursor.execute(
        """
        select id, type, created_at, agent_id, run_id, payload from public.events
         where created_at >= %s and (%s::timestamptz is null or created_at < %s::timestamptz)
           and (%s or type <> all(%s))
         order by created_at, id limit %s
        """,
        (since, until, until, not quiet, list(QUIET), limit),
    )
    return [
        {
            "id": str(r["id"]),
            "type": r["type"],
            "at": r["created_at"].isoformat(),
            "agent_id": str(r["agent_id"]) if r["agent_id"] else None,
            "run_id": str(r["run_id"]) if r["run_id"] else None,
            "payload": r["payload"],
        }
        for r in cursor.fetchall()
    ]


# --- Routes -------------------------------------------------------------------------


@router.get("/screen/status")
def get_status(principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    """Running or paused, and when that last changed."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        return pause_state(cursor)


@router.get("/screen/pulse")
def get_pulse(principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    """Today's strip: spend against budget, facts, tasks, and what needs the owner."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        return pulse(cursor)


@router.get("/screen/snapshot")
def get_snapshot(principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    """Everything the brain screen draws at load: departments, agents and their
    states, neighbourhoods, facts with their places, held claims, the pulse."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        state = pause_state(cursor)
        return {
            "status": state,
            "pulse": pulse(cursor),
            "departments": departments(cursor),
            "agents": agents(cursor, state["state"] == "paused"),
            "neighbourhoods": view.neighbourhoods(cursor),
            "facts": view.facts_on_the_map(cursor),
            "held": view.held_claims(cursor),
        }


@router.get("/screen/agents")
def get_agents_and_departments(principal: OwnerPrincipal, connection: Connection) -> dict:
    """Agents with their states and departments with their spend: the part of
    the snapshot that changes with every event, without the facts."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        paused = pause_state(cursor)["state"] == "paused"
        return {"agents": agents(cursor, paused), "departments": departments(cursor)}


@router.get("/screen/events")
def get_events(
    principal: OwnerPrincipal,
    connection: Connection,
    since: datetime,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
    quiet: bool = True,
) -> list[dict[str, Any]]:
    """Events in a time window, oldest first, for replay. `quiet` leaves out
    model calls, judgments and run steps."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        return events(cursor, since, until, limit, quiet)


@router.get("/facts/{fact_id}")
def get_fact(fact_id: UUID, principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        found = view.fact_detail(cursor, fact_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such fact")
    return found


@router.get("/agents/{name}/detail")
def get_agent_detail(name: str, principal: OwnerPrincipal, connection: Connection) -> dict:
    """What an agent is doing now, its last results, tools, level and spend."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        found = agent_detail(cursor, name, pause_state(cursor)["state"] == "paused")
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent named {name!r}")
    return found


@router.get("/orders")
def get_orders(
    principal: OwnerPrincipal,
    connection: Connection,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """The chat with the Chief of Staff, newest order first."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        return orders(cursor, limit)


@router.get("/orders/{task_id}/path")
def get_order_path(task_id: UUID, principal: OwnerPrincipal, connection: Connection) -> dict:
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        found = order_path(cursor, task_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such order")
    return found
