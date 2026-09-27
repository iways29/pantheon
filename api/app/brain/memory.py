"""The brain learns from what the owner does (owner, 2026-09-27; ADR 033).

Each morning, before the brief is written, the brief writer turns what the
owner did since the last brief into facts from the owner, through the write
gate like any fact:

- an order given to an agent;
- a question asked for the next morning's research;
- a draft approved for publishing.

Each is one short, dated statement in the owner's own words, so it stands
on its own and is supported by itself. Which kinds are remembered is the
`brain_policy` flag. Written once each (idempotent on the item).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from app.brain.policy import BrainPolicy
from app.brain.write_gate import BrainWriter, FactCandidate
from app.judge import JudgeError

#: At most this many moments a morning: a guard, not a quota.
MAX_MOMENTS = 30


@dataclass(frozen=True)
class Moment:
    key: str
    statement: str


def moments(
    cursor: psycopg.Cursor, *, org_id: UUID, since: datetime, policy: BrainPolicy
) -> list[Moment]:
    """What the owner did since `since`, as statements to remember."""
    found: list[Moment] = []
    if policy.remember_orders:
        cursor.execute(
            """
            select t.id, t.title, t.instructions, t.created_at, a.name as agent
              from public.tasks t join public.agents a on a.id = t.assigned_agent_id
             where t.org_id = %s and t.created_by = 'owner' and t.parent_task_id is null
               and t.created_at >= %s
             order by t.created_at
            """,
            (str(org_id), since),
        )
        for r in cursor.fetchall():
            what = _clip(r["instructions"] or r["title"])
            found.append(
                Moment(f"order:{r['id']}", f"On {_day(r['created_at'])} the owner ordered: {what}")
            )
    if policy.remember_asks:
        cursor.execute(
            "select id, routine_key, request, created_at from public.routine_requests "
            "where org_id = %s and created_at >= %s and status <> 'cancelled' "
            "order by created_at",
            (str(org_id), since),
        )
        for r in cursor.fetchall():
            department = r["routine_key"].split(":", 1)[0]
            found.append(
                Moment(
                    f"ask:{r['id']}",
                    f"On {_day(r['created_at'])} the owner asked {department} to find out: "
                    f"{_clip(r['request'])}",
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


def remember(
    writer: BrainWriter, found: list[Moment], *, org_id: UUID, agent_id: UUID
) -> dict[str, int]:
    """Write each moment as a fact from the owner. Returns outcomes counted."""
    counted: dict[str, int] = {}
    for moment in found:
        try:
            result = writer.propose(
                FactCandidate(
                    claim=moment.statement,
                    source="owner",
                    source_text=moment.statement,
                    source_ref=moment.key,
                ),
                org_id=org_id,
                agent_id=agent_id,
                idempotency_key=f"owner:{moment.key}",
            )
            outcome = result.outcome
        except JudgeError:
            outcome = "not_judged"
        counted[outcome] = counted.get(outcome, 0) + 1
    return counted


def _day(when: datetime) -> str:
    return f"{when:%-d %B %Y}"


def _clip(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 3] + "..."
