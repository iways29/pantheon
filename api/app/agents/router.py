"""The `router` runner: the Chief of Staff routes the owner's orders (Step 8.2, ADR 027).

No model loop: routing is a Jev decision, logged like every judgment.

First run on an order:
1. The departments that can take work are read from their charters (live,
   final, head switched on); their purposes are the options (`route_order`).
2. The order is checked against the owner's earlier decisions and standing
   rules (right-hand idea 4, `owner_conflict`).
3. A clear routing hands the order to that department's head as a sub-task,
   with the tier its complexity suggests; the order waits for it.
4. An unclear one, one only the owner can settle, or one that goes against
   an owner decision comes back as a question (right-hand idea 5): an
   approval card with the likeliest departments. The owner approves the
   suggestion, picks another (edited `department`), cancels, or redirects
   with a note, which is routed again with the note.

When the department has finished, the next run records its result as the
order's result.
"""

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg

from app.approvals.desk import _conflicts, _facts
from app.judge.store import load_gate
from app.tasks import children, delegate, report_result

if TYPE_CHECKING:
    from app.agents.research import Session
    from app.agents.runs import _Run

GATE = "route_order"
MAX_QUESTIONS = 3
_LEVELS = ("simple", "moderate", "complex")


def run(session: "Session", run: "_Run") -> dict[str, Any]:
    if run.task_id is None:
        return _stop("failed", "no_task", error="The Chief of Staff routes orders given as tasks")
    cursor = session.connection.cursor()
    task = _task(cursor, run.task_id)
    subtasks = children(cursor, run.task_id)
    if subtasks:
        return _finish(cursor, run, subtasks)

    asked = _questions(cursor, run.task_id)
    last = asked[0] if asked else None
    order = _order_text(task)
    if last is not None:
        if last["status"] == "pending":
            return _stop("paused", "awaiting_approval")
        if last["status"] == "approved":
            chosen = (last["edited_payload"] or {}).get("department") or last["payload"].get(
                "recommended"
            )
            return _route(
                cursor,
                run,
                task,
                chosen,
                last["payload"].get("suggested_tier"),
                f"the owner chose {chosen}",
            )
        # Redirected: route again with the owner's note.
        order = f"{order}\n\nThe owner's note: {last['verdict']}"
        if len(asked) >= MAX_QUESTIONS:
            return _stop(
                "failed",
                "unroutable",
                error=f"Still unclear after {len(asked)} questions to the owner",
            )

    departments = _departments(cursor, run)
    if not departments:
        return _stop("failed", "no_departments", error="No department is set up to take work")
    ctx = SimpleNamespace(
        connection=session.connection,
        org_id=run.org_id,
        agent_id=run.agent_id,
        run_id=run.id,
        judge=session.judge,
        brain=session.brain,
    )
    settings = load_gate(session.connection, org_id=run.org_id, gate=GATE).policy
    conflicts = []
    if session.judge is not None:
        rules = _facts(ctx, order, source="owner", limit=int(settings.setting("owner_facts", 3)))
        conflicts = _conflicts(ctx, order, rules)

    options = {d["department"]: d["purpose"] for d in departments}
    decision = (
        session.judge.run(
            GATE,
            {"order": order, "departments": options},
            agent_id=run.agent_id,
            run_id=run.id,
            extra_options={"department": options},
        )
        if session.judge is not None
        else None
    )
    probs: dict[str, float] = {}
    chosen, tier, reasons = None, None, ["TypeSafe is not configured"]
    if decision is not None and not decision.failed:
        answer = decision.answers["department"]
        probs = dict(answer.probabilities)
        chosen = answer.choice
        level = min(max(round(decision.answers["complexity"].score), 0), 2)
        tier = settings.text_setting(f"tier_{_LEVELS[level]}")
        reasons = [r.text for r in decision.reasons]
    elif decision is not None:
        reasons = [r.text for r in decision.reasons]

    clear = decision is not None and decision.outcome == "route" and chosen in options
    if clear and not conflicts:
        return _route(cursor, run, task, chosen, tier, "clear routing")
    return _ask(
        cursor,
        run,
        task,
        order,
        options,
        probs,
        chosen,
        tier,
        reasons,
        conflicts,
        attempt=len(asked) + 1,
    )


# --- steps ---------------------------------------------------------------------------


def _route(
    cursor: psycopg.Cursor,
    run: "_Run",
    task: dict[str, Any],
    department: str | None,
    tier: str | None,
    why: str,
) -> dict[str, Any]:
    head = next(
        (d["head"] for d in _departments(cursor, run) if d["department"] == department), None
    )
    if head is None:
        return _stop("failed", "unroutable", error=f"No department {department!r} to route to")
    sub = delegate(
        cursor,
        org_id=run.org_id,
        parent_task_id=run.task_id,
        by_agent_id=run.agent_id,
        to_agent=head,
        title=task["title"],
        instructions=task["instructions"] or task["title"],
        input={
            **(task["input"] or {}),
            "routed_by": "chief-of-staff",
            **({"suggested_tier": tier} if tier else {}),
        },
    )
    _event(
        cursor,
        run,
        "order_routed",
        {
            "task_id": str(run.task_id),
            "department": department,
            "head": head,
            "subtask_id": str(sub.id),
            "suggested_tier": tier,
            "why": why,
        },
    )
    # No output yet: the order's result is the department's, recorded later.
    return _stop("succeeded", "routed")


