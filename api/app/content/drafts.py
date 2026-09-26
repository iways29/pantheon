"""Drafts: written by an agent, checked, then approved or not by the owner.

`save` stores a draft and the public facts it relied on (`artifact_claims`).

`check` runs every check on a draft and moves it on:

1. code checks, exact and free: Devanagari script, and the banned phrases in
   the `check_draft` tool's settings (hype words, the names of the fictional
   characters the owner described the voice with);
2. the output guardrail (`guard_output`): harm, secrets, Sanskrit, and the
   fund and verse rules that always go to the owner;
3. the claim check (`draft_claim`), one sentence at a time, against the
   brain's public facts (right-hand idea 3). An unsupported or contradicted
   claim blocks the draft and names the sentence;
4. voice and quality (`draft_voice`): narrow hazards plus a weighted score.

Anything blocking sends the draft back (`blocked`) with the reasons, for the
writer to revise as a new draft. Otherwise it becomes `ready` behind a
`draft_review` approval card, flagged with anything the owner should look at.
The owner approves (edits kept as a voice example) or rejects; phase 1 posts
by hand.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from app.judge.guardrails import CODE_CHECK, DEVANAGARI_REASON, Guard
from app.judge.policy import Policy
from app.judge.store import load_gate

CHANNELS = ("blog", "x", "reddit", "instagram", "newsletter", "other")

_DEVANAGARI = re.compile(r"[ऀ-ॿ꣠-ꣿ]")
# Sentence ends, and line breaks (list items, headings).
_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n+")
_MARKUP = re.compile(r"^\s*(?:[#>*\-+]+|\d+[.)])\s*")
#: Guard chunks stay under the gate's state size limit.
_CHUNK = 12000


@dataclass(frozen=True)
class Saved:
    draft_id: UUID
    relied_on: list[str]
    #: Facts it named that were not recorded, and why.
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckResult:
    draft_id: UUID
    status: str  # blocked | ready | unchanged status
    blocking: list[dict[str, Any]]
    flags: list[dict[str, Any]]
    voice_score: float | None = None
    approval_id: UUID | None = None


def save(
    cursor: psycopg.Cursor,
    *,
    org_id: UUID | str,
    agent_id: UUID | str,
    channel: str,
    format: str,
    title: str,
    body: str,
    idempotency_key: str,
    brief: str | None = None,
    fact_ids: list[str] | None = None,
    revises: str | None = None,
    task_id: UUID | str | None = None,
    run_id: UUID | str | None = None,
) -> Saved:
    """Store a draft. Saving the same one twice changes nothing."""
    cursor.execute(
        "select id from public.drafts where org_id = %s and idempotency_key = %s",
        (str(org_id), idempotency_key),
    )
    existing = cursor.fetchone()
    if existing is not None:
        return Saved(existing["id"], [])
    cursor.execute(
        """
        insert into public.drafts
            (org_id, agent_id, task_id, run_id, revises, channel, format, title, body,
             brief, idempotency_key)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            str(org_id),
            str(agent_id),
            str(task_id) if task_id else None,
            str(run_id) if run_id else None,
            revises,
            channel,
            format,
            title,
            body,
            brief,
            idempotency_key,
        ),
    )
    draft_id = cursor.fetchone()["id"]

    wanted = list(dict.fromkeys(fact_ids or []))
    visibility: dict[str, str] = {}
    if wanted:
        cursor.execute(
            "select id::text as id, visibility from public.facts "
            "where org_id = %s and id::text = any(%s) and status = 'active'",
            (str(org_id), wanted),
        )
        visibility = {r["id"]: r["visibility"] for r in cursor.fetchall()}
    relied, skipped = [], {}
    for fact_id in wanted:
        seen = visibility.get(fact_id)
        if seen is None:
            skipped[fact_id] = "no active fact with this id"
        elif seen != "public":
            skipped[fact_id] = "internal: public content may not rely on it"
        else:
            cursor.execute(
                "insert into public.artifact_claims (org_id, draft_id, fact_id, relation) "
                "values (%s, %s, %s, 'relied_on')",
                (str(org_id), str(draft_id), fact_id),
            )
            relied.append(fact_id)
    return Saved(draft_id, relied, skipped)


def sentences(body: str) -> list[str]:
    """The draft's sentences, without list or heading markup; very short
    fragments (headings, sign-offs) are left out."""
    found = []
    for part in _SPLIT.split(body):
        text = _MARKUP.sub("", part).strip()
        if len(text.split()) >= 3:
            found.append(text)
    return found


