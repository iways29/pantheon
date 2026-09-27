"""Re-ranking what an agent recalls, before it reads it (Step 9, ADR 032).

Vector search finds facts near a query; near is not the same as useful. When
the `recall_rank` gate exists, `brain_search` fetches more candidates than it
was asked for and Jev judges each against the query: how much it helps answer
it (a Score), and whether it contradicts what the query takes for granted.
Facts judged unrelated are dropped; the rest are ordered by relevance, less a
penalty for a fact past its review date (decided here, not by Jev), and the
agent gets the number it asked for, each marked `stale` or
`contradicts_premise` where that applies.

Fail open: with no gate, no judge, or a judgment that fails, the agent gets
the plain vector order, as before.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from app.brain.store import FactMatch
from app.gateway import GatewayError
from app.judge import JudgeError, Policy
from app.judge.store import load_gate

GATE = "recall_rank"


@dataclass(frozen=True)
class Ranked:
    match: FactMatch
    relevance: float | None = None
    stale: bool = False
    contradicts_premise: bool = False


def policy_for(connection: Any, org_id: UUID | str) -> Policy | None:  # noqa: ANN401
    """The gate's policy, or None when the org has no such gate."""
    try:
        return load_gate(connection, org_id=org_id, gate=GATE).policy
    except JudgeError:
        return None


def candidates(policy: Policy, limit: int) -> int:
    factor = policy.setting("candidates_factor", 2)
    return max(limit, min(int(limit * factor), int(policy.setting("max_candidates", 20))))


def rerank(
    judge: Any,  # noqa: ANN401 - app.judge.Judge
    policy: Policy,
    matches: list[FactMatch],
    *,
    query: str,
    limit: int,
    agent_id: UUID | str,
    run_id: UUID | str | None = None,
    today: date | None = None,
) -> list[Ranked]:
    today = today or date.today()
    penalty = policy.setting("stale_penalty", 0.5)
    flag_at = policy.setting("premise_flag_at", 0.5)
    ranked: list[tuple[float, float, Ranked]] = []
    for match in matches:
        stale = match.fact.review_after is not None and match.fact.review_after < today
        try:
            decision = judge.run(
                GATE,
                {"query": query, "claim": match.fact.claim},
                agent_id=agent_id,
                run_id=run_id,
                input_ref=f"fact:{match.fact.id}",
            )
        except (JudgeError, GatewayError):
            return [Ranked(m) for m in matches[:limit]]
        if decision.failed:
            return [Ranked(m) for m in matches[:limit]]
        if decision.outcome == "drop":
            continue
        relevance = decision.answers["relevance"].score
        item = Ranked(
            match,
            relevance=round(relevance, 3),
            stale=stale,
            contradicts_premise=decision.answers["contradicts_premise"].noul >= flag_at,
        )
        ranked.append((relevance - (penalty if stale else 0.0), -match.distance, item))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [item for _, _, item in ranked[:limit]]
