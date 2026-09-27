"""Checking the workers' results (Step 9, right-hand idea 7; ADR 031).

When a worker's run finishes its task, Jev asks the `result_check` gate
narrow "is this wrong?" questions about the result: no answer, off the task,
names something the agent never read, contradicts what it read. The evidence
is what the run's tools actually returned.

- `pass`: the task finishes as before.
- `redo`, and the gate's settings allow it (the run's tier is in
  `from_tiers`, a stronger tier up to `max_tier` exists, fewer than
  `max_escalations` redos so far): the task goes back to the queue on the
  next tier, with the first attempt and its problems in its input. The run
  ends `succeeded` with stop reason `escalated`, and the task waits for the
  redo.
- `redo` with no redo left: the task finishes, its result marked with the
  problems, so whoever reads it knows.

Every check is a judgment (raw answers kept) and a `result_checked` event,
which is also the run's quality score. Fail open: a check that cannot run
never stops the work.
"""

import json
from typing import Any
from uuid import UUID

import psycopg

from app.db import as_service_role
from app.gateway import GatewayError
from app.gateway.systemone import PROVIDER as SYSTEMONE_PROVIDER
from app.gateway.tiers import TIERS
from app.judge import JudgeError, Policy
from app.judge.store import load_gate

GATE = "result_check"
#: Tools whose output is not evidence about the world.
NOT_EVIDENCE = ("report_result", "create_task")


def check(
    session_factory: Any,  # noqa: ANN401 - a context manager yielding a Session
    connection: psycopg.Connection,
    *,
    run_id: UUID,
    org_id: UUID,
    agent_id: UUID,
    task_id: UUID,
    output: dict[str, Any] | None,
) -> str | None:
    """Check a worker's finished task. Returns `escalated` when the task was
    sent back for a redo, else None (the run finishes as it would have)."""
    with as_service_role(connection) as conn:
        found = conn.execute(
            """
            select t.title, t.instructions, t.result, t.escalations, t.input,
                   a.role_type, coalesce(r.model_tier, a.model_tier) as tier,
                   (select x.id from public.runs x where x.task_id = t.id
                     order by x.created_at desc limit 1) as latest_run
              from public.tasks t
              join public.agents a on a.id = t.assigned_agent_id
              join public.runs r on r.id = %s
             where t.id = %s and t.status = 'running'
            """,
            (str(run_id), str(task_id)),
        ).fetchone()
    # Workers only (heads are judged by their workers' results), and only the
    # task's latest run: a crash and replay must not check twice.
    if found is None or found["role_type"] != "worker" or found["latest_run"] != run_id:
        return None
    try:
        with as_service_role(connection) as conn:
            gate = load_gate(conn, org_id=org_id, gate=GATE)
    except JudgeError:
        return None  # not seeded: nothing to check with
    policy = gate.policy
    result = found["result"] or output or {}
    state = {
        "task": " ".join(filter(None, [found["title"], found["instructions"]])),
        "result": _text(result),
        "evidence": _evidence(connection, run_id, int(policy.setting("evidence_chars", 24000))),
    }
    # Escaping can grow the evidence: trim it until the state fits the gate.
    while (
        state["evidence"]
        and len(json.dumps(state, ensure_ascii=False)) > gate.config.max_state_chars
    ):
        state["evidence"] = state["evidence"][: int(len(state["evidence"]) * 0.9)]
    try:
        with session_factory() as session:
            if session.judge is None:
                return None
            decision = session.judge.run(
                GATE, state, agent_id=agent_id, run_id=run_id, input_ref=f"task:{task_id}"
            )
    except (JudgeError, GatewayError):
        return None
    problems = [reason.text for reason in decision.reasons] if decision.outcome == "redo" else []
    next_tier = _next_tier(found["tier"], policy)
    escalate = (
        decision.outcome == "redo"
        and not decision.failed
        and next_tier is not None
        and found["escalations"] < int(policy.setting("max_escalations", 1))
    )
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        if escalate:
            cursor.execute(
                """
                update public.tasks
                   set model_tier = %s, escalations = escalations + 1, status = 'queued',
                       result = null, input = input || %s
                 where id = %s and status = 'running'
                """,
                (
                    next_tier,
                    json.dumps(
                        {
                            "previous_attempt": {
                                "tier": found["tier"],
                                "result": result,
                                "problems": problems,
                            }
                        },
                        default=str,
                    ),
                    str(task_id),
                ),
            )
        elif problems:
            cursor.execute(
                "update public.tasks set result = %s where id = %s",
                (
                    json.dumps({**_as_dict(result), "check": {"problems": problems}}, default=str),
                    str(task_id),
                ),
            )
        cursor.execute(
            "insert into public.events (org_id, run_id, agent_id, type, payload) "
            "values (%s, %s, %s, 'result_checked', %s)",
            (
                str(org_id),
                str(run_id),
                str(agent_id),
                json.dumps(
                    {
                        "task_id": str(task_id),
                        "outcome": decision.outcome,
                        "failed": decision.failed,
                        "problems": problems,
                        "tier": found["tier"],
                        "escalated_to": next_tier if escalate else None,
                        "escalations": found["escalations"] + (1 if escalate else 0),
                        "request_id": str(decision.request_id) if decision.request_id else None,
                    }
                ),
            ),
        )
    return "escalated" if escalate else None