def check(
    cursor: psycopg.Cursor,
    *,
    judge: Any,  # noqa: ANN401 - app.judge.Judge
    brain: Any,  # noqa: ANN401 - app.brain.store.BrainStore
    org_id: UUID | str,
    agent_id: UUID | str,
    draft_id: UUID | str,
    banned_phrases: list[str],
    run_id: UUID | str | None = None,
) -> CheckResult:
    cursor.execute(
        "select * from public.drafts where id = %s and org_id = %s", (str(draft_id), str(org_id))
    )
    draft = cursor.fetchone()
    if draft is None:
        raise ValueError(f"No draft {draft_id}")
    if draft["status"] not in ("draft", "blocked"):
        return CheckResult(draft["id"], draft["status"], [], [], approval_id=draft["approval_id"])
    text = f"{draft['title']}\n\n{draft['body']}"
    blocking: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []

    # 1. Code checks.
    if _DEVANAGARI.search(text):
        blocking.append({"check": "code", "reason": DEVANAGARI_REASON})
    lowered = text.lower()
    for phrase in banned_phrases:
        if re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", lowered):
            blocking.append({"check": "code", "reason": f"{CODE_CHECK}banned phrase {phrase!r}"})

    # 2. The output guardrail, in chunks under the gate's size limit.
    guard = Guard(cursor.connection, judge)
    for start in range(0, len(text), _CHUNK):
        decision = guard.check(
            text[start : start + _CHUNK], side="output", agent_id=agent_id, run_id=run_id
        )
        entry = _entry("guard_output", decision)
        if decision.outcome == "block":
            blocking.append(entry)
        elif decision.outcome == "review":
            flags.append(entry)

    # 3. The claim check, sentence by sentence.
    claim_gate = load_gate(cursor.connection, org_id=org_id, gate="draft_claim")
    settings = claim_gate.policy
    per_sentence = int(settings.setting("facts_per_sentence", 5))
    max_distance = settings.setting("max_distance", 0.9)
    limit = int(settings.setting("max_sentences", 80))
    found = sentences(draft["body"])
    if len(found) > limit:
        flags.append(
            {
                "check": "draft_claim",
                "reason": f"Only the first {limit} of {len(found)} sentences were checked",
            }
        )
    relied = _relied_on(cursor, draft["id"])
    for sentence in found[:limit]:
        near = [
            m
            for m in brain.search(sentence, limit=per_sentence, visibility="public")
            if m.distance <= max_distance
        ]
        facts = {str(m.fact.id): m.fact.claim for m in near} | relied
        decision = judge.run(
            "draft_claim",
            {"sentence": sentence, "facts": list(facts.values()), "piece": draft["title"]},
            agent_id=agent_id,
            run_id=run_id,
        )
        verdict = decision.answers.get("claim")
        chose = getattr(verdict, "choice", None)
        if chose in ("supported", "contradicted") and near:
            cursor.execute(
                "insert into public.artifact_claims "
                "(org_id, draft_id, fact_id, relation, sentence, verdict) "
                "values (%s, %s, %s, 'checked', %s, %s)",
                (str(org_id), str(draft["id"]), str(near[0].fact.id), sentence, chose),
            )
        if decision.outcome == "pass":
            continue
        entry = _entry("draft_claim", decision) | {"sentence": sentence}
        if decision.outcome == "block":
            hint = _internal_hint(brain, sentence, max_distance)
            if hint:
                entry["internal_facts"] = hint
            blocking.append(entry)
        else:
            flags.append(entry)

    # 4. Voice and quality.
    voice = judge.run(
        "draft_voice",
        {"text": text[:_CHUNK], "channel": draft["channel"], "format": draft["format"]},
        agent_id=agent_id,
        run_id=run_id,
    )
    voice_policy = load_gate(cursor.connection, org_id=org_id, gate="draft_voice").policy
    score = _voice_score(voice, voice_policy)
    if voice.outcome != "pass":
        blocking.append(_entry("draft_voice", voice))
    elif score is not None and score < voice_policy.setting("min_score", 0.5):
        blocking.append(
            {"check": "draft_voice", "reason": f"Voice score {score:.2f} is under the bar"}
        )

    checks = {
        "blocking": blocking,
        "flags": flags,
        "voice_score": score,
        "sentences_checked": min(len(found), limit),
    }
    if blocking:
        cursor.execute(
            "update public.drafts set status = 'blocked', checks = %s where id = %s",
            (json.dumps(checks), str(draft["id"])),
        )
        return CheckResult(draft["id"], "blocked", blocking, flags, score)

    approval_id = _card(cursor, draft, flags, score, run_id=run_id, agent_id=agent_id)
    cursor.execute(
        "update public.drafts set status = 'ready', checks = %s, approval_id = %s where id = %s",
        (json.dumps(checks), str(approval_id), str(draft["id"])),
    )
    return CheckResult(draft["id"], "ready", [], flags, score, approval_id)


