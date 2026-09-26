"""The `digest` runner: the morning brief and the approvals digest (Step 8.2, ADR 027).

What happened since the last brief, from the database alone:
- orders and routine tasks that finished, with their results;
- approvals waiting for the owner, with Jev's recommendation;
- runs that failed or stopped for a reason the owner should know;
- facts added, rejected and held;
- spend per department against its budget.

Each item is ranked by Jev (`brief_rank`: urgency, impact, needs the owner),
weighted by the gate's settings, and the brief leads with the top items
(right-hand idea 6). One short model call writes the brief from the ranked
items, with the brief writer's own `brief` prompt, which lives in the
database like every prompt.

When the brief's task names a `mailing_list` (the Executive charter's
routine does), the brief is also written as an email to that list (ADR 028):
ready to send if the owner let that list's emails go out on their own,
otherwise held for the owner's approval. It is sent after this run's
transaction commits, never inside it.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg

from app.judge.store import load_gate
from app.mail import compose

if TYPE_CHECKING:
    from app.agents.research import Session
    from app.agents.runs import _Run
    from app.judge.policy import Policy

GATE = "brief_rank"
QUIET_REASONS = (
    "completed",
    "routed",
    "waiting",
    "awaiting_approval",
    "deadline",
    "approved",
    "redirected",
    "resumed",
)


def run(session: "Session", run: "_Run") -> dict[str, Any]:
    cursor = session.connection.cursor()
    cursor.execute(
        "select body from public.agent_prompts where agent_id = %s and slot = 'brief' and active",
        (str(run.agent_id),),
    )
    prompt = cursor.fetchone()
    if prompt is None:
        return {
            "status": "failed",
            "reason": "prompt_missing",
            "error": "The brief writer has no `brief` prompt",
            "output": None,
        }
    since = _since(cursor, run)
    items = _items(cursor, run, since)
    policy = load_gate(session.connection, org_id=run.org_id, gate=GATE).policy
    items = items[: int(policy.setting("max_items", 15))]
    ranked = _rank(session, run, items, policy)
    lead_count = int(policy.setting("lead_count", 5))
    lead, rest = ranked[:lead_count], ranked[lead_count:]
    response = session.gateway.complete(
        agent_id=run.agent_id,
        run_id=run.id,
        max_tokens=700,
        messages=[
            {"role": "system", "content": prompt["body"]},
            {
                "role": "user",
                "content": json.dumps(
                    {"since": since.isoformat(), "lead": lead, "rest": rest},
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
    )
    output = {
        "summary": response.text.strip()[:4000],
        "since": since.isoformat(),
        "lead": lead,
        "items": len(ranked),
    }
    list_key = _mailing_list(cursor, run)
    if list_key:
        email = compose(
            cursor,
            org_id=run.org_id,
            list_key=list_key,
            body=output["summary"],
            idempotency_key=f"brief:{run.task_id}",
            task_id=run.task_id,
            run_id=run.id,
            agent_id=run.agent_id,
        )
        output["email"] = {
            "list": list_key,
            "id": str(email.email_id) if email.email_id else None,
            "status": email.status,
            **({"reason": email.reason} if email.reason else {}),
        }
    cursor.execute(
        "insert into public.events (org_id, run_id, agent_id, type, payload) "
        "values (%s, %s, %s, 'brief_written', %s)",
        (
            str(run.org_id),
            str(run.id),
            str(run.agent_id),
            json.dumps(
                {
                    "items": len(ranked),
                    "lead": [i["title"] for i in lead],
                    "since": since.isoformat(),
                }
            ),
        ),
    )
    return {"status": "succeeded", "reason": "completed", "output": output, "error": None}


def _mailing_list(cursor: psycopg.Cursor, run: "_Run") -> str | None:
    if run.task_id is None:
        return None
    cursor.execute(
        "select input->>'mailing_list' as l from public.tasks where id = %s", (str(run.task_id),)
    )
    row = cursor.fetchone()
    return row["l"] if row else None


def _since(cursor: psycopg.Cursor, run: "_Run") -> datetime:
    cursor.execute(
        "select max(finished_at) as at from public.tasks where assigned_agent_id = %s "
        "and status = 'done' and id <> %s",
        (str(run.agent_id), str(run.task_id)),
    )
    row = cursor.fetchone()
    return row["at"] or datetime.now(UTC) - timedelta(hours=24)


def _items(cursor: psycopg.Cursor, run: "_Run", since: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor.execute(
        """
        select ap.action_key, ap.recommendation, ap.explanation, ap.created_at, a.name as agent,
               ap.payload->>'tool' as tool, ap.payload->'arguments' as arguments,
               ap.payload->>'order' as order_text
          from public.approvals ap left join public.agents a on a.id = ap.agent_id
         where ap.org_id = %s and ap.status = 'pending'
         order by ap.created_at
        """,
        (str(run.org_id),),
    )
    for r in cursor.fetchall():
        what = r["order_text"] or f"{r['tool']} {json.dumps(r['arguments'])[:200]}"
        items.append(
            {
                "kind": "approval",
                "title": f"Waiting for you: {r['action_key']}",
                "agent": r["agent"],
                "detail": what[:400],
                "recommendation": r["recommendation"],
            }
        )
    cursor.execute(
        """
        select r.status, r.stop_reason, r.error, a.name as agent
          from public.runs r join public.agents a on a.id = r.agent_id
         where r.org_id = %s and r.created_at >= %s
           and (r.status = 'failed' or (r.status = 'paused' and not (r.stop_reason = any(%s))))
        """,
        (str(run.org_id), since, list(QUIET_REASONS)),
    )
    for r in cursor.fetchall():
        items.append(
            {
                "kind": "problem",
                "title": f"{r['agent']} {r['status']}: {r['stop_reason']}",
                "agent": r["agent"],
                "detail": (r["error"] or "")[:300],
            }
        )
    cursor.execute(
        """
        select t.title, t.status, t.result->>'summary' as summary, t.error, a.name as agent
          from public.tasks t join public.agents a on a.id = t.assigned_agent_id
         where t.org_id = %s and t.parent_task_id is null and t.finished_at >= %s
           and t.assigned_agent_id <> %s
         order by t.finished_at
        """,
        (str(run.org_id), since, str(run.agent_id)),
    )
    for r in cursor.fetchall():
        items.append(
            {
                "kind": "task",
                "title": f"{r['title']} ({r['status']})",
                "agent": r["agent"],
                "detail": (r["summary"] or r["error"] or "")[:500],
            }
        )
    cursor.execute(
        """
        select e.payload->>'outcome' as outcome, count(*) as n
          from public.events e
         where e.org_id = %s and e.type = 'fact_write_decided' and e.created_at >= %s
         group by 1
        """,
        (str(run.org_id), since),
    )
    facts = {r["outcome"]: r["n"] for r in cursor.fetchall()}
    if facts:
        items.append(
            {
                "kind": "facts",
                "title": "Facts proposed to the brain",
                "detail": ", ".join(f"{n} {o}" for o, n in sorted(facts.items())),
            }
        )
    cursor.execute(
        """
        select d.name, d.daily_budget_usd, coalesce(sum(mc.cost_usd), 0) as spent
          from public.departments d
          left join public.agents a on a.department_id = d.id
          left join public.model_calls mc on mc.agent_id = a.id and mc.created_at >= %s
         where d.org_id = %s
         group by d.id
        """,
        (since, str(run.org_id)),
    )
    for r in cursor.fetchall():
        if r["spent"]:
            items.append(
                {
                    "kind": "spend",
                    "title": f"{r['name']} spent ${r['spent']:.4f}",
                    "detail": f"budget ${r['daily_budget_usd']}/day",
                }
            )
    return items


def _rank(
    session: "Session", run: "_Run", items: list[dict[str, Any]], policy: "Policy"
) -> list[dict[str, Any]]:
    weights = {
        k: float(policy.setting(f"weight_{k}", 1.0)) for k in ("urgency", "impact", "needs_owner")
    }
    scored = []
    for order, item in enumerate(items):
        score = 0.0
        if session.judge is not None:
            decision = session.judge.run(GATE, {"item": item}, agent_id=run.agent_id, run_id=run.id)
            if not decision.failed:
                answers = decision.answers
                score = (
                    weights["urgency"] * answers["urgency"].score
                    + weights["impact"] * answers["impact"].score
                    + weights["needs_owner"] * answers["needs_owner"].noul
                )
        else:
            # Without Jev: waiting approvals and problems first, as gathered.
            score = {"approval": 3, "problem": 2}.get(item["kind"], 0)
        scored.append((score, -order, {**item, "rank_score": round(score, 3)}))
    return [item for _, _, item in sorted(scored, key=lambda s: (s[0], s[1]), reverse=True)]
