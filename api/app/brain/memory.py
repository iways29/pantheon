"""The brain learns from the owner (owner, 2026-09-27; ADR 033, ADR 034).

What the owner says to the agents, and what the owner does, becomes memory
from the owner, through the write gate like any fact:

- an order given to an agent, and a question asked for the next morning's
  research: **sorted by Jev** (`memory_triage`) into a preference, a rule,
  an interest, a fact or a way of working, or forgotten (never asked);
- a draft approved for publishing: kept as something the owner did.

Every agent reads the active preferences before it works (`preferences`),
so "call me boss" is followed from then on. A newer preference that
replaces an older one supersedes it through the write gate's neighbour
check, like any fact.

Picked up by the brief writer each morning, and by the Chief of Staff when
it takes an order (so a preference in an order applies within a minute).
Which sources are remembered is the `brain_policy` flag. Each item is kept
once (idempotent on it).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from app.brain.policy import BrainPolicy
from app.brain.write_gate import BrainWriter, FactCandidate
from app.gateway import GatewayError
from app.judge import JudgeError
from app.judge.store import load_gate

#: At most this many moments at a time: a guard, not a quota.
MAX_MOMENTS = 30
TRIAGE_GATE = "memory_triage"
#: Kinds every agent follows from then on: they need `min_probability_lasting`.
LASTING = ("preference", "rule")
#: How each kind reads as a statement from the owner.
LABELS = {
    "preference": "The owner's preference (from {day}): {said}",
    "rule": "The owner's rule (from {day}): {said}",
    "interest": "On {day} the owner asked about: {said}",
    "fact": "The owner said on {day}: {said}",
    "style": "How the owner works (from {day}): {said}",
}


@dataclass(frozen=True)
class Moment:
    key: str
    #: What is kept when it is not sorted (or cannot be).
    statement: str
    #: The owner's own words, when they are to be sorted by Jev.
    said: str | None = None
    #: Where they were said: an order, a research question.
    where: str | None = None
    when: datetime | None = None


def moments(
    cursor: psycopg.Cursor, *, org_id: UUID, since: datetime, policy: BrainPolicy
) -> list[Moment]:
    """What the owner said and did since `since`."""
    found: list[Moment] = []
    if policy.remember_orders:
        cursor.execute(
            """
            select t.id, t.title, t.instructions, t.created_at
              from public.tasks t
             where t.org_id = %s and t.created_by = 'owner' and t.parent_task_id is null
               and t.created_at >= %s
             order by t.created_at
            """,
            (str(org_id), since),
        )
        found += [order_moment(r) for r in cursor.fetchall()]
    if policy.remember_asks:
        cursor.execute(
            "select id, routine_key, request, created_at from public.routine_requests "
            "where org_id = %s and created_at >= %s and status <> 'cancelled' "
            "order by created_at",
            (str(org_id), since),
        )
        for r in cursor.fetchall():
            department = r["routine_key"].split(":", 1)[0]
            said = _clip(r["request"])
            found.append(
                Moment(
                    f"ask:{r['id']}",
                    f"On {_day(r['created_at'])} the owner asked {department} to find out: {said}",
                    said=said,
                    where=f"a question for tomorrow's {department} research",
                    when=r["created_at"],
                )
            )
    if policy.remember_approved_drafts:
        cursor.execute(
            """
            select ap.id, ap.decided_at, ap.payload->>'channel' as channel,
                   ap.payload->>'title' as title
              from public.approvals ap
             where ap.org_id = %s and ap.action_type = 'draft_review'
               and ap.status = 'approved' and ap.decided_at >= %s
             order by ap.decided_at
            """,
            (str(org_id), since),
        )
        for r in cursor.fetchall():
            found.append(
                Moment(
                    f"draft:{r['id']}",
                    f"On {_day(r['decided_at'])} the owner approved a {r['channel']} draft "
                    f"titled: {_clip(r['title'])}",
                )
            )
    return found[:MAX_MOMENTS]


def order_moment(task: dict[str, Any]) -> Moment:
    """An order from the owner, to be sorted."""
    said = _clip(task.get("instructions") or task.get("title") or "")
    return Moment(
        f"order:{task['id']}",
        f"On {_day(task['created_at'])} the owner ordered: {said}",
        said=said,
        where="an order to the agents",
        when=task["created_at"],
    )


def chat_moment(message: dict[str, Any], agent: str) -> Moment:
    """Something the owner said in the chat (ADR 037), to be sorted."""
    said = _clip(message["body"])
    return Moment(
        f"chat:{message['id']}",
        f"On {_day(message['created_at'])} the owner said to {agent}: {said}",
        said=said,
        where=f"a chat with {agent}",
        when=message["created_at"],
    )


def remember(
    writer: BrainWriter,
    found: list[Moment],
    *,
    org_id: UUID,
    agent_id: UUID,
    judge: Any = None,  # noqa: ANN401 - app.judge.Judge; None: nothing is sorted
    connection: Any = None,  # noqa: ANN401 - for the triage gate's settings
    run_id: UUID | None = None,
) -> dict[str, int]:
    """Keep each moment, sorted where it can be. Returns outcomes counted."""
    counted: dict[str, int] = {}
    minimum = _setting(connection, org_id, "min_probability", 0.5)
    lasting = _setting(connection, org_id, "min_probability_lasting", 0.8)
    for moment in found:
        kind, statement = "fact", moment.statement
        if moment.said and judge is not None:
            sorted_as = _sort(
                judge, moment, agent_id=agent_id, run_id=run_id, minimum=minimum, lasting=lasting
            )
            if sorted_as == "forget":
                counted["forgotten"] = counted.get("forgotten", 0) + 1
                continue
            if sorted_as is not None:
                kind = sorted_as
                statement = LABELS[kind].format(day=_day(moment.when), said=moment.said)
        try:
            result = writer.propose(
                FactCandidate(
                    claim=statement,
                    source="owner",
                    source_text=statement,
                    source_ref=moment.key,
                    kind=kind,
                ),
                org_id=org_id,
                agent_id=agent_id,
                run_id=run_id,
                idempotency_key=f"owner:{moment.key}",
            )
            outcome = result.outcome if kind == "fact" else f"{result.outcome}:{kind}"
        except JudgeError:
            outcome = "not_judged"
        counted[outcome] = counted.get(outcome, 0) + 1
    return counted


def preferences(connection: Any, org_id: UUID | str, limit: int = 20) -> list[str]:  # noqa: ANN401
    """The owner's active preferences, newest first: every agent reads them."""
    rows = connection.execute(
        "select claim from public.facts where org_id = %s and kind = 'preference' "
        "and status = 'active' order by created_at desc limit %s",
        (str(org_id), limit),
    ).fetchall()
    return [r["claim"] for r in rows]