def _relied_on(cursor: psycopg.Cursor, draft_id: UUID) -> dict[str, str]:
    cursor.execute(
        "select f.id::text as id, f.claim from public.artifact_claims c "
        "join public.facts f on f.id = c.fact_id "
        "where c.draft_id = %s and c.relation = 'relied_on' and f.status = 'active' "
        "and f.visibility = 'public'",
        (str(draft_id),),
    )
    return {r["id"]: r["claim"] for r in cursor.fetchall()}


def _internal_hint(brain: Any, sentence: str, max_distance: float) -> list[dict[str, str]]:  # noqa: ANN401
    """Internal facts near an unsupported sentence: the owner may clear one."""
    return [
        {"fact_id": str(m.fact.id), "claim": m.fact.claim}
        for m in brain.search(sentence, limit=2, visibility="internal")
        if m.distance <= max_distance
    ]


def _entry(check_name: str, decision: Any) -> dict[str, Any]:  # noqa: ANN401
    reasons = [r.text for r in decision.reasons if r.outcome == decision.outcome]
    if decision.failed:
        reasons.append(f"The check gave no answer ({decision.failure}); run check_draft again")
    return {"check": check_name, "outcome": decision.outcome, "reason": "; ".join(reasons)}


def _voice_score(decision: Any, policy: Policy) -> float | None:  # noqa: ANN401
    """A weighted mean of the rubric scores, from 0 to 1; weights are settings."""
    if decision.failed:
        return None
    weights = {
        key: policy.setting(f"weight_{key}", default)
        for key, default in (("on_brand", 2.0), ("specific", 1.0), ("clear", 1.0))
    }
    total = weight_sum = 0.0
    for key, weight in weights.items():
        answer = decision.answers.get(key)
        if answer is None:
            continue
        top = max(len(answer.probabilities) - 1, 1)
        total += weight * answer.score / top
        weight_sum += weight
    return round(total / weight_sum, 3) if weight_sum else None


def _card(
    cursor: psycopg.Cursor,
    draft: dict[str, Any],
    flags: list[dict[str, Any]],
    score: float | None,
    *,
    run_id: UUID | str | None,
    agent_id: UUID | str,
) -> UUID:
    """The owner's approval card for a ready draft."""
    look_at = "; ".join(f["reason"] for f in flags)[:600]
    explanation = f"{draft['channel']} {draft['format']}: {draft['title']!r}" + (
        f". Look at: {look_at}" if look_at else ". All checks passed"
    )
    key = f"draft:{draft['id']}"
    cursor.execute(
        """
        insert into public.approvals
            (org_id, run_id, agent_id, action_type, action_key, payload,
             agent_output_snapshot, idempotency_key, explanation, recommendation)
        values (%s, %s, %s, 'draft_review', %s, %s, %s, %s, %s, %s)
        on conflict (org_id, idempotency_key) where idempotency_key is not null do nothing
        returning id
        """,
        (
            str(draft["org_id"]),
            str(run_id) if run_id else None,
            str(agent_id),
            f"draft:{draft['channel']}",
            json.dumps(
                {
                    "draft_id": str(draft["id"]),
                    "channel": draft["channel"],
                    "format": draft["format"],
                    "title": draft["title"],
                    "body": draft["body"],
                    "flags": flags,
                    "voice_score": score,
                    "task_id": str(draft["task_id"]) if draft["task_id"] else None,
                }
            ),
            json.dumps({"title": draft["title"], "body": draft["body"]}),
            f"approval:{key}:{_digest(draft['body'])}",
            explanation,
            "look_closer" if flags else None,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            "select id from public.approvals where org_id = %s and idempotency_key = %s",
            (str(draft["org_id"]), f"approval:{key}:{_digest(draft['body'])}"),
        )
        row = cursor.fetchone()
    return row["id"]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
