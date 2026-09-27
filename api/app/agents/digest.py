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

from app.gateway import UpstreamError
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


#: How many findings from the web the brief may list.
NEW_FACTS = 25
#: A digest task with this `kind` is the evening question, not a brief.
EVENING = "evening_question"


def run(session: "Session", run: "_Run") -> dict[str, Any]:
    cursor = session.connection.cursor()
    if _input(cursor, run).get("kind") == EVENING:
        return _evening(cursor, run)
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
    remembered = _remember(session, cursor, run, since)
    items = _items(cursor, run, since)
    policy = load_gate(session.connection, org_id=run.org_id, gate=GATE).policy
    # What the departments found is the point of the brief: it is never cut
    # and always leads; everything else is ranked (idea 6).
    # An alert (the models cannot be reached) comes before everything.
    first = [i for i in items if i["kind"] in ("alert", "findings")]
    others = [i for i in items if i["kind"] not in ("alert", "findings")]
    others = others[: max(int(policy.setting("max_items", 15)) - len(first), 0)]
    ranked = first + _rank(session, run, others, policy)
    lead_count = int(policy.setting("lead_count", 5))
    lead, rest = ranked[:lead_count], ranked[lead_count:]
    try:
        response = session.gateway.complete(
            agent_id=run.agent_id,
            run_id=run.id,
            max_tokens=700,
            messages=[
                {"role": "system", "content": _with_preferences(session, run, prompt["body"])},
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
        text = response.text.strip()
    except UpstreamError as error:
        # No model (out of credit, provider down): the brief still goes out,
        # written from the items as they are, so the owner hears about it.
        text = _plain_brief(lead, rest, error)
    output = {
        "summary": text[:4000],
        "since": since.isoformat(),
        "lead": lead,
        "items": len(ranked),
        "remembered": remembered,
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
            approval_links=True,
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


def _with_preferences(session: "Session", run: "_Run", body: str) -> str:
    """The prompt, with the owner's standing preferences after it (ADR 034)."""
    from app.brain import memory

    extra = memory.preamble(session.connection, run.org_id)
    return f"{body}\n\n{extra}" if extra else body


def _remember(
    session: "Session", cursor: psycopg.Cursor, run: "_Run", since: datetime
) -> dict[str, int]:
    """Teach the brain what the owner did since the last brief (ADR 033)."""
    from app.brain import memory
    from app.brain import policy as brain_policy

    if session.writer is None:
        return {}
    found = memory.moments(
        cursor, org_id=run.org_id, since=since, policy=brain_policy.load(cursor, run.org_id)
    )
    return memory.remember(
        session.writer,
        found,
        org_id=run.org_id,
        agent_id=run.agent_id,
        judge=session.judge,
        connection=session.connection,
        run_id=run.id,
    )


def _input(cursor: psycopg.Cursor, run: "_Run") -> dict[str, Any]:
    if run.task_id is None:
        return {}
    cursor.execute("select input from public.tasks where id = %s", (str(run.task_id),))
    row = cursor.fetchone()
    return dict(row["input"] or {}) if row else {}


def _mailing_list(cursor: psycopg.Cursor, run: "_Run") -> str | None:
    return _input(cursor, run).get("mailing_list")


def _evening(cursor: psycopg.Cursor, run: "_Run") -> dict[str, Any]:
    """Ask the owner what to research tomorrow (Step 8.1b). Fixed text: no
    model call. The answer is a `routine_requests` row, used once."""
    task = _input(cursor, run)
    routine = task.get("routine", "research:morning-brief")
    department = routine.split(":", 1)[0]
    cursor.execute(
        "select task from public.triggers where org_id = %s and routine_key = %s",
        (str(run.org_id), routine),
    )
    trigger = cursor.fetchone()
    topics = list(((trigger or {}).get("task") or {}).get("topics") or [])
    cursor.execute(
        "select request from public.routine_requests where org_id = %s and routine_key = %s "
        "and status = 'pending' order by created_at",
        (str(run.org_id), routine),
    )
    asked = [r["request"] for r in cursor.fetchall()]
    lines = ["Anything you want researched tomorrow morning?", ""]
    lines += ["These run anyway:"] + [f"- {t}" for t in topics] + [""]
    if asked:
        lines += ["Already asked for tomorrow:"] + [f"- {a}" for a in asked] + [""]
    lines += [
        "To add something, run:",
        f'uv run python -m scripts.department ask {department} "your question"',
        "",
        "No answer is fine: the standing topics run as usual.",
    ]
    body = "\n".join(lines)
    output: dict[str, Any] = {"summary": body, "routine": routine, "pending": len(asked)}
    list_key = task.get("mailing_list")
    if list_key:
        email = compose(
            cursor,
            org_id=run.org_id,
            list_key=list_key,
            body=body,
            subject=task.get("subject") or "Tomorrow's research: anything to add?",
            idempotency_key=f"evening:{run.task_id}",
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
    return {"status": "succeeded", "reason": "completed", "output": output, "error": None}


def _since(cursor: psycopg.Cursor, run: "_Run") -> datetime:
    cursor.execute(
        "select max(finished_at) as at from public.tasks where assigned_agent_id = %s "
        "and status = 'done' and id <> %s and coalesce(input->>'kind', '') <> %s",
        (str(run.agent_id), str(run.task_id), EVENING),
    )
    row = cursor.fetchone()
    return row["at"] or datetime.now(UTC) - timedelta(hours=24)


def _items(cursor: psycopg.Cursor, run: "_Run", since: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # Out of model credit stops every department: say so first, and how to fix it.
    cursor.execute(
        """
        select count(*) as n from public.runs r
         where r.org_id = %s and r.updated_at >= %s and r.status in ('paused', 'failed')
           and (r.error ilike '%%returned 402%%' or r.error ilike '%%more credits%%')
        """,
        (str(run.org_id), since),
    )
    if cursor.fetchone()["n"]:
        items.append(
            {
                "kind": "alert",
                "title": "OpenRouter is out of credit: agents cannot run",
                "detail": (
                    "Model calls were refused for lack of credit (402). Add credits at "
                    "openrouter.ai, Settings, Credits, then resume the paused work with "
                    "scripts.approvals resume --reason upstream_error."
                ),
            }
        )
    cursor.execute(
        """
        select ap.action_key, ap.recommendation, ap.explanation, ap.created_at, a.name as agent,
               ap.payload->>'tool' as tool, ap.payload->'arguments' as arguments,
               ap.payload->>'order' as order_text,
               case when ap.action_type = 'fact_write' then ap.payload->>'claim' end
                 as fact_claim
          from public.approvals ap left join public.agents a on a.id = ap.agent_id
         where ap.org_id = %s and ap.status = 'pending'
         order by ap.created_at
        """,
        (str(run.org_id),),
    )
    held_facts: list[str] = []
    for r in cursor.fetchall():
        if r["fact_claim"]:
            held_facts.append(r["fact_claim"])
            continue
        # A held tool call, a routing question, or (a draft) its explanation.
        what = (
            r["order_text"]
            or (r["tool"] and f"{r['tool']} {json.dumps(r['arguments'])[:200]}")
            or r["explanation"]
            or ""
        )
        items.append(
            {
                "kind": "approval",
                "title": f"Waiting for you: {r['action_key']}",
                "agent": r["agent"],
                "detail": what[:400],
                "recommendation": r["recommendation"],
            }
        )
    if held_facts:
        items.append(
            {
                "kind": "approval",
                "title": f"Facts held for your review ({len(held_facts)})",
                "detail": " | ".join(held_facts)[:1500],
                "how": "scripts.brain held, then admit or reject",
            }
        )
    # Problems are one item, counted: a bad day must not crowd out the rest.
    problems: list[str] = []
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
        problems.append(f"{r['agent']} {r['status']}: {r['stop_reason']}")
    # This morning's work that has not finished: a stuck or slow department
    # must reach the owner now, not after it fails.
    cursor.execute(
        """
        select t.title, t.status, a.name as agent
          from public.tasks t join public.agents a on a.id = t.assigned_agent_id
         where t.org_id = %s and t.parent_task_id is null and t.created_at >= %s
           and t.status in ('queued', 'running', 'blocked') and t.id <> %s
         order by t.created_at
        """,
        (str(run.org_id), since, str(run.task_id)),
    )
    for r in cursor.fetchall():
        items.append(
            {
                "kind": "problem",
                "title": f"Not finished yet: {r['title']}",
                "agent": r["agent"],
                "detail": f"Still {r['status']} when the brief was written.",
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
        if r["status"] != "done":
            problems.append(f"{r['title']} {r['status']}: {(r['error'] or '')[:120]}")
            continue
        items.append(
            {
                "kind": "task",
                "title": r["title"],
                "agent": r["agent"],
                "detail": (r["summary"] or "")[:500],
            }
        )
    if problems:
        counted: dict[str, int] = {}
        for problem in problems:
            counted[problem] = counted.get(problem, 0) + 1
        items.append(
            {
                "kind": "problem",
                "title": f"Problems since the last brief ({len(problems)})",
                "detail": " | ".join(f"{p} (x{n})" if n > 1 else p for p, n in counted.items())[
                    :800
                ],
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
    # What was learned, not only how much: what the agents read on the web
    # today, so the brief can name the startups, founders and rounds. These
    # are the day's findings (the pages' claims), not brain facts (ADR 033).
    cursor.execute(
        """
        select c.claim, split_part(split_part(p.final_url, '://', 2), '/', 1) as site
          from public.link_previews p
          cross join lateral jsonb_array_elements_text(p.claims) as c(claim)
         where p.org_id = %s and p.status = 'pushed' and p.label = 'clean'
           and p.pushed_at >= %s
         order by p.pushed_at
        """,
        (str(run.org_id), since),
    )
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in cursor.fetchall():
        key = " ".join(r["claim"].lower().split())
        if key not in seen:
            seen.add(key)
            found.append(r)
    found = found[:NEW_FACTS]
    if found:
        items.append(
            {
                "kind": "findings",
                "title": f"New from your sources ({len(found)})",
                "detail": " | ".join(f"{r['claim']} ({r['site']})" for r in found),
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


def _plain_brief(lead: list[dict[str, Any]], rest: list[dict[str, Any]], error: Exception) -> str:
    """The brief without a model: the ranked items, one line each."""
    lines = [
        f"The brief writer could not reach its model ({str(error)[:120]}), so this is "
        "the plain list.",
        "",
    ]
    for item in lead + rest:
        detail = f": {item['detail']}" if item.get("detail") else ""
        lines.append(f"- {item['title']}{detail}"[:600])
    return "\n".join(lines)
