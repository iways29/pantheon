"""The Control Center's backend (Step 11, ADR 039).

Screens over what is already data: departments and their charters, agents and
their prompts, the morning routine, models, tools, the autonomy ladder and its
limits, the judge, standing rules and the change log. Every write runs as the
owner (RLS decides), and every change reaches `events` through the tables' own
audit triggers, so nothing here writes an event by hand unless no table does.

One rule keeps the charter the single source of truth: a change to something a
charter defines (a charter agent's prompt, a routine, a department's budget)
publishes a new charter version and applies it, so applying the charter later
never quietly undoes the owner's edit.
"""

import json
from datetime import time
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, get_settings
from app.db import acting_as
from app.owner_api import Connection, OwnerPrincipal, owner_org

router = APIRouter(prefix="/control", tags=["control"])

#: Events the change log leaves out: work, not changes.
_WORK = (
    "model_call",
    "judgment_made",
    "run_step",
    "run_invoked",
    "tool_called",
    "chat_recalled",
    "chat_said",
    "fact_write_decided",
)


def _money(value: Any) -> float:  # noqa: ANN401 - numeric from the database
    return round(float(value or 0), 4)


def _bad(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(code, message)


# --- Charters: the one place a department is defined ---------------------------


def _charter_patch(
    connection: psycopg.Connection,
    user_id: str,
    org_id: str,
    department: str,
    change: Any,  # noqa: ANN401 - a function from charter dict to charter dict
    note: str,
    *,
    apply: bool = True,
) -> dict[str, Any]:
    """Publish the live charter with `change` made, then apply it."""
    from app.departments.apply import apply_charter
    from app.departments.charter import Charter, CharterError, load, publish

    try:
        with acting_as(connection, user_id=user_id) as conn:
            _, live = load(conn, org_id=org_id, department=department)
        data = change(live.model_dump(mode="json"))
        charter = Charter.model_validate(data)
    except CharterError as error:
        raise _bad(str(error), error.status) from error
    except ValidationError as error:
        raise _bad(error.errors(include_url=False)[0]["msg"], 422) from error
    version = publish(
        connection,
        user_id=user_id,
        org_id=org_id,
        department=department,
        charter=charter,
        note=note,
    )
    out: dict[str, Any] = {"department": department, "version": version}
    if apply and not charter.draft:
        out["applied"] = apply_charter(
            connection, user_id=user_id, org_id=org_id, department=department
        ).summary()
    return out


def _charter_of(cursor: psycopg.Cursor, agent: str) -> tuple[str, dict[str, Any]] | None:
    """The live charter that defines this agent, if one does."""
    cursor.execute(
        "select department, charter from public.department_charters where active "
        "and (charter->'head'->>'name' = %s or exists (select 1 from "
        "jsonb_array_elements(charter->'workers') w where w->>'name' = %s))",
        (agent, agent),
    )
    row = cursor.fetchone()
    return (row["department"], row["charter"]) if row else None


@router.get("/departments")
def departments(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    """Every department: switch, budget, spend, head, agents, charter."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select d.id, d.name, d.enabled, d.daily_budget_usd,
                   (select a.name from public.agents a where a.department_id = d.id
                     and a.role_type in ('head', 'chief_of_staff') order by a.name limit 1) as head,
                   (select count(*) from public.agents a where a.department_id = d.id) as agents,
                   (select count(*) from public.agents a where a.department_id = d.id
                     and a.enabled) as agents_on,
                   (select coalesce(sum(mc.cost_usd), 0) from public.model_calls mc
                      join public.agents a on a.id = mc.agent_id
                     where a.department_id = d.id
                       and mc.created_at >= date_trunc('day', now() at time zone
                           'America/New_York') at time zone 'America/New_York') as spend_today,
                   (select coalesce(sum(mc.cost_usd), 0) from public.model_calls mc
                      join public.agents a on a.id = mc.agent_id
                     where a.department_id = d.id
                       and mc.created_at >= now() - interval '7 days') as spend_7d,
                   c.version as charter_version, (c.charter->>'draft')::boolean as draft,
                   c.charter->>'purpose' as purpose
              from public.departments d
              left join public.department_charters c on c.department = d.name and c.active
             order by d.name
            """
        )
        return [
            {
                "name": r["name"],
                "enabled": r["enabled"],
                "daily_budget_usd": _money(r["daily_budget_usd"]),
                "head": r["head"],
                "agents": r["agents"],
                "agents_on": r["agents_on"],
                "spend_today_usd": _money(r["spend_today"]),
                "spend_7d_usd": _money(r["spend_7d"]),
                "charter_version": r["charter_version"],
                "draft": bool(r["draft"]),
                "purpose": r["purpose"],
            }
            for r in cursor.fetchall()
        ]


@router.get("/departments/{department}/charters")
def charter_versions(
    department: str, principal: OwnerPrincipal, connection: Connection
) -> list[dict[str, Any]]:
    """Every version of a department's charter, newest first."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select version, active, note, created_at, charter from public.department_charters "
            "where department = %s order by version desc",
            (department,),
        )
        return [
            {
                "version": r["version"],
                "active": r["active"],
                "note": r["note"],
                "at": r["created_at"].isoformat(),
                "charter": r["charter"],
            }
            for r in cursor.fetchall()
        ]


class CharterBody(BaseModel):
    charter: dict[str, Any]


@router.post("/departments/{department}/preview")
def preview_charter(
    department: str, body: CharterBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """What publishing and applying this charter would do, done and undone:
    "Creates 3 agents (switched off), updates 1 prompt...". Nothing is kept."""
    from app.departments.apply import apply_charter
    from app.departments.charter import Charter, CharterError, publish

    org_id = owner_org(connection, principal.user_id)
    try:
        charter = Charter.model_validate({**body.charter, "draft": False})
    except ValidationError as error:
        raise _bad(error.errors(include_url=False)[0]["msg"], 422) from error
    try:
        with connection.transaction(force_rollback=True):
            publish(
                connection,
                user_id=principal.user_id,
                org_id=org_id,
                department=department,
                charter=charter,
                note="preview",
            )
            report = apply_charter(
                connection, user_id=principal.user_id, org_id=org_id, department=department
            )
    except CharterError as error:
        raise _bad(str(error), error.status) from error
    return report.summary()


class BudgetBody(BaseModel):
    daily_budget_usd: Decimal = Field(ge=0, le=100)


@router.post("/departments/{department}/budget")
def set_department_budget(
    department: str, body: BudgetBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """A department's daily budget, through its charter."""
    org_id = owner_org(connection, principal.user_id)
    budget = str(body.daily_budget_usd)
    return _charter_patch(
        connection,
        principal.user_id,
        org_id,
        department,
        lambda c: {**c, "daily_budget_usd": budget},
        f"Daily budget ${budget} (Control Center)",
    )


class VersionBody(BaseModel):
    version: int = Field(ge=1)


@router.post("/departments/{department}/restore")
def restore_charter(
    department: str, body: VersionBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """Make an earlier charter live again, as a new version, and apply it."""
    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select charter from public.department_charters where department = %s and version = %s",
            (department, body.version),
        )
        row = cursor.fetchone()
    if row is None:
        raise _bad(f"No version {body.version} of {department!r}", 404)
    old = row["charter"]
    return _charter_patch(
        connection,
        principal.user_id,
        org_id,
        department,
        lambda _c: old,
        f"Back to v{body.version} (Control Center)",
    )


# --- Agents and their prompts --------------------------------------------------


@router.get("/agents")
def agents(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select a.name, a.role, a.role_type, a.runner, a.model_tier, a.autonomy_level,
                   a.enabled, a.daily_budget_usd, a.allowed_tools, d.name as department,
                   (select coalesce(sum(mc.cost_usd), 0) from public.model_calls mc
                     where mc.agent_id = a.id
                       and mc.created_at >= date_trunc('day', now() at time zone
                           'America/New_York') at time zone 'America/New_York') as spend_today,
                   (select coalesce(sum(mc.cost_usd), 0) from public.model_calls mc
                     where mc.agent_id = a.id and mc.created_at >= now() - interval '7 days')
                     as spend_7d,
                   (select jsonb_object_agg(p.slot, p.version) from public.agent_prompts p
                     where p.agent_id = a.id and p.active) as prompts
              from public.agents a left join public.departments d on d.id = a.department_id
             order by d.name nulls last, a.role_type <> 'chief_of_staff',
                      a.role_type <> 'head', a.name
            """
        )
        return [
            {
                "name": r["name"],
                "role": r["role"],
                "role_type": r["role_type"],
                "runner": r["runner"],
                "tier": r["model_tier"],
                "level": r["autonomy_level"],
                "enabled": r["enabled"],
                "daily_budget_usd": _money(r["daily_budget_usd"]),
                "tools": list(r["allowed_tools"] or []),
                "department": r["department"],
                "spend_today_usd": _money(r["spend_today"]),
                "spend_7d_usd": _money(r["spend_7d"]),
                "prompts": r["prompts"] or {},
            }
            for r in cursor.fetchall()
        ]


def _agent_row(cursor: psycopg.Cursor, name: str) -> dict[str, Any]:
    cursor.execute("select id, name from public.agents where name = %s", (name,))
    row = cursor.fetchone()
    if row is None:
        raise _bad(f"No agent {name!r}", 404)
    return row


@router.get("/agents/{name}/prompts")
def prompt_versions(
    name: str, principal: OwnerPrincipal, connection: Connection
) -> list[dict[str, Any]]:
    """Every version of every prompt slot, newest first, with how many runs used it."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        agent = _agent_row(cursor, name)
        cursor.execute(
            """
            select p.slot, p.version, p.body, p.note, p.active, p.created_at,
                   (select count(*) from public.runs r where r.agent_id = p.agent_id
                     and (r.prompt_versions ->> p.slot)::int = p.version) as runs
              from public.agent_prompts p where p.agent_id = %s
             order by p.slot, p.version desc
            """,
            (str(agent["id"]),),
        )
        return [
            {
                "slot": r["slot"],
                "version": r["version"],
                "body": r["body"],
                "note": r["note"],
                "active": r["active"],
                "at": r["created_at"].isoformat(),
                "runs": r["runs"],
            }
            for r in cursor.fetchall()
        ]


class PromptBody(BaseModel):
    body: str = Field(min_length=1, max_length=40000)
    note: str | None = Field(default=None, max_length=300)


def _prompt_into_charter(
    connection: psycopg.Connection,
    user_id: str,
    org_id: str,
    agent: str,
    slot: str,
    body: str,
    note: str,
) -> int | None:
    """Keep the defining charter in step with a prompt the owner changed."""
    with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:
        found = _charter_of(cursor, agent)
    if found is None:
        return None
    department, charter = found
    plans = [charter["head"], *charter.get("workers", [])]
    plan = next(p for p in plans if p["name"] == agent)
    if (plan.get("prompts") or {}).get(slot) == body:
        return None

    def change(c: dict[str, Any]) -> dict[str, Any]:
        def fix(p: dict[str, Any]) -> dict[str, Any]:
            if p["name"] != agent:
                return p
            return {**p, "prompts": {**(p.get("prompts") or {}), slot: body}}

        return {**c, "head": fix(c["head"]), "workers": [fix(w) for w in c.get("workers", [])]}

    # The prompt itself is already live; the charter only follows it.
    return _charter_patch(connection, user_id, org_id, department, change, note, apply=False)[
        "version"
    ]


@router.post("/agents/{name}/prompts/{slot}")
def publish_prompt(
    name: str, slot: str, body: PromptBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """A new version of a prompt, live from the next run."""
    from app.agents.prompts import publish

    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        agent = _agent_row(cursor, name)
    prompt = publish(
        connection,
        user_id=principal.user_id,
        agent_id=agent["id"],
        slot=slot,
        body=body.body,
        note=body.note or "Edited in the Control Center",
    )
    charter = _prompt_into_charter(
        connection,
        principal.user_id,
        org_id,
        name,
        slot,
        body.body,
        f"{name} {slot} prompt v{prompt.version} (Control Center)",
    )
    return {"slot": slot, "version": prompt.version, "charter_version": charter}


@router.post("/agents/{name}/prompts/{slot}/activate")
def activate_prompt(
    name: str, slot: str, body: VersionBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """Make an earlier version live: the roll back."""
    from app.agents.prompts import activate

    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        agent = _agent_row(cursor, name)
    try:
        prompt = activate(
            connection,
            user_id=principal.user_id,
            agent_id=agent["id"],
            slot=slot,
            version=body.version,
        )
    except psycopg.Error as error:
        raise _bad(f"No version {body.version} of {name}'s {slot} prompt", 404) from error
    charter = _prompt_into_charter(
        connection,
        principal.user_id,
        org_id,
        name,
        slot,
        prompt.body,
        f"{name} {slot} prompt back to v{body.version} (Control Center)",
    )
    return {"slot": slot, "version": prompt.version, "charter_version": charter}


class AgentSettings(BaseModel):
    tier: Literal["cheap", "standard", "frontier"] | None = None
    daily_budget_usd: Decimal | None = Field(default=None, ge=0, le=100)


@router.post("/agents/{name}/settings")
def agent_settings(
    name: str, body: AgentSettings, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """An agent's tier and budget; through its charter when it has one."""
    org_id = owner_org(connection, principal.user_id)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise _bad("Nothing to change")
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        _agent_row(cursor, name)
        found = _charter_of(cursor, name)
    if found is not None:
        plan_fields = {"tier": fields.get("tier")}
        if "daily_budget_usd" in fields:
            plan_fields["daily_budget_usd"] = str(fields["daily_budget_usd"])

        def change(c: dict[str, Any]) -> dict[str, Any]:
            def fix(p: dict[str, Any]) -> dict[str, Any]:
                if p["name"] != name:
                    return p
                return {**p, **{k: v for k, v in plan_fields.items() if v is not None}}

            return {
                **c,
                "head": fix(c["head"]),
                "workers": [fix(w) for w in c.get("workers", [])],
            }

        return _charter_patch(
            connection,
            principal.user_id,
            org_id,
            found[0],
            change,
            f"{name}: {', '.join(f'{k} {v}' for k, v in fields.items())} (Control Center)",
        )
    columns = {"model_tier": fields.get("tier"), "daily_budget_usd": fields.get("daily_budget_usd")}
    columns = {k: v for k, v in columns.items() if v is not None}
    with acting_as(connection, user_id=principal.user_id) as conn:
        conn.execute(
            f"update public.agents set {', '.join(f'{k} = %s' for k in columns)} "
            "where org_id = %s and name = %s",
            (*columns.values(), org_id, name),
        )
    return {"agent": name, **{k: str(v) for k, v in fields.items()}}


# --- The morning routine ---------------------------------------------------------


def _routine_json(r: dict[str, Any]) -> dict[str, Any]:
    task = r["task"] or {}
    return {
        "id": str(r["id"]),
        "key": r["routine_key"],
        "name": r["name"],
        "agent": r["agent"],
        "department": r["department"],
        "instructions": task.get("instructions") or task.get("question") or "",
        "input": {k: v for k, v in task.items() if k not in ("instructions", "question")},
        "time": r["time_of_day"].strftime("%H:%M"),
        "days": list(r["days_of_week"]),
        "timezone": r["timezone"],
        "enabled": r["enabled"],
        "grace_minutes": r["grace_minutes"],
        "max_steps": r["max_steps"],
        "max_tokens": r["max_tokens"],
        "runs": r["runs"] or [],
    }


@router.get("/routines")
def routines(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    """Every routine, with its last ten firings: when, how it ended, what it cost."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select t.*, a.name as agent, d.name as department,
                   (select jsonb_agg(x order by x->>'at' desc) from (
                      select jsonb_build_object('task_id', k.id, 'at', k.created_at,
                             'status', k.status,
                             'cost_usd', coalesce(c.tree_cost_usd, 0)) as x
                        from public.tasks k
                        left join public.task_costs c on c.task_id = k.id
                       where k.idempotency_key like 'trigger:' || t.id || ':%%'
                          or k.idempotency_key like 'routine-now:' || t.id || ':%%'
                       order by k.created_at desc limit 10) s) as runs
              from public.triggers t
              join public.agents a on a.id = t.agent_id
              left join public.departments d on d.id = a.department_id
             order by t.time_of_day, t.name
            """
        )
        return [_routine_json(r) for r in cursor.fetchall()]


class RoutineBody(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    instructions: str | None = Field(default=None, max_length=8000)
    input: dict[str, Any] | None = None
    time: str | None = None
    days: list[int] | None = None
    timezone: str | None = None
    max_steps: int | None = Field(default=None, gt=0, le=100)
    max_tokens: int | None = Field(default=None, ge=1000, le=500000)


def _trigger(cursor: psycopg.Cursor, routine_id: UUID) -> dict[str, Any]:
    cursor.execute("select * from public.triggers where id = %s", (str(routine_id),))
    row = cursor.fetchone()
    if row is None:
        raise _bad("No such routine", 404)
    return row


@router.put("/routines/{routine_id}")
def edit_routine(
    routine_id: UUID, body: RoutineBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """Change a routine. One a charter made changes in its charter, then applies."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    org_id = owner_org(connection, principal.user_id)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise _bad("Nothing to change")
    if "time" in fields:
        try:
            time.fromisoformat(fields["time"])
        except ValueError as error:
            raise _bad("Time is 24-hour, like 06:30", 422) from error
    if "days" in fields and (not fields["days"] or any(d not in range(7) for d in fields["days"])):
        raise _bad("Pick at least one day", 422)
    if "timezone" in fields:
        try:
            ZoneInfo(fields["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise _bad(f"Unknown time zone {fields['timezone']!r}", 422) from error
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        row = _trigger(cursor, routine_id)
    key = row["routine_key"]
    if key:
        department, item_key = key.split(":", 1)
        rename = {"title": "title", "instructions": "instructions", "input": "input"}

        def change(c: dict[str, Any]) -> dict[str, Any]:
            items = []
            for item in c.get("routine", []):
                if item["key"] == item_key:
                    item = {**item, **{rename.get(k, k): v for k, v in fields.items()}}
                items.append(item)
            return {**c, "routine": items}

        return _charter_patch(
            connection,
            principal.user_id,
            org_id,
            department,
            change,
            f"Routine {row['name']}: {', '.join(sorted(fields))} (Control Center)",
        )
    columns: dict[str, Any] = {}
    task = dict(row["task"] or {})
    if "instructions" in fields:
        task["instructions"] = fields["instructions"]
    if "input" in fields:
        task = {**fields["input"], "instructions": task.get("instructions", "")}
    if "instructions" in fields or "input" in fields:
        columns["task"] = json.dumps(task)
    for name, column in (
        ("title", "name"),
        ("time", "time_of_day"),
        ("days", "days_of_week"),
        ("timezone", "timezone"),
        ("max_steps", "max_steps"),
        ("max_tokens", "max_tokens"),
    ):
        if name in fields:
            columns[column] = fields[name]
    with acting_as(connection, user_id=principal.user_id) as conn:
        conn.execute(
            f"update public.triggers set {', '.join(f'{k} = %s' for k in columns)} where id = %s",
            (*columns.values(), str(routine_id)),
        )
    return {"routine": str(routine_id), "changed": sorted(fields)}


class SwitchBody(BaseModel):
    on: bool


@router.post("/routines/{routine_id}/switch")
def switch_routine(
    routine_id: UUID, body: SwitchBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        _trigger(cursor, routine_id)
        cursor.execute(
            "update public.triggers set enabled = %s where id = %s", (body.on, str(routine_id))
        )
    return {"routine": str(routine_id), "enabled": body.on}


@router.post("/routines/{routine_id}/run")
def run_routine_now(
    routine_id: UUID, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """Fire a routine now, as its morning would (owner requests included are
    left for the morning). One press a minute counts once."""
    from datetime import UTC, datetime

    from app.tasks import TaskError, order

    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        row = _trigger(cursor, routine_id)
        cursor.execute("select name from public.agents where id = %s", (str(row["agent_id"]),))
        agent = cursor.fetchone()["name"]
    task = dict(row["task"] or {})
    instructions = task.get("question") or task.get("instructions") or ""
    minute = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    try:
        made = order(
            connection,
            user_id=principal.user_id,
            org_id=org_id,
            agent=agent,
            title=row["name"],
            instructions=instructions,
            input=task,
            idempotency_key=f"routine-now:{routine_id}:{minute}",
        )
    except TaskError as error:
        raise _bad(str(error)) from error
    try:
        with acting_as(connection, user_id=principal.user_id) as conn:
            conn.execute("select public.start_now()")
    except psycopg.Error:
        pass  # the minute tick starts it instead
    return {"task_id": str(made.id), "status": made.status}


# --- Models and spend ---------------------------------------------------------------

_catalogue_cache: dict[str, Any] = {}


def get_catalogue() -> Any:  # noqa: ANN401 - overridden in tests
    """OpenRouter's public model list (no key): ids, names and prices."""
    import time as clock

    import httpx

    cached = _catalogue_cache.get("models")
    if cached and clock.monotonic() - cached[0] < 3600:
        return cached[1]
    response = httpx.get("https://openrouter.ai/api/v1/models", timeout=20.0)
    response.raise_for_status()
    models = []
    for m in response.json()["data"]:
        pricing = m.get("pricing") or {}
        try:
            prompt = float(pricing.get("prompt") or 0) * 1_000_000
            completion = float(pricing.get("completion") or 0) * 1_000_000
        except (TypeError, ValueError):
            prompt = completion = 0.0
        models.append(
            {
                "id": m["id"],
                "name": m.get("name") or m["id"],
                "in_usd_per_mtok": round(prompt, 4),
                "out_usd_per_mtok": round(completion, 4),
                "tools": "tools" in (m.get("supported_parameters") or []),
                "context": m.get("context_length"),
            }
        )
    models.sort(key=lambda m: m["id"])
    _catalogue_cache["models"] = (clock.monotonic(), models)
    return models


CatalogueDep = Annotated[Any, Depends(get_catalogue)]


@router.get("/models")
def models(
    principal: OwnerPrincipal,
    connection: Connection,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Which model each tier uses, company-wide and per department, with prices
    and spend against budgets."""
    from app.gateway.factory import tier_map_from

    owner_org(connection, principal.user_id)
    try:
        fallback = dict(tier_map_from(settings).models)
    except ValueError:
        fallback = {}
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select m.tier, m.model, d.name as department, m.updated_at "
            "from public.model_tier_assignments m "
            "left join public.departments d on d.id = m.department_id "
            "order by d.name nulls first, m.tier"
        )
        assignments = [
            {
                "tier": r["tier"],
                "model": r["model"],
                "department": r["department"],
                "at": r["updated_at"].isoformat(),
            }
            for r in cursor.fetchall()
        ]
        cursor.execute(
            "select model, input_usd_per_mtok, output_usd_per_mtok from public.model_prices"
        )
        prices = {
            r["model"]: {
                "in": _money(r["input_usd_per_mtok"]),
                "out": _money(r["output_usd_per_mtok"]),
            }
            for r in cursor.fetchall()
        }
        cursor.execute(
            """
            select coalesce(sum(cost_usd) filter (where created_at >= date_trunc('day',
                     now() at time zone 'America/New_York') at time zone 'America/New_York'), 0)
                     as today,
                   coalesce(sum(cost_usd) filter (where created_at >= now() - interval '7 days'),
                     0) as week,
                   coalesce(sum(cost_usd) filter (where created_at >= now() - interval '30 days'),
                     0) as month,
                   coalesce(sum(cost_usd) filter (where created_at >= date_trunc('month', now())),
                     0) as this_month
              from public.model_calls
            """
        )
        spend = {k: _money(v) for k, v in cursor.fetchone().items()}
        cursor.execute(
            "select coalesce(sum(daily_budget_usd), 0) as budget from public.departments "
            "where enabled"
        )
        spend["daily_budget"] = _money(cursor.fetchone()["budget"])
    return {
        "tiers": ["cheap", "standard", "frontier", "embedding"],
        "fallback": fallback,
        "assignments": assignments,
        "prices": prices,
        "spend": spend,
    }


@router.get("/models/catalogue")
def model_catalogue(principal: OwnerPrincipal, catalogue: CatalogueDep) -> list[dict[str, Any]]:
    return catalogue


class TierBody(BaseModel):
    tier: Literal["cheap", "standard", "frontier", "embedding"]
    model: str | None = Field(default=None, max_length=200)
    #: None: company-wide. A department's row overrides the company's.
    department: str | None = None


@router.put("/models")
def set_model(
    body: TierBody, principal: OwnerPrincipal, connection: Connection, catalogue: CatalogueDep
) -> dict[str, Any]:
    """Point a tier at a model from OpenRouter's catalogue, or clear an override."""
    from app.gateway.model_admin import UnknownModel, assign_model, clear_assignment

    org_id = owner_org(connection, principal.user_id)

    class _Ids:
        def model_ids(self) -> frozenset[str]:
            return frozenset(m["id"] for m in catalogue)

    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        department_id = None
        if body.department:
            cursor.execute("select id from public.departments where name = %s", (body.department,))
            row = cursor.fetchone()
            if row is None:
                raise _bad(f"No department {body.department!r}", 404)
            department_id = row["id"]
        if body.model is None:
            clear_assignment(conn, org_id=org_id, tier=body.tier, department_id=department_id)
            return {"tier": body.tier, "department": body.department, "model": None}
        if body.tier == "embedding":
            raise _bad("The embedding model changes with a re-embedding of the brain; ask first")
        try:
            assign_model(
                conn,
                org_id=org_id,
                tier=body.tier,
                model=body.model,
                catalogue=_Ids(),
                department_id=department_id,
            )
        except UnknownModel as error:
            raise _bad(str(error), 422) from error
    return {"tier": body.tier, "department": body.department, "model": body.model}


# --- Tools ---------------------------------------------------------------------------


@router.get("/tools")
def tools(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    """Every tool: built-in and from MCP servers, with calls today."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select t.name, t.description, t.risk_class, t.approval, t.enabled,
                   t.timeout_seconds, t.max_output_chars, t.max_calls_per_day, t.settings,
                   t.source, t.suggested_risk, s.name as server,
                   t.approved_sha is distinct from t.definition_sha as changed,
                   (select count(*) from public.tool_calls c where c.tool = t.name
                     and c.created_at >= now() - interval '1 day') as calls_today,
                   (select array_agg(a.name order by a.name) from public.agents a
                     where t.name = any(a.allowed_tools)) as agents
              from public.tools t left join public.mcp_servers s on s.id = t.mcp_server_id
             order by t.source, t.name
            """
        )
        return [
            {
                **{k: r[k] for k in r if k not in ("agents", "settings")},
                "changed": bool(r["changed"]) and r["source"] == "mcp",
                "settings": r["settings"] or {},
                "agents": list(r["agents"] or []),
            }
            for r in cursor.fetchall()
        ]


class ToolBody(BaseModel):
    risk_class: Literal["R0", "R1", "R2", "R3", "R4"] | None = None
    approval: Literal["auto", "approval"] | None = None
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)
    max_output_chars: int | None = Field(default=None, ge=100, le=100000)
    max_calls_per_day: int | None = Field(default=None, ge=1, le=10000)
    settings: dict[str, Any] | None = None


@router.put("/tools/{name}")
def edit_tool(
    name: str, body: ToolBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """A tool's risk, approval, switch, timeout, output cap, daily cap or settings.
    R4 always asks; an MCP tool switches on only through its approval."""
    owner_org(connection, principal.user_id)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise _bad("Nothing to change")
    if "settings" in fields:
        fields["settings"] = json.dumps(fields["settings"])
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute("select risk_class, source from public.tools where name = %s", (name,))
        row = cursor.fetchone()
        if row is None:
            raise _bad(f"No tool {name!r}", 404)
        if row["source"] == "mcp" and fields.get("enabled"):
            raise _bad("An MCP tool is switched on by approving it (MCP servers)")
        risk = fields.get("risk_class", row["risk_class"])
        if risk == "R4" and fields.get("approval") == "auto":
            raise _bad("R4 tools always ask you; that cannot be changed")
        if risk == "R4":
            fields["approval"] = "approval"
        try:
            cursor.execute(
                f"update public.tools set {', '.join(f'{k} = %s' for k in fields)} where name = %s",
                (*fields.values(), name),
            )
        except psycopg.errors.CheckViolation as error:
            raise _bad(str(error).splitlines()[0], 422) from error
    return {"tool": name, "changed": sorted(fields)}


# --- Autonomy and limits -------------------------------------------------------------

_LIMITS = (
    "max_depth",
    "max_children",
    "max_tasks_per_department_per_day",
    "max_tasks_per_agent_per_hour",
    "loop_repeat_limit",
    "stuck_task_minutes",
    "promotion_min_decisions",
    "promotion_min_agreement",
)


@router.get("/autonomy")
def autonomy(principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    """The ladder, the limits, each agent's level and the promotion suggestions."""
    from app.agents.autonomy import suggestions

    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute("select level, risk_class, mode from public.autonomy_rules")
        rules = [dict(r) for r in cursor.fetchall()]
        cursor.execute(f"select {', '.join(_LIMITS)} from public.delegation_limits")
        row = cursor.fetchone()
        if row is None:
            # No row yet: the defaults the database would give one.
            cursor.execute(
                "select column_name, column_default from information_schema.columns "
                "where table_schema = 'public' and table_name = 'delegation_limits' "
                "and column_name = any(%s)",
                (list(_LIMITS),),
            )
            row = {r["column_name"]: float(r["column_default"]) for r in cursor.fetchall()}
            row = {
                k: int(v) if v.is_integer() and k != "promotion_min_agreement" else v
                for k, v in row.items()
            }
        limits = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in (row or {}).items()}
    return {
        "rules": rules,
        "limits": limits,
        "suggestions": suggestions(connection, user_id=principal.user_id, eligible_only=False),
    }


class RuleBody(BaseModel):
    level: Literal["L0", "L1", "L2", "L3"]
    risk_class: Literal["R0", "R1", "R2", "R3"]
    mode: Literal["run", "gate", "hold"]


@router.put("/autonomy/rules")
def set_rule(body: RuleBody, principal: OwnerPrincipal, connection: Connection) -> dict:
    """One cell of the ladder. R4 is not a cell: it always asks."""
    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn:
        try:
            conn.execute(
                "insert into public.autonomy_rules (org_id, level, risk_class, mode) "
                "values (%s, %s, %s, %s) on conflict (org_id, level, risk_class) "
                "do update set mode = excluded.mode",
                (org_id, body.level, body.risk_class, body.mode),
            )
        except psycopg.errors.CheckViolation as error:
            raise _bad(str(error).splitlines()[0], 422) from error
    return body.model_dump()


class LimitsBody(BaseModel):
    max_depth: int | None = Field(default=None, ge=1, le=10)
    max_children: int | None = Field(default=None, ge=1, le=50)
    max_tasks_per_department_per_day: int | None = Field(default=None, ge=1, le=1000)
    max_tasks_per_agent_per_hour: int | None = Field(default=None, ge=1, le=500)
    loop_repeat_limit: int | None = Field(default=None, ge=1, le=50)
    stuck_task_minutes: int | None = Field(default=None, ge=1, le=1440)
    promotion_min_decisions: int | None = Field(default=None, ge=1, le=1000)
    promotion_min_agreement: float | None = Field(default=None, ge=0.5, le=1)


@router.put("/autonomy/limits")
def set_limits(body: LimitsBody, principal: OwnerPrincipal, connection: Connection) -> dict:
    org_id = owner_org(connection, principal.user_id)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise _bad("Nothing to change")
    with acting_as(connection, user_id=principal.user_id) as conn:
        try:
            conn.execute(
                f"insert into public.delegation_limits (org_id, {', '.join(fields)}) "
                f"values (%s, {', '.join(['%s'] * len(fields))}) on conflict (org_id) do update "
                f"set {', '.join(f'{k} = excluded.{k}' for k in fields)}",
                (org_id, *fields.values()),
            )
        except psycopg.errors.CheckViolation as error:
            raise _bad(str(error).splitlines()[0], 422) from error
    return fields


# --- Judge -----------------------------------------------------------------------------


@router.get("/judge")
def judge(principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    """Every gate's live version and questions, recent volume, and agreement
    with the owner's decisions per kind of action."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select g.gate, g.version, g.enabled, g.model, g.fail_mode, g.allow_sensitive,
                   g.policy, g.note, g.created_at,
                   (select count(*) from public.judge_gates h where h.gate = g.gate) as versions,
                   (select count(*) from public.judgments j where j.gate = g.gate
                     and j.created_at >= now() - interval '7 days') as week,
                   (select jsonb_agg(jsonb_build_object('key', q.key, 'version', q.version,
                           'type', q.type, 'instructions', q.instructions,
                           'criteria', q.criteria) order by q.key)
                      from public.judge_questions q where q.gate = g.gate and q.active)
                     as questions
              from public.judge_gates g where g.active order by g.gate
            """
        )
        gates = [
            {
                **{k: v for k, v in r.items() if k != "created_at"},
                "at": r["created_at"].isoformat(),
                "questions": r["questions"] or [],
            }
            for r in cursor.fetchall()
        ]
        cursor.execute(
            "select action_key, decided, recommended, agreed, agreement "
            "from public.approval_agreement order by decided desc"
        )
        agreement = [
            {**r, "agreement": None if r["agreement"] is None else float(r["agreement"])}
            for r in cursor.fetchall()
        ]
    return {"gates": gates, "agreement": agreement}


@router.get("/judge/{gate}/versions")
def judge_versions(
    gate: str, principal: OwnerPrincipal, connection: Connection
) -> list[dict[str, Any]]:
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select version, active, enabled, fail_mode, model, policy, note, created_at "
            "from public.judge_gates where gate = %s order by version desc",
            (gate,),
        )
        return [
            {**{k: v for k, v in r.items() if k != "created_at"}, "at": r["created_at"].isoformat()}
            for r in cursor.fetchall()
        ]


class GateBody(BaseModel):
    enabled: bool | None = None
    fail_mode: Literal["open", "closed"] | None = None
    policy: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=300)


@router.post("/judge/{gate}")
def edit_gate(
    gate: str, body: GateBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """A new version of a gate with the given settings changed; the rest kept."""
    from app.judge.errors import JudgeError
    from app.judge.store import list_gates, publish_gate

    org_id = owner_org(connection, principal.user_id)
    live = {
        g.gate: g for g in list_gates(connection, user_id=principal.user_id, org_id=org_id)
    }.get(gate)
    if live is None:
        raise _bad(f"No gate {gate!r}", 404)
    try:
        made = publish_gate(
            connection,
            user_id=principal.user_id,
            org_id=org_id,
            gate=gate,
            policy=body.policy if body.policy is not None else live.policy,
            enabled=live.enabled if body.enabled is None else body.enabled,
            model=live.model,
            fail_mode=body.fail_mode or live.fail_mode,
            allow_sensitive=live.allow_sensitive,
            max_state_chars=live.max_state_chars,
            note=body.note or "Changed in the Control Center",
        )
    except (JudgeError, ValueError) as error:
        raise _bad(str(error), 422) from error
    return {"gate": gate, "version": made.version}


@router.post("/judge/{gate}/activate")
def activate_gate(
    gate: str, body: VersionBody, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    from app.judge.errors import JudgeError
    from app.judge.store import activate_gate as activate

    org_id = owner_org(connection, principal.user_id)
    try:
        made = activate(
            connection, user_id=principal.user_id, org_id=org_id, gate=gate, version=body.version
        )
    except (JudgeError, LookupError, psycopg.Error) as error:
        raise _bad(str(error), 422) from error
    return {"gate": gate, "version": made.version}


# --- Standing rules ---------------------------------------------------------------------


@router.get("/rules")
def rules(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    """The owner's standing rules: written here, or said in chat and sorted as
    rules. Retired ones are listed last."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select id, claim, status, kind, source_ref, created_at, updated_at
              from public.facts
             where source = 'owner' and (source_ref like 'policy:%%' or kind = 'rule')
             order by status = 'retired', created_at desc
            """
        )
        return [
            {
                "id": str(r["id"]),
                "text": r["claim"],
                "status": r["status"],
                "from": "written" if (r["source_ref"] or "").startswith("policy:") else "chat",
                "at": r["created_at"].isoformat(),
                "changed_at": r["updated_at"].isoformat(),
            }
            for r in cursor.fetchall()
        ]


class NewRule(BaseModel):
    text: str = Field(min_length=5, max_length=500)


@router.post("/rules", status_code=status.HTTP_201_CREATED)
def add_rule(
    body: NewRule,
    principal: OwnerPrincipal,
    connection: Connection,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """A standing rule, into the brain as the owner's own words. The Chief of
    Staff's budget pays for the check."""
    import hashlib

    from app.approvals import remember
    from app.knowledge.wiring import writer_from

    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select id from public.agents where role_type = 'chief_of_staff' "
            "order by created_at limit 1"
        )
        payer = cursor.fetchone()
        if payer is None:
            raise _bad("No Chief of Staff to record the rule")
        digest = hashlib.sha256(body.text.strip().lower().encode()).hexdigest()[:24]
        result = remember(
            writer_from(conn, settings, agent_id=payer["id"]),
            org_id=org_id,
            agent_id=payer["id"],
            statement=body.text,
            ref=f"policy:{digest}",
        )
    return {
        "outcome": result.outcome,
        "id": str(result.fact.id) if result.fact else None,
        "reasons": list(result.reasons),
    }


@router.post("/rules/{fact_id}/retire")
def retire_rule(fact_id: UUID, principal: OwnerPrincipal, connection: Connection) -> dict:
    """Take a rule back: kept on record, never recalled again."""
    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.facts set status = 'retired' where id = %s and source = 'owner' "
            "and status <> 'retired' returning claim",
            (str(fact_id),),
        )
        row = cursor.fetchone()
        if row is None:
            raise _bad("No such rule, or already retired", 404)
        cursor.execute(
            "insert into public.events (org_id, type, payload) values (%s, 'rule_retired', %s)",
            (org_id, json.dumps({"fact_id": str(fact_id), "rule": row["claim"]})),
        )
    return {"id": str(fact_id), "status": "retired"}


# --- Knowledge ----------------------------------------------------------------------------


@router.get("/documents")
def documents(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select d.id, d.title, d.scope, d.status, d.screening, d.chunks, d.bytes,
                   d.content_type, d.source_kind, d.created_at,
                   dep.name as department, a.name as agent
              from public.documents d
              left join public.departments dep on dep.id = d.department_id
              left join public.agents a on a.id = d.agent_id
             order by d.created_at desc
            """
        )
        return [
            {
                **{k: v for k, v in r.items() if k not in ("id", "created_at", "screening")},
                "id": str(r["id"]),
                "at": r["created_at"].isoformat(),
                "screening": (r["screening"] or {}).get("outcome")
                if isinstance(r["screening"], dict)
                else r["screening"],
            }
            for r in cursor.fetchall()
        ]


@router.get("/agents/{name}/reads")
def agent_reads(name: str, principal: OwnerPrincipal, connection: Connection) -> dict:
    """What this agent can read: the documents its scope shows it, as RLS
    shows them to it (ADR 015)."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        agent = _agent_row(cursor, name)
    with (
        acting_as(connection, user_id=principal.user_id, agent_id=str(agent["id"])) as conn,
        conn.cursor() as cursor,
    ):
        cursor.execute(
            "select d.title, d.scope from public.documents d where d.status = 'ready' "
            "order by d.scope, d.title"
        )
        docs = [dict(r) for r in cursor.fetchall()]
    return {"agent": name, "documents": docs}


# --- Tasks and the change log ---------------------------------------------------------------


@router.get("/tasks")
def task_trees(
    principal: OwnerPrincipal,
    connection: Connection,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """The latest top-level tasks with their tree's status and cost."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select t.id, t.title, t.status, t.created_at, t.created_by, a.name as agent,
                   coalesce(c.tree_cost_usd, 0) as cost,
                   (select count(*) from public.tasks k where k.root_task_id = t.id) as steps
              from public.tasks t
              join public.agents a on a.id = t.assigned_agent_id
              left join public.task_costs c on c.task_id = t.id
             where t.parent_task_id is null
             order by t.created_at desc limit %s
            """,
            (limit,),
        )
        return [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "status": r["status"],
                "at": r["created_at"].isoformat(),
                "by": str(r["created_by"]) if r["created_by"] else None,
                "agent": r["agent"],
                "cost_usd": _money(r["cost"]),
                "steps": r["steps"],
            }
            for r in cursor.fetchall()
        ]


@router.get("/log")
def change_log(
    principal: OwnerPrincipal,
    connection: Connection,
    limit: Annotated[int, Query(ge=1, le=300)] = 100,
    before: str | None = None,
    prefix: Annotated[str | None, Query(max_length=40)] = None,
) -> list[dict[str, Any]]:
    """What changed, newest first: every audited change, without the work."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select e.id, e.type, e.payload, e.created_at, a.name as agent
              from public.events e left join public.agents a on a.id = e.agent_id
             where not (e.type = any(%s))
               and e.type not like 'task\\_%%' and e.type not like 'run\\_%%'
               and (%s::timestamptz is null or e.created_at < %s::timestamptz)
               and (%s::text is null or e.type like %s || '%%')
             order by e.created_at desc limit %s
            """,
            (list(_WORK), before, before, prefix, prefix, limit),
        )
        return [
            {
                "id": str(r["id"]),
                "type": r["type"],
                "payload": r["payload"],
                "at": r["created_at"].isoformat(),
                "agent": r["agent"],
            }
            for r in cursor.fetchall()
        ]
