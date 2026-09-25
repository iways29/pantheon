"""The only path to facts.

Nothing outside this module reads or writes the facts table. Centralising it
is what makes provenance, embedding and supersession consistent: every fact
arrives with a source and an embedding, and a fact that turns out to be wrong
is superseded rather than deleted, so what was believed and when survives.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

import psycopg

from app.brain.embeddings import Embedder

_FACT_COLUMNS = """
    id, org_id, claim, source, source_ref, confidence, status,
    superseded_by, created_by_run_id, created_at, updated_at,
    admitted_by, review_after, visibility, quote, embedding_model, document_id
"""


@dataclass(frozen=True)
class Admission:
    """Proof that a fact passed the brain write gate (ADR 010).

    `request_id` is the brain_claim judgment that admitted it; the database
    refuses a fact without one. Only `app.brain.write_gate` should make these.
    """

    request_id: UUID
    status: str = "active"
    review_after: date | None = None
    visibility: str = "internal"
    quote: str | None = None
    #: The document the claim was taken from, when it was (ADR 015).
    document_id: UUID | str | None = None


@dataclass(frozen=True)
class Fact:
    id: UUID
    org_id: UUID
    claim: str
    source: str | None
    source_ref: str | None
    confidence: float | None
    status: str
    superseded_by: UUID | None
    created_by_run_id: UUID | None
    created_at: datetime
    updated_at: datetime
    admitted_by: UUID | None = None
    review_after: date | None = None
    visibility: str = "internal"
    quote: str | None = None
    embedding_model: str | None = None
    document_id: UUID | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Fact":
        return cls(
            id=row["id"],
            org_id=row["org_id"],
            claim=row["claim"],
            source=row["source"],
            source_ref=row["source_ref"],
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            status=row["status"],
            superseded_by=row["superseded_by"],
            created_by_run_id=row["created_by_run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            admitted_by=row["admitted_by"],
            review_after=row["review_after"],
            visibility=row["visibility"],
            quote=row["quote"],
            embedding_model=row["embedding_model"],
            document_id=row["document_id"],
        )


@dataclass(frozen=True)
class FactMatch:
    """A fact and how far it sat from the query, by cosine distance.

    Distance runs 0 (identical direction) to 2 (opposite), so smaller is more
    similar. Exposed raw rather than as a similarity score because thresholds
    are policy and belong in config, not here.
    """

    fact: Fact
    distance: float


class BrainError(RuntimeError):
    pass


class Brain:
    """Fact storage and retrieval for one database connection.

    The connection carries the organisation identity: rows are filtered by RLS
    based on who the caller is, not by an org_id argument threaded through
    every method. Construct this inside `db.acting_as` and the isolation
    follows from the policies rather than from remembering a WHERE clause.
    """

    def __init__(self, connection: psycopg.Connection, embedder: Embedder) -> None:
        self._connection = connection
        self._embedder = embedder

    def insert_fact(
        self,
        *,
        org_id: UUID | str,
        claim: str,
        admission: Admission,
        source: str | None = None,
        source_ref: str | None = None,
        confidence: float | None = None,
        created_by_run_id: UUID | str | None = None,
    ) -> Fact:
        """Store a claim with its embedding, provenance and admission.

        Callers other than the write gate have no `Admission` to give, and the
        database refuses a fact without the judgment it names.
        """
        claim = claim.strip()
        if not claim:
            raise ValueError("A fact needs a claim")

        embedding = self._embedder.embed(claim)
        if not any(embedding):
            # Cosine distance against a zero vector is undefined, so such a
            # fact would be invisible to every search. Better to refuse it.
            raise BrainError(f"Claim produced an empty embedding: {claim!r}")

        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                insert into public.facts
                    (org_id, claim, source, source_ref, confidence,
                     created_by_run_id, embedding, admitted_by, status,
                     review_after, visibility, quote, embedding_model, document_id)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning {_FACT_COLUMNS}
                """,
                (
                    str(org_id),
                    claim,
                    source,
                    source_ref,
                    confidence,
                    str(created_by_run_id) if created_by_run_id else None,
                    embedding,
                    str(admission.request_id),
                    admission.status,
                    admission.review_after,
                    admission.visibility,
                    admission.quote,
                    self._embedder.name,
                    str(admission.document_id) if admission.document_id else None,
                ),
            )
            row = cursor.fetchone()

        if row is None:
            raise BrainError("Insert returned no row; RLS may have rejected it")
        return Fact.from_row(row)

    def get(self, fact_id: UUID | str) -> Fact | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"select {_FACT_COLUMNS} from public.facts where id = %s",
                (str(fact_id),),
            )
            row = cursor.fetchone()
        return Fact.from_row(row) if row else None

    def find_same_claim(self, claim: str, *, org_id: UUID | str) -> Fact | None:
        """An active or disputed fact with the same words, ignoring case,
        spacing and trailing punctuation. No model involved.

        Filtered by org as well as by RLS, so a service-role caller cannot
        match another org's fact."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                select {_FACT_COLUMNS} from public.facts
                where org_id = %s
                  and public.normalize_claim(claim) = public.normalize_claim(%s)
                  and status in ('active', 'disputed')
                order by created_at
                limit 1
                """,
                (str(org_id), claim),
            )
            row = cursor.fetchone()
        return Fact.from_row(row) if row else None

    def stale_embeddings(self, model: str, *, limit: int = 100) -> list[Fact]:
        """Facts embedded by another model than `model`, oldest first."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                select {_FACT_COLUMNS} from public.facts
                where embedding_model is distinct from %s
                order by created_at
                limit %s
                """,
                (model, limit),
            )
            return [Fact.from_row(row) for row in cursor.fetchall()]

    def reembed(self, facts: list[Fact]) -> int:
        """Re-embed facts with this brain's model. Text and history are unchanged."""
        many = getattr(self._embedder, "embed_many", None)
        claims = [f.claim for f in facts]
        vectors = many(claims) if many else [self._embedder.embed(c) for c in claims]
        with self._connection.cursor() as cursor:
            for fact, vector in zip(facts, vectors, strict=True):
                cursor.execute(
                    "update public.facts set embedding = %s, embedding_model = %s where id = %s",
                    (vector, self._embedder.name, str(fact.id)),
                )
        return len(facts)

    def claims_from_run(self, run_id: UUID | str) -> list[str]:
        """Every claim a run has already stored, in insertion order.

        What lets an agent step that re-runs after a crash skip the facts it
        wrote the first time instead of duplicating them.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                "select claim from public.facts where created_by_run_id = %s order by created_at",
                (str(run_id),),
            )
            return [row["claim"] for row in cursor.fetchall()]

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        status: str | None = "active",
    ) -> list[FactMatch]:
        """Find the facts nearest to `query` by cosine distance.

        Defaults to active facts only: superseded and disputed claims stay
        readable through `get`, but must not surface as though still believed.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")

        embedding = self._embedder.embed(query)
        if not any(embedding):
            return []

        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                select {_FACT_COLUMNS}, embedding <=> %s::vector as distance
                from public.facts
                where embedding is not null
                  and embedding_model = %s
                  and (%s::text is null or status = %s::text)
                order by embedding <=> %s::vector
                limit %s
                """,
                (embedding, self._embedder.name, status, status, embedding, limit),
            )
            rows = cursor.fetchall()

        return [FactMatch(fact=Fact.from_row(row), distance=float(row["distance"])) for row in rows]

    def supersede(self, fact_id: UUID | str, *, replaced_by: UUID | str) -> Fact:
        """Mark a fact as replaced by a newer one.

        The old claim is kept. Deleting it would erase the record of what the
        system believed when it acted on that belief.
        """
        if str(fact_id) == str(replaced_by):
            raise ValueError("A fact cannot supersede itself")

        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""
                update public.facts
                set status = 'superseded', superseded_by = %s
                where id = %s
                returning {_FACT_COLUMNS}
                """,
                (str(replaced_by), str(fact_id)),
            )
            row = cursor.fetchone()

        if row is None:
            raise BrainError(f"No fact {fact_id} visible to supersede")
        return Fact.from_row(row)
