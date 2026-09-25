"""Making a department match its charter (Step 8.0, ADR 024).

`apply_charter` reconciles, in one transaction acting as the owner:

- the department row and its daily budget;
- each agent in the charter: created switched off if missing, otherwise its
  role, tier, runner, tools, autonomy level and caps updated to match; a
  prompt slot gets a new version only when the charter's text differs from
  the live one;
- each routine item: a trigger keyed by `routine_key`, created switched off,
  otherwise updated (its on/off state is left as the owner set it); a
  trigger whose item left the charter is switched off, never deleted.

Nothing is switched on here. `enable` does that, after the owner's review.
Every change is audited by the tables' own triggers, and the whole apply is
one `charter_applied` event.
"""

import json
from dataclasses import dataclass, field
from datetime import time
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.agents.admin import AgentSpec, create_agent
from app.db import acting_as
from app.departments.charter import AgentPlan, Charter, CharterError, load


@dataclass
class ApplyReport:
    department: str
    version: int
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    switched_off: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "version": self.version,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "switched_off": self.switched_off,
        }


def apply_charter(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    department: str,
) -> ApplyReport:
    import app.tools.builtin  # noqa: F401 - registers the built-in tools
    from app.tools import REGISTRY, seed_tools

    with acting_as(connection, user_id=str(user_id)) as conn:
        version, charter = load(conn, org_id=org_id, department=department)
    if charter.draft:
        raise CharterError(f"The {department!r} charter is a draft; publish it as final first")
    seed_tools(connection, user_id=user_id, org_id=org_id)
    report = ApplyReport(department, version)

    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        missing_tools = sorted({t for a in charter.agents for t in a.allowed_tools} - set(REGISTRY))
        if missing_tools:
            raise CharterError(f"Tools not built yet: {', '.join(missing_tools)}")
        cursor.execute(
            "select gate from public.judge_gates where org_id = %s and active and gate = any(%s)",
            (str(org_id), charter.gates),
        )
        missing_gates = sorted(set(charter.gates) - {r["gate"] for r in cursor.fetchall()})
        if missing_gates:
            raise CharterError(f"Gates not set up: {', '.join(missing_gates)}")

        cursor.execute(
            "select id, daily_budget_usd from public.departments where org_id = %s and name = %s",
            (str(org_id), department),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "insert into public.departments (org_id, name, daily_budget_usd) "
                "values (%s, %s, %s)",
                (str(org_id), department, charter.daily_budget_usd),
            )
            report.created.append(f"department {department}")
        elif row["daily_budget_usd"] != charter.daily_budget_usd:
            cursor.execute(
                "update public.departments set daily_budget_usd = %s where id = %s",
                (charter.daily_budget_usd, str(row["id"])),
            )
            report.updated.append(f"department {department} budget")

    for plan in charter.agents:
        _agent(connection, user_id, org_id, department, charter, plan, report)

    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        keys = []
        for item in charter.routine:
            key = f"{department}:{item.key}"
            keys.append(key)
            _routine(cursor, org_id, user_id, key, item, report)
        cursor.execute(
            "update public.triggers set enabled = false "
            "where org_id = %s and routine_key like %s and not (routine_key = any(%s)) "
            "and enabled returning name",
            (str(org_id), f"{department}:%", keys),
        )
        report.switched_off += [f"trigger {r['name']}" for r in cursor.fetchall()]
        cursor.execute(
            "insert into public.events (org_id, type, payload) values (%s, 'charter_applied', %s)",
            (str(org_id), json.dumps(report.summary())),
        )
    return report


def enable(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    department: str,
    on: bool = True,
) -> list[str]:
    """Switch a department's charter agents and routine on (or off) together."""
    with acting_as(connection, user_id=str(user_id)) as conn:
        _, charter = load(conn, org_id=org_id, department=department)
    names = [a.name for a in charter.agents]
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.agents set enabled = %s where org_id = %s and name = any(%s) "
            "and enabled <> %s returning name",
            (on, str(org_id), names, on),
        )
        changed = [f"agent {r['name']}" for r in cursor.fetchall()]
        cursor.execute(
            "update public.triggers set enabled = %s where org_id = %s and routine_key = any(%s) "
            "and enabled <> %s returning name",
            (on, str(org_id), [f"{department}:{i.key}" for i in charter.routine], on),
        )
        changed += [f"trigger {r['name']}" for r in cursor.fetchall()]
    return changed


