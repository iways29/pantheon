"""The decision desk: what the owner sees with each held action (ADR 021).

Right-hand ideas 1 and 4 (`docs/design/right-hand.md`). For every held
action it gathers, once, at the moment of holding:

- the brain facts nearest to the action (what was checked);
- the owner's most recent decisions on the same kind of action;
- Jev's recommendation (approve, reject, look closer) with its probabilities,
  from the `approval_recommend` gate;
- the owner's earlier decisions and standing rules this action goes against,
  from the `owner_conflict` gate;
- a short plain-English explanation from the agent's own `explain` prompt, if
  the owner gave it one.

A recommendation is advice, never permission: the action stays held until a
person decides. Without TypeSafe the card still has facts and history; its
recommendation is `look_closer`.
"""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.runtime import ToolContext

RECOMMEND_GATE = "approval_recommend"
CONFLICT_GATE = "owner_conflict"
#: The source of facts that record the owner's decisions and standing rules.
OWNER_SOURCE = "owner"


@dataclass(frozen=True)
class Card:
    recommendation: str
    probabilities: dict[str, float] | None = None
    request_id: str | None = None
    explanation: str | None = None
    facts_checked: list[dict[str, Any]] = field(default_factory=list)
    similar_decisions: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def build_card(
    ctx: "ToolContext",
    *,
    tool: str,
    arguments: dict[str, Any],
    risk_class: str,
    reason: str,
    action_key: str,
) -> Card:
    action = {"tool": tool, "arguments": arguments, "risk_class": risk_class, "reason": reason}
    described = _describe(action)
    facts = _facts(ctx, described, source=None, limit=3)
    owner_rules = _facts(ctx, described, source=OWNER_SOURCE, limit=3)
    similar = _similar(ctx, action_key)
    task = _task(ctx)

    recommendation, probabilities, request_id = "look_closer", None, None
    conflicts: list[dict[str, Any]] = []
    if ctx.judge is not None:
        decision = ctx.judge.run(
            RECOMMEND_GATE,
            {
                "action": action,
                "agent": ctx.agent_name,
                "task": task,
                "facts_checked": [f["claim"] for f in facts],
                "similar_decisions": similar,
            },
            agent_id=ctx.agent_id,
            run_id=ctx.run_id,
        )
        if not decision.failed:
            recommendation = decision.outcome
            answer = decision.answers.get("recommendation")
            probabilities = dict(getattr(answer, "probabilities", {}) or {})
            request_id = str(decision.request_id)
        conflicts = _conflicts(ctx, described, owner_rules)

    card = Card(
        recommendation=recommendation,
        probabilities=probabilities,
        request_id=request_id,
        facts_checked=facts,
        similar_decisions=similar,
        conflicts=conflicts,
    )
    explanation = _explain(ctx, action, card)
    return Card(**{**card.__dict__, "explanation": explanation})


def _describe(action: dict[str, Any]) -> str:
    return f"{action['tool']}: {json.dumps(action['arguments'], ensure_ascii=False)}"[:1000]


def _facts(
    ctx: "ToolContext", query: str, *, source: str | None, limit: int
) -> list[dict[str, Any]]:
    if ctx.brain is None:
        return []
    matches = ctx.brain.search(query, limit=limit, source=source)
    return [{"id": str(m.fact.id), "claim": m.fact.claim, "source": m.fact.source} for m in matches]


def _similar(ctx: "ToolContext", action_key: str, limit: int = 5) -> list[dict[str, Any]]:
    """The owner's latest decisions on the same kind of action."""
    with ctx.connection.cursor() as cursor:
        cursor.execute(
            """
            select payload -> 'arguments' as arguments, status, verdict, recommendation
            from public.approvals
            where org_id = %s and action_key = %s and status in ('approved', 'rejected')
            order by decided_at desc
            limit %s
            """,
            (str(ctx.org_id), action_key, limit),
        )
        return [
            {
                "arguments": row["arguments"],
                "decision": row["status"],
                "note": row["verdict"],
                "recommended": row["recommendation"],
            }
            for row in cursor.fetchall()
        ]


def _task(ctx: "ToolContext") -> dict[str, Any] | None:
    task_id = ctx.extras.get("task_id")
    if not task_id:
        return None
    with ctx.connection.cursor() as cursor:
        cursor.execute(
            "select title, instructions from public.tasks where id = %s", (str(task_id),)
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def _conflicts(
    ctx: "ToolContext", proposal: str, owner_rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    found = []
    for rule in owner_rules:
        decision = ctx.judge.run(
            CONFLICT_GATE,
            {"proposal": proposal, "owner_decision": rule["claim"]},
            agent_id=ctx.agent_id,
            run_id=ctx.run_id,
        )
        if decision.failed or decision.outcome != "consistent":
            answer = decision.answers.get("conflicts")
            found.append(
                {
                    **rule,
                    "outcome": "unchecked" if decision.failed else decision.outcome,
                    "probability": getattr(answer, "noul", None),
                }
            )
    return found


def _explain(ctx: "ToolContext", action: dict[str, Any], card: Card) -> str | None:
    """A few sentences for the owner, from the agent's `explain` prompt if it has one.

    The prompt lives in the database like every other; no prompt, no
    explanation (and no model call).
    """
    if ctx.gateway is None:
        return None
    with ctx.connection.cursor() as cursor:
        cursor.execute(
            "select body from public.agent_prompts where agent_id = %s and slot = 'explain' "
            "and active",
            (str(ctx.agent_id),),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    facts = {
        "action": action,
        "recommendation": card.recommendation,
        "conflicts": [c["claim"] for c in card.conflicts],
        "similar_decisions": card.similar_decisions,
    }
    response = ctx.gateway.complete(
        agent_id=ctx.agent_id,
        run_id=ctx.run_id,
        max_tokens=200,
        messages=[
            {"role": "system", "content": row["body"]},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)},
        ],
    )
    return response.text.strip()[:1500] or None