def _ask(
    cursor: psycopg.Cursor,
    run: "_Run",
    task: dict[str, Any],
    order: str,
    options: dict[str, str],
    probs: dict[str, float],
    chosen: str | None,
    tier: str | None,
    reasons: list[str],
    conflicts: list[dict[str, Any]],
    attempt: int,
) -> dict[str, Any]:
    ranked = sorted(((d, probs.get(d, 0.0)) for d in options), key=lambda p: -p[1])[:3]
    recommended = chosen if chosen in options else (ranked[0][0] if ranked else None)
    why = "; ".join(reasons + [f"goes against: {c['claim']}" for c in conflicts])
    cursor.execute(
        """
        insert into public.approvals
            (org_id, run_id, agent_id, task_id, action_type, action_key, payload,
             agent_output_snapshot, idempotency_key, recommendation, recommendation_probs,
             explanation, conflicts)
        values (%s, %s, %s, %s, 'route_order', 'route_order', %s, %s, %s, 'look_closer', %s,
                %s, %s)
        on conflict (org_id, idempotency_key) where idempotency_key is not null do nothing
        """,
        (
            str(run.org_id),
            str(run.id),
            str(run.agent_id),
            str(run.task_id),
            json.dumps(
                {
                    "order": order,
                    "recommended": recommended,
                    "suggested_tier": tier,
                    "options": [{"department": d, "probability": round(p, 3)} for d, p in ranked],
                }
            ),
            json.dumps({"order": order}),
            f"route:{run.task_id}:{attempt}",
            json.dumps(probs),
            f"Which department should do this? {why}"[:1500],
            json.dumps(conflicts),
        ),
    )
    cursor.execute(
        "update public.tasks set status = 'awaiting_approval' where id = %s and status = 'running'",
        (str(run.task_id),),
    )
    _event(
        cursor,
        run,
        "order_question",
        {"task_id": str(run.task_id), "recommended": recommended, "why": why[:500]},
    )
    return _stop("paused", "awaiting_approval")


def _finish(cursor: psycopg.Cursor, run: "_Run", subtasks: list[dict[str, Any]]) -> dict[str, Any]:
    open_ = [s for s in subtasks if s["status"] not in ("done", "failed", "cancelled")]
    if open_:
        return _stop("succeeded", "waiting")
    parts = []
    for sub in subtasks:
        summary = (sub["result"] or {}).get("summary") or sub["error"] or sub["status"]
        parts.append(f"{sub['agent']}: {summary}")
    result = {"summary": " | ".join(parts)[:4000], "done_by": [s["agent"] for s in subtasks]}
    report_result(cursor, task_id=run.task_id, agent_id=run.agent_id, result=result)
    return _stop("succeeded", "completed", output=result)


# --- reading ---------------------------------------------------------------------------


def _task(cursor: psycopg.Cursor, task_id: UUID) -> dict[str, Any]:
    cursor.execute(
        "select title, instructions, input from public.tasks where id = %s", (str(task_id),)
    )
    return dict(cursor.fetchone())


def _order_text(task: dict[str, Any]) -> str:
    return f"{task['title']}\n{task['instructions'] or ''}".strip()


def _questions(cursor: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    cursor.execute(
        "select status, payload, edited_payload, verdict from public.approvals "
        "where task_id = %s and action_type = 'route_order' order by created_at desc",
        (str(task_id),),
    )
    return [dict(r) for r in cursor.fetchall()]


def _departments(cursor: psycopg.Cursor, run: "_Run") -> list[dict[str, Any]]:
    """Departments that can take work now: a live, final charter whose head is on."""
    cursor.execute(
        """
        select c.department, c.charter->>'purpose' as purpose,
               c.charter->'head'->>'name' as head
          from public.department_charters c
          join public.agents h on h.org_id = c.org_id
                              and h.name = c.charter->'head'->>'name' and h.enabled
          join public.departments d on d.org_id = c.org_id and d.name = c.department
                                   and d.enabled
         where c.org_id = %s and c.active
           and not coalesce((c.charter->>'draft')::boolean, false)
           and h.id <> %s
         order by c.department
        """,
        (str(run.org_id), str(run.agent_id)),
    )
    return [dict(r) for r in cursor.fetchall()]


def _event(cursor: psycopg.Cursor, run: "_Run", kind: str, payload: dict[str, Any]) -> None:
    cursor.execute(
        "insert into public.events (org_id, run_id, agent_id, type, payload) "
        "values (%s, %s, %s, %s, %s)",
        (str(run.org_id), str(run.id), str(run.agent_id), kind, json.dumps(payload)),
    )


def _stop(
    status: str, reason: str, *, output: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    return {"status": status, "reason": reason, "output": output, "error": error}