def _agent(
    connection: psycopg.Connection,
    user_id: UUID | str,
    org_id: UUID | str,
    department: str,
    charter: Charter,
    plan: AgentPlan,
    report: ApplyReport,
) -> None:
    is_head = plan.name == charter.head.name
    spec = AgentSpec(
        department=department,
        name=plan.name,
        role=plan.role,
        tier=plan.tier,
        daily_budget_usd=plan.daily_budget_usd,
        prompts=plan.prompts,
        allowed_tools=plan.allowed_tools,
        parent=None if is_head else charter.head.name,
        role_type="head" if is_head else "worker",
        runner=plan.runner,
        autonomy_level=plan.autonomy_level or charter.autonomy_level,
        max_children=plan.max_children,
    )
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select a.*, d.name as department_name, p.name as parent_name "
            "from public.agents a join public.departments d on d.id = a.department_id "
            "left join public.agents p on p.id = a.parent_agent_id "
            "where a.org_id = %s and a.name = %s",
            (str(org_id), plan.name),
        )
        current = cursor.fetchone()
    if current is None:
        create_agent(connection, user_id=user_id, org_id=org_id, spec=spec)
        report.created.append(f"agent {plan.name}")
        return

    changes: list[str] = []
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        if current["department_name"] != department:
            raise CharterError(
                f"Agent {plan.name!r} belongs to {current['department_name']!r}; "
                "a charter does not move agents between departments"
            )
        wanted = {
            "role": spec.role,
            "model_tier": spec.tier,
            "runner": spec.runner,
            "role_type": spec.role_type,
            "allowed_tools": list(spec.allowed_tools),
            "autonomy_level": spec.autonomy_level,
            "max_children": spec.max_children,
            "daily_budget_usd": spec.daily_budget_usd,
        }
        differs = {
            k: v
            for k, v in wanted.items()
            if (list(current[k]) if k == "allowed_tools" else current[k]) != v
        }
        if spec.parent != current["parent_name"]:
            cursor.execute(
                "select id from public.agents where org_id = %s and name = %s",
                (str(org_id), spec.parent),
            )
            parent = cursor.fetchone() if spec.parent else None
            differs["parent_agent_id"] = str(parent["id"]) if parent else None
        if differs:
            cursor.execute(
                f"update public.agents set {', '.join(f'{k} = %s' for k in differs)} where id = %s",
                (*differs.values(), str(current["id"])),
            )
            changes += sorted(differs)
        for slot, text in sorted(plan.prompts.items()):
            cursor.execute(
                "select body from public.agent_prompts where agent_id = %s and slot = %s "
                "and active",
                (str(current["id"]), slot),
            )
            live = cursor.fetchone()
            if live is None or live["body"] != text:
                cursor.execute(
                    "select 1 from public.publish_agent_prompt(%s, %s, %s, %s)",
                    (str(current["id"]), slot, text, f"From the {department} charter"),
                )
                changes.append(f"prompt {slot}")
    if changes:
        report.updated.append(f"agent {plan.name} ({', '.join(changes)})")
    else:
        report.unchanged.append(f"agent {plan.name}")


def _routine(
    cursor: psycopg.Cursor,
    org_id: UUID | str,
    user_id: UUID | str,
    key: str,
    item: Any,  # noqa: ANN401 - a RoutineItem
    report: ApplyReport,
) -> None:
    cursor.execute(
        "select id from public.agents where org_id = %s and name = %s", (str(org_id), item.agent)
    )
    agent_id = cursor.fetchone()["id"]
    task = {**item.input, "instructions": item.instructions}
    values = {
        "agent_id": str(agent_id),
        "name": item.title,
        "task": Jsonb(task),
        "time_of_day": time.fromisoformat(item.time),
        "days_of_week": item.days,
        "timezone": item.timezone,
        "max_steps": item.max_steps,
        "max_tokens": item.max_tokens,
    }
    cursor.execute(
        "select id, agent_id, name, task, time_of_day, days_of_week, timezone, max_steps, "
        "max_tokens from public.triggers where org_id = %s and routine_key = %s",
        (str(org_id), key),
    )
    current = cursor.fetchone()
    if current is None:
        cursor.execute(
            "insert into public.triggers (org_id, routine_key, run_as, "
            f"{', '.join(values)}) values (%s, %s, %s, {', '.join(['%s'] * len(values))})",
            (str(org_id), key, str(user_id), *values.values()),
        )
        report.created.append(f"trigger {item.title}")
        return
    now = {
        "agent_id": str(current["agent_id"]),
        "name": current["name"],
        "task": current["task"],
        "time_of_day": current["time_of_day"],
        "days_of_week": list(current["days_of_week"]),
        "timezone": current["timezone"],
        "max_steps": current["max_steps"],
        "max_tokens": current["max_tokens"],
    }
    compare = {**values, "task": task}
    differs = {k: values[k] for k in values if now[k] != compare[k]}
    if differs:
        cursor.execute(
            f"update public.triggers set {', '.join(f'{k} = %s' for k in differs)} where id = %s",
            (*differs.values(), str(current["id"])),
        )
        report.updated.append(f"trigger {item.title} ({', '.join(sorted(differs))})")
    else:
        report.unchanged.append(f"trigger {item.title}")