def _next_tier(tier: str, policy: Policy) -> str | None:
    """The next tier up, if `tier`'s results may be sent back and it is below
    the gate's `max_tier`."""
    allowed = {t.strip() for t in (policy.text_setting("from_tiers", "cheap") or "").split(",")}
    top = policy.text_setting("max_tier", "standard")
    if tier not in allowed or tier not in TIERS or top not in TIERS:
        return None
    index = TIERS.index(tier) + 1
    return TIERS[index] if index <= TIERS.index(top) else None


def _evidence(connection: psycopg.Connection, run_id: UUID, limit: int) -> str:
    """What the run's tools returned, in order, cut to `limit` characters."""
    with as_service_role(connection) as conn:
        rows = conn.execute(
            "select tool, arguments, result from public.tool_calls "
            "where run_id = %s and status = 'ok' and not (tool = any(%s)) order by created_at",
            (str(run_id), list(NOT_EVIDENCE)),
        ).fetchall()
    parts = [
        f"[{r['tool']} {json.dumps(r['arguments'], ensure_ascii=False)[:200]}]\n"
        f"{json.dumps(r['result'], ensure_ascii=False, default=str)}"
        for r in rows
    ]
    text = "\n\n".join(parts)
    return text[:limit] if text else "(the agent read nothing)"


def _text(result: Any) -> str:  # noqa: ANN401
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)[:4000]
    return str(result)[:4000]


def _as_dict(result: Any) -> dict[str, Any]:  # noqa: ANN401
    return dict(result) if isinstance(result, dict) else {"summary": str(result)}


def summary(
    connection: psycopg.Connection, *, user_id: UUID | str, org_id: UUID | str, days: int = 7
) -> dict[str, Any]:
    """How the checks did over the last `days` days, and what they cost.

    Model cost is split into the first attempts, the redos, and Jev's checks.
    `standard_vs_cheap` is what a redo cost against the attempt it replaced,
    averaged over the redos; `always_standard_estimate_usd` applies it to
    every checked first attempt, the cost of skipping the cheap tier.
    """
    from app.db import acting_as

    with acting_as(connection, user_id=str(user_id)) as conn:
        rows = conn.execute(
            """
            select e.run_id, e.payload, r.cost_usd,
                   coalesce((select sum(mc.cost_usd) from public.model_calls mc
                              where mc.run_id = e.run_id and mc.provider = %s), 0)
                     as check_cost
              from public.events e join public.runs r on r.id = e.run_id
             where e.org_id = %s and e.type = 'result_checked'
               and e.created_at >= now() - make_interval(days => %s)
             order by e.created_at
            """,
            (SYSTEMONE_PROVIDER, str(org_id), days),
        ).fetchall()
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(row["payload"]["task_id"], []).append(row)
    attempts_cost = sum(float(r["cost_usd"]) - float(r["check_cost"]) for r in rows)
    first_cost = 0.0
    redo_cost = 0.0
    ratios: list[float] = []
    for attempts in by_task.values():
        head, *redos = attempts
        head_cost = float(head["cost_usd"]) - float(head["check_cost"])
        first_cost += head_cost
        for redo in redos:
            cost = float(redo["cost_usd"]) - float(redo["check_cost"])
            redo_cost += cost
            if head_cost > 0:
                ratios.append(cost / head_cost)
    ratio = sum(ratios) / len(ratios) if ratios else None
    last = [attempts[-1]["payload"] for attempts in by_task.values()]
    return {
        "days": days,
        "tasks_checked": len(by_task),
        "checks": len(rows),
        "passed_first_time": sum(
            1 for a in by_task.values() if a[0]["payload"]["outcome"] == "pass"
        ),
        "redone": sum(1 for a in by_task.values() if len(a) > 1),
        "still_flagged": sum(1 for p in last if p["outcome"] == "redo"),
        "check_failed": sum(1 for r in rows if r["payload"].get("failed")),
        "problems": _count(p for r in rows for p in r["payload"]["problems"]),
        "cost_usd": {
            "first_attempts": round(first_cost, 6),
            "redos": round(redo_cost, 6),
            "checks": round(sum(float(r["check_cost"]) for r in rows), 6),
            "total_work": round(attempts_cost, 6),
        },
        "standard_vs_cheap": round(ratio, 2) if ratio is not None else None,
        "always_standard_estimate_usd": round(first_cost * ratio, 6) if ratio else None,
    }


def _count(items: Any) -> dict[str, int]:  # noqa: ANN401 - an iterable of text
    counted: dict[str, int] = {}
    for item in items:
        counted[item] = counted.get(item, 0) + 1
    return counted
