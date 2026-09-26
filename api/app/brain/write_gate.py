"""The brain write gate: the only way a new fact enters the brain (ADR 010).

The recipe combines TypeSafe's citation-check and entity-alignment cookbooks:

1. **Code first, no model.** Length limits; provenance present; evidence
   present; a quote, when given, must appear in the source text (otherwise it
   is `fabricated`); every number in the claim must appear in the evidence
   (Jev is weak at numbers, so they are compared here); an exact duplicate of
   a stored fact is skipped.
2. **One Jev request per claim** (gate `brain_claim`): is it a standalone
   factual claim, an opinion or hedge, a secret or personal data, an
   instruction aimed at an AI, likely to change; and does the evidence
   support it, contradict it, or say nothing.
3. **Neighbours.** The nearest existing facts, each judged against the claim
   (gate `brain_neighbour`): same, related or different; contradicts; gives
   a newer value.
4. **Decision in code:** accept; accept as disputed; supersede the old fact;
   skip as duplicate; reject; or hold for review in the approval queue. The
   uncertain band always goes to review.

Thresholds and limits are the gates' data, not constants here. With TypeSafe
down the gates fail closed: nothing is written.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal
from uuid import UUID

import psycopg

from app.brain.store import Admission, Brain, Fact
from app.gateway import NoulAnswer
from app.judge import Decision, Judge, Policy, SensitiveStateRefused
from app.judge.store import load_gate

CLAIM_GATE = "brain_claim"
NEIGHBOUR_GATE = "brain_neighbour"

Outcome = Literal["accepted", "disputed", "superseded", "duplicate", "rejected", "review"]


@dataclass(frozen=True)
class FactCandidate:
    """A claim an agent (or the owner) wants the brain to hold."""

    claim: str
    #: Who or what it came from, e.g. `agent:research` or a URL's host.
    source: str
    #: The full text the claim was taken from, when there is one.
    source_text: str | None = None
    #: The exact words in `source_text` that support the claim.
    quote: str | None = None
    #: Where to find the source again: a URL, a run id, a document id.
    source_ref: str | None = None
    #: Marked sensitive, it never goes to TypeSafe (open decision 11); it is
    #: held for a person instead.
    sensitive: bool = False
    visibility: Literal["internal", "public"] = "internal"
    #: The document the claim was taken from (ADR 015); the fact links back.
    document_id: UUID | str | None = None


@dataclass(frozen=True)
class WriteResult:
    outcome: Outcome
    reasons: tuple[str, ...]
    fact: Fact | None = None
    #: The fact this one replaced, when the outcome is `superseded`, or the
    #: stored fact it duplicates, when `duplicate`.
    related_fact_id: UUID | None = None
    approval_id: UUID | None = None
    #: judgments.request_id of every judgment behind this decision.
    judgments: tuple[UUID, ...] = field(default_factory=tuple)

    @property
    def written(self) -> bool:
        return self.fact is not None


class BrainWriter:
    """Proposes facts through the gate, over one connection.

    The connection carries the caller's identity (RLS), as Brain's and the
    judge's do; build all three on the same one.
    """

    def __init__(self, connection: psycopg.Connection, brain: Brain, judge: Judge) -> None:
        self._connection = connection
        self._brain = brain
        self._judge = judge

    def propose(
        self,
        candidate: FactCandidate,
        *,
        org_id: UUID | str,
        agent_id: UUID | str,
        run_id: UUID | str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResult:
        claim = " ".join(candidate.claim.split())
        settings = load_gate(self._connection, org_id=org_id, gate=CLAIM_GATE).policy

        early = self._code_checks(claim, candidate, org_id=org_id, settings=settings)
        if early is not None:
            return self._finish(early, candidate, claim, org_id, agent_id, run_id)

        evidence = _evidence(candidate, claim, int(settings.setting("max_evidence_chars", 6000)))
        state = {"claim": claim, "source": candidate.source, "evidence": evidence}
        reference = f"claim:{_digest(claim)}"
        key = idempotency_key or f"fact:{run_id or 'direct'}:{_digest(claim)}"

        try:
            verdict = self._judge.run(
                CLAIM_GATE,
                state,
                agent_id=agent_id,
                run_id=run_id,
                input_ref=reference,
                sensitive=candidate.sensitive,
            )
        except SensitiveStateRefused:
            reasons = ("Marked sensitive: a person must check it",)
            result = self._hold(reasons, (), candidate, claim, org_id, run_id, key)
            return self._finish(result, candidate, claim, org_id, agent_id, run_id)

        requests = (verdict.request_id,) if verdict.request_id else ()
        if verdict.outcome == "reject" or verdict.failed:
            result = WriteResult("rejected", _texts(verdict), judgments=requests)
            return self._finish(result, candidate, claim, org_id, agent_id, run_id)
        if verdict.outcome == "review":
            result = self._hold(_texts(verdict), requests, candidate, claim, org_id, run_id, key)
            return self._finish(result, candidate, claim, org_id, agent_id, run_id)

        neighbours = self._neighbours(claim, org_id, settings)
        judged: list[tuple[Fact, Decision]] = []
        for neighbour in neighbours:
            decision = self._judge.run(
                NEIGHBOUR_GATE,
                {"new_claim": claim, "existing_fact": neighbour.claim},
                agent_id=agent_id,
                run_id=run_id,
                input_ref=f"{reference}:vs:{neighbour.id}",
                sensitive=candidate.sensitive,
            )
            judged.append((neighbour, decision))
        requests += tuple(d.request_id for _, d in judged if d.request_id)

        result = self._settle(
            verdict, judged, requests, candidate, claim, org_id, run_id, key, settings
        )
        return self._finish(result, candidate, claim, org_id, agent_id, run_id)

    # --- Step 1: code checks ------------------------------------------------

    def _code_checks(
        self,
        claim: str,
        candidate: FactCandidate,
        *,
        org_id: UUID | str,
        settings: Policy,
    ) -> WriteResult | None:
        if not claim:
            return WriteResult("rejected", ("Empty claim",))
        limit = int(settings.setting("max_claim_chars", 500))
        if len(claim) > limit:
            reason = f"Claim is {len(claim)} characters; the limit is {limit}"
            return WriteResult("rejected", (reason,))
        if not candidate.source.strip():
            return WriteResult("rejected", ("No provenance: the claim names no source",))
        source_text = (candidate.source_text or "").strip()
        quote = (candidate.quote or "").strip()
        if not source_text and not quote:
            return WriteResult("rejected", ("No evidence: neither a source text nor a quote",))
        if quote and source_text and _squash(quote) not in _squash(source_text):
            return WriteResult("rejected", ("Fabricated: the quote does not appear in the source",))
        missing = _numbers(claim) - _numbers(quote or source_text)
        if missing:
            return WriteResult(
                "rejected",
                (f"Numbers not in the evidence: {', '.join(sorted(missing))}",),
            )
        same = self._brain.find_same_claim(claim, org_id=org_id)
        if same is not None:
            return WriteResult(
                "duplicate", ("The brain already holds this claim",), related_fact_id=same.id
            )
        return None

    # --- Step 3: neighbours -------------------------------------------------

    def _neighbours(self, claim: str, org_id: UUID | str, settings: Policy) -> list[Fact]:
        count = int(settings.setting("neighbours", 3))
        if count <= 0:
            return []
        reach = settings.setting("neighbour_max_distance", 0.8)
        matches = self._brain.search(claim, limit=count * 3, status=None)
        near = [
            m.fact
            for m in matches
            if str(m.fact.org_id) == str(org_id)
            and m.fact.status in ("active", "disputed")
            and m.distance <= reach
        ]
        return near[:count]

    # --- Step 4: decide and write -------------------------------------------

    def _settle(
        self,
        verdict: Decision,
        judged: list[tuple[Fact, Decision]],
        requests: tuple[UUID, ...],
        candidate: FactCandidate,
        claim: str,
        org_id: UUID | str,
        run_id: UUID | str | None,
        key: str,
        settings: Policy,
    ) -> WriteResult:
        def texts(outcome: str) -> tuple[str, ...]:
            return tuple(
                f"{r.text} (vs fact {fact.id})"
                for fact, d in judged
                if d.outcome == outcome
                for r in d.reasons
                if r.outcome == outcome
            ) or tuple(f"{outcome} vs fact {fact.id}" for fact, d in judged if d.outcome == outcome)

        by_outcome: dict[str, list[Fact]] = {}
        for fact, decision in judged:
            by_outcome.setdefault(decision.outcome, []).append(fact)

        if "review" in by_outcome:
            return self._hold(texts("review"), requests, candidate, claim, org_id, run_id, key)
        if "duplicate" in by_outcome:
            return WriteResult(
                "duplicate",
                texts("duplicate"),
                related_fact_id=by_outcome["duplicate"][0].id,
                judgments=requests,
            )
        if len(by_outcome.get("update", [])) > 1:
            return self._hold(
                ("It would replace more than one existing fact",),
                requests,
                candidate,
                claim,
                org_id,
                run_id,
                key,
            )

        admission = Admission(
            request_id=verdict.request_id,  # type: ignore[arg-type]
            status="disputed" if "conflict" in by_outcome else "active",
            review_after=_review_after(verdict, settings),
            visibility=candidate.visibility,
            quote=candidate.quote,
            document_id=candidate.document_id,
        )
        fact = self._brain.insert_fact(
            org_id=org_id,
            claim=claim,
            admission=admission,
            source=candidate.source,
            source_ref=candidate.source_ref,
            created_by_run_id=run_id,
        )
        if "update" in by_outcome:
            old = by_outcome["update"][0]
            self._brain.supersede(old.id, replaced_by=fact.id)
            return WriteResult(
                "superseded", texts("update"), fact=fact, related_fact_id=old.id, judgments=requests
            )
        if "conflict" in by_outcome:
            return WriteResult("disputed", texts("conflict"), fact=fact, judgments=requests)
        return WriteResult("accepted", _texts(verdict), fact=fact, judgments=requests)

    def admit_approved(
        self, approval: dict[str, Any], *, agent_id: UUID | str, visibility: str | None = None
    ) -> WriteResult:
        """Store a held claim the owner approved (a `fact_write` approval).

        The fact is admitted by the brain_claim judgment made when it was held,
        exactly as proposed: an owner's edit would not be the text Jev judged.
        Admitting twice returns the fact already stored.
        """
        if approval["action_type"] != "fact_write" or approval["status"] != "approved":
            raise ValueError("Only an approved fact_write can be admitted")
        proposal = approval["payload"]
        org_id = approval["org_id"]
        with self._connection.cursor() as cursor:
            cursor.execute(
                "select request_id from public.judgments where org_id = %s "
                "and gate = %s and request_id = any(%s::uuid[]) limit 1",
                (str(org_id), CLAIM_GATE, proposal.get("judgments") or []),
            )
            judged = cursor.fetchone()
            cursor.execute(
                "select id from public.facts where org_id = %s and admitted_by = %s",
                (str(org_id), str(judged["request_id"]) if judged else None),
            )
            stored = cursor.fetchone()
        if judged is None:
            raise ValueError("It was held before any judgment; propose it again instead")
        if stored is not None:
            fact = self._brain.get(stored["id"])
            return WriteResult("accepted", ("Already admitted",), fact=fact)
        candidate = FactCandidate(
            claim=proposal["claim"],
            source=proposal.get("source"),
            source_text=proposal["claim"],
            source_ref=proposal.get("source_ref"),
            quote=proposal.get("quote"),
            visibility=visibility or proposal.get("visibility") or "internal",
        )
        fact = self._brain.insert_fact(
            org_id=org_id,
            claim=candidate.claim,
            admission=Admission(
                request_id=judged["request_id"],
                visibility=candidate.visibility,
                quote=candidate.quote,
            ),
            source=candidate.source,
            source_ref=candidate.source_ref,
        )
        result = WriteResult(
            "accepted", (f"Approved by the owner (approval {approval['id']})",), fact=fact
        )
        return self._finish(result, candidate, candidate.claim, org_id, agent_id, None)

    def _hold(
        self,
        reasons: tuple[str, ...],
        requests: tuple[UUID, ...],
        candidate: FactCandidate,
        claim: str,
        org_id: UUID | str,
        run_id: UUID | str | None,
        key: str,
    ) -> WriteResult:
        """Queue the claim for a person. Idempotent on `key`."""
        proposal = {
            "claim": claim,
            "source": candidate.source,
            "source_ref": candidate.source_ref,
            "quote": candidate.quote,
            "visibility": candidate.visibility,
            "sensitive": candidate.sensitive,
            "reasons": list(reasons),
            "judgments": [str(r) for r in requests],
        }
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.approvals
                    (org_id, run_id, action_type, payload, agent_output_snapshot,
                     idempotency_key)
                values (%s, %s, 'fact_write', %s, %s, %s)
                on conflict (org_id, idempotency_key) where idempotency_key is not null
                do nothing
                returning id
                """,
                (
                    str(org_id),
                    str(run_id) if run_id else None,
                    json.dumps(proposal),
                    json.dumps({"claim": claim, "source_text": candidate.source_text}),
                    key,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "select id from public.approvals where org_id = %s and idempotency_key = %s",
                    (str(org_id), key),
                )
                row = cursor.fetchone()
        return WriteResult("review", reasons, approval_id=row["id"], judgments=requests)

    def _finish(
        self,
        result: WriteResult,
        candidate: FactCandidate,
        claim: str,
        org_id: UUID | str,
        agent_id: UUID | str,
        run_id: UUID | str | None,
    ) -> WriteResult:
        payload = {
            "claim": claim,
            "source": candidate.source,
            "outcome": result.outcome,
            "reasons": list(result.reasons),
            "fact_id": str(result.fact.id) if result.fact else None,
            "related_fact_id": str(result.related_fact_id) if result.related_fact_id else None,
            "approval_id": str(result.approval_id) if result.approval_id else None,
            "judgments": [str(r) for r in result.judgments],
        }
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.events (org_id, run_id, agent_id, type, payload)
                values (%s, %s, %s, 'fact_write_decided', %s)
                """,
                (
                    str(org_id),
                    str(run_id) if run_id else None,
                    str(agent_id),
                    json.dumps(payload),
                ),
            )
        return result


def _texts(decision: Decision) -> tuple[str, ...]:
    return tuple(r.text for r in decision.reasons) or (f"{decision.gate}: {decision.outcome}",)


def _review_after(verdict: Decision, settings: Policy) -> date | None:
    """When to recheck a claim that is likely to change. Dates are code's job."""
    answer = verdict.answers.get("volatile")
    if not isinstance(answer, NoulAnswer):
        return None
    if answer.noul < settings.setting("volatile_at_least", 0.5):
        return None
    return date.today() + timedelta(days=int(settings.setting("review_after_days", 90)))