def preamble(connection: Any, org_id: UUID | str) -> str:  # noqa: ANN401
    """The preferences as a short block for an agent's instructions, or ''."""
    limit = int(_setting(connection, org_id, "max_preferences", 20))
    found = preferences(connection, org_id, limit=limit)
    if not found:
        return ""
    lines = "\n".join(f"- {p}" for p in found)
    return f"The owner's standing preferences (follow them):\n{lines}"


def _sort(
    judge: Any,  # noqa: ANN401
    moment: Moment,
    *,
    agent_id: UUID,
    run_id: UUID | None,
    minimum: float,
    lasting: float = 0.8,
) -> str | None:
    """The kind Jev sorts the owner's words into, or None when it cannot."""
    try:
        decision = judge.run(
            TRIAGE_GATE,
            {"said": moment.said, "where": moment.where or ""},
            agent_id=agent_id,
            run_id=run_id,
            input_ref=f"owner:{moment.key}",
        )
    except (JudgeError, GatewayError):
        return None
    if decision.failed:
        return None
    if decision.outcome == "forget":
        return "forget"
    answer = decision.answers["kind"]
    sure = answer.probabilities.get(answer.choice, 0.0)
    if answer.choice in LASTING:
        # A preference or rule steers every agent from then on, so it needs a
        # higher bar (2026-10-10: "don't trouble any agents" for one image, at
        # 0.60, blocked every order after it).
        minimum = max(minimum, lasting)
    if sure < minimum or answer.choice not in LABELS:
        # Unsure: kept as an interest, so the owner's words are never lost.
        return "interest"
    return answer.choice


def _setting(connection: Any, org_id: UUID | str, name: str, default: float) -> float:  # noqa: ANN401
    """A setting of the triage gate, or `default` without one."""
    if connection is None:
        return default
    try:
        return load_gate(connection, org_id=org_id, gate=TRIAGE_GATE).policy.setting(name, default)
    except JudgeError:
        return default


def _day(when: datetime | None) -> str:
    return f"{when:%-d %B %Y}" if when else "an unknown day"


def _clip(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 3] + "..."
