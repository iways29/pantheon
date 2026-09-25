"""How a department is doing, against Step 8's "done when" (ADR 024).

For the last N days, from the database alone:

- each morning routine task: its status, what the whole tree cost, its result;
- the department's spend per day against its budget;
- facts the department proposed, by write-gate outcome;
- outside actions (R3 and R4 tools) that ran without an approval: must be 0;
- the owner's own orders to the department, and whether they were done.

`done` applies the plan's test: the routine ran unattended on five mornings
in a row inside the budget, nothing external ran unapproved, and the owner
gave at least one order that was carried out.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from app.db import acting_as
from app.departments.charter import load


def department_report(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    department: str,
    days: int = 5,
    today: date | None = None,
) -> dict[str, Any]:
    with acting_as(connection, user_id=str(user_id)) as conn:
        version, charter = load(conn, org_id=org_id, department=department)
    tz = charter.routine[0].timezone if charter.routine else "UTC"
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute("select (now() at time zone %s)::date as d", (tz,))
        last = today or cursor.fetchone()["d"]
        first = last - timedelta(days=days - 1)
        cursor.execute(
            "select id, daily_budget_usd from public.departments where org_id = %s and name = %s",
            (str(org_id), department),
        )
        dept = cursor.fetchone()
        if dept is None:
            return {"department": department, "error": "not applied yet"}
        span = (tz, first, last)

        cursor.execute(
            """
            select (t.created_at at time zone %s)::date as day, t.id, t.title, t.status,
                   t.result, c.tree_cost_usd
              from public.tasks t
              join public.triggers tr on t.created_by = 'trigger:' || tr.id
              join public.task_costs c on c.task_id = t.id
             where t.department_id = %s and t.parent_task_id is null
               and tr.routine_key like %s
               and (t.created_at at time zone %s)::date between %s and %s
             order by t.created_at
            """,
            (tz, str(dept["id"]), f"{department}:%", *span),
        )
        mornings = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            """
            select (mc.created_at at time zone %s)::date as day, sum(mc.cost_usd) as cost
              from public.model_calls mc join public.agents a on a.id = mc.agent_id
             where a.department_id = %s
               and (mc.created_at at time zone %s)::date between %s and %s
             group by 1
            """,
            (tz, str(dept["id"]), *span),
        )
        spend = {r["day"]: Decimal(r["cost"]) for r in cursor.fetchall()}

        cursor.execute(
            """
            select e.payload->>'outcome' as outcome, count(*) as n
              from public.events e join public.agents a on a.id = e.agent_id
             where e.type = 'fact_write_decided' and a.department_id = %s
               and (e.created_at at time zone %s)::date between %s and %s
             group by 1
            """,
            (str(dept["id"]), tz, first, last),
        )
        facts = {r["outcome"]: r["n"] for r in cursor.fetchall()}

        cursor.execute(
            """
            select count(*) as n
              from public.tool_calls tc
              join public.agents a on a.id = tc.agent_id
              join public.tools tl on tl.org_id = tc.org_id and tl.name = tc.tool
             where a.department_id = %s and tc.status = 'ok'
               and tl.risk_class in ('R3', 'R4')
               and not exists (select 1 from public.approvals ap
                                where ap.tool_call_id = tc.id and ap.status = 'approved')
               and (tc.created_at at time zone %s)::date between %s and %s
            """,
            (str(dept["id"]), tz, first, last),
        )
        unapproved = cursor.fetchone()["n"]

        cursor.execute(
            """
            select count(*) as given, count(*) filter (where status = 'done') as done
              from public.tasks
             where department_id = %s and parent_task_id is null and created_by = 'owner'
               and (created_at at time zone %s)::date between %s and %s
            """,
            (str(dept["id"]), tz, first, last),
        )
        orders = dict(cursor.fetchone())

    budget = Decimal(dept["daily_budget_usd"])
    by_day = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        runs = [m for m in mornings if m["day"] == day]
        cost = spend.get(day, Decimal(0))
        by_day.append(
            {
                "day": day.isoformat(),
                "routine": [
                    {
                        "title": m["title"],
                        "status": m["status"],
                        "tree_cost_usd": str(m["tree_cost_usd"]),
                        "summary": (m["result"] or {}).get("summary"),
                    }
                    for m in runs
                ],
                "spend_usd": str(cost),
                "within_budget": cost <= budget,
                "ok": bool(runs) and all(m["status"] == "done" for m in runs) and cost <= budget,
            }
        )
    streak = 0
    for entry in reversed(by_day):
        if not entry["ok"]:
            break
        streak += 1
    return {
        "department": department,
        "charter_version": version,
        "budget_usd": str(budget),
        "days": by_day,
        "facts": facts,
        "unapproved_outside_actions": unapproved,
        "owner_orders": orders,
        "done": streak >= 5 and unapproved == 0 and orders["done"] >= 1,
        "mornings_in_a_row": streak,
    }