def _digest(text: str) -> str:
    return hashlib.sha256(_squash(text).encode()).hexdigest()[:16]


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """Numbers as written, normalised: `5,000` is `5000`, `2.50` is `2.5`."""
    found = set()
    for raw in _NUMBER.findall(text):
        value = raw.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        found.add(value)
    return found


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[a-z0-9]+")


def _evidence(candidate: FactCandidate, claim: str, limit: int) -> str:
    """The evidence Jev sees: the quote, or the source sentences nearest the claim.

    Jev is less accurate with irrelevant state, so a long source is cut down
    in code to the sentences that share the most words with the claim, kept
    in their original order.
    """
    if candidate.quote and candidate.quote.strip():
        return candidate.quote.strip()[:limit]
    text = (candidate.source_text or "").strip()
    if len(text) <= limit:
        return text
    words = set(_WORD.findall(claim.lower()))
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    ranked = sorted(
        range(len(sentences)),
        key=lambda i: len(words & set(_WORD.findall(sentences[i].lower()))),
        reverse=True,
    )
    chosen: list[int] = []
    size = 0
    for index in ranked:
        if size + len(sentences[index]) + 1 > limit:
            continue
        chosen.append(index)
        size += len(sentences[index]) + 1
    return " ".join(sentences[i] for i in sorted(chosen))
