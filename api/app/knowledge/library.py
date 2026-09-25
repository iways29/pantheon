"""Documents: add them safely, find them by meaning (Step 6, ADR 015).

Adding a document, always in the owner's session:

1. extract plain text (standard library only);
2. if the same content is already in the same scope, return that document;
3. screen the text (Step 5.3) before anything else happens to it;
4. keep the original file in the private Storage bucket;
5. record the document with its screening label;
6. only if it is clean: cut it into chunks, embed them through the gateway,
   and store them. A document held for review goes to the approval queue; a
   quarantined one is kept for the record and never chunked.

Searching is RLS-scoped: a session acting for an agent sees only company
chunks, its department's and its own; the owner's session sees all.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import psycopg

from app.brain.embeddings import Embedder
from app.judge.screening import Screener, chunk_text
from app.knowledge.extract import extract_text
from app.knowledge.storage import FileStore

Scope = Literal["company", "department", "agent"]
#: Retrieval chunks: small enough to put a handful in a prompt.
CHUNK_CHARS = 1500
#: A larger upload is refused before any work is done.
MAX_BYTES = 2_000_000


class DocumentError(ValueError):
    status = 400


class ScopeTargetNotFound(DocumentError):
    status = 404


@dataclass(frozen=True)
class DocumentResult:
    id: UUID
    status: str
    chunks: int
    created: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    approval_id: UUID | None = None


@dataclass(frozen=True)
class ChunkMatch:
    document_id: UUID
    title: str
    scope: str
    position: int
    text: str
    distance: float


class Library:
    def __init__(
        self,
        connection: psycopg.Connection,
        *,
        embedder: Embedder,
        screener: Screener | None = None,
        files: FileStore | None = None,
    ) -> None:
        self._connection = connection
        self._embedder = embedder
        self._screener = screener
        self._files = files

    def add(
        self,
        *,
        org_id: UUID | str,
        content: bytes,
        filename: str,
        content_type: str,
        title: str,
        scope: Scope,
        processor_agent_id: UUID | str,
        department: str | None = None,
        agent: str | None = None,
        source_kind: Literal["upload", "link"] = "upload",
        source_ref: str | None = None,
    ) -> DocumentResult:
        """Screen, store and (if clean) chunk a document. Owner's session only.

        `processor_agent_id` is the agent whose department pays for the
        screening and the embeddings.
        """
        if self._screener is None or self._files is None:
            raise RuntimeError("Adding documents needs a screener and a file store")
        if len(content) > MAX_BYTES:
            raise DocumentError(f"{len(content)} bytes; the limit is {MAX_BYTES}")
        department_id, agent_id = self._scope_target(org_id, scope, department, agent)
        text = extract_text(content, content_type)
        if not text.strip():
            raise DocumentError("No text in the document")
        digest = hashlib.sha256(content).hexdigest()

        existing = self._existing(org_id, scope, department_id, agent_id, digest)
        if existing is not None:
            return existing

        screening = self._screener.screen(
            text,
            purpose=f"Reference material for the company's agents: {title}",
            source_kind="web_page" if source_kind == "link" else "upload",
            source_ref=source_ref or filename,
            org_id=org_id,
            agent_id=processor_agent_id,
        )
        path = f"{org_id}/{digest}/{_safe_name(filename)}"
        self._files.put(path, content, content_type)

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.documents
                    (org_id, scope, department_id, agent_id, title, source_kind, source_ref,
                     content_type, bytes, sha256, storage_path, status, screening)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    str(org_id),
                    scope,
                    department_id,
                    agent_id,
                    title,
                    source_kind,
                    source_ref or filename,
                    content_type,
                    len(content),
                    digest,
                    path,
                    screening.label,
                    json.dumps(
                        {
                            "reasons": list(screening.reasons),
                            "chunks": [c.label for c in screening.chunks],
                        }
                    ),
                ),
            )
            document_id = cursor.fetchone()["id"]

        if screening.label == "clean":
            count = self._store_chunks(org_id, document_id, scope, department_id, agent_id, text)
            return DocumentResult(document_id, "clean", count, True)

        approval_id = None
        if screening.label == "review":
            approval_id = self._hold(org_id, document_id, title, screening.reasons)
        return DocumentResult(document_id, screening.label, 0, True, screening.reasons, approval_id)

    def search(self, query: str, *, limit: int = 5) -> list[ChunkMatch]:
        """Chunks nearest the query that this session may read."""
        vector = self._embedder.embed(query)
        if not any(vector):
            return []
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                select c.document_id, d.title, c.scope, c.position, c.text,
                       c.embedding <=> %s::vector as distance
                from public.document_chunks c
                join public.documents d on d.id = c.document_id
                where c.embedding_model = %s
                order by c.embedding <=> %s::vector
                limit %s
                """,
                (vector, self._embedder.name, vector, limit),
            )
            return [
                ChunkMatch(
                    r["document_id"],
                    r["title"],
                    r["scope"],
                    r["position"],
                    r["text"],
                    float(r["distance"]),
                )
                for r in cursor.fetchall()
            ]

    def _scope_target(
        self, org_id: UUID | str, scope: Scope, department: str | None, agent: str | None
    ) -> tuple[str | None, str | None]:
        if scope == "company":
            if department or agent:
                raise DocumentError("A company document names no department or agent")
            return None, None
        with self._connection.cursor() as cursor:
            if scope == "department":
                if not department or agent:
                    raise DocumentError("A department document names one department")
                cursor.execute(
                    "select id from public.departments where org_id = %s and name = %s",
                    (str(org_id), department),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ScopeTargetNotFound(f"No department {department!r}")
                return str(row["id"]), None
            if not agent or department:
                raise DocumentError("An agent document names one agent")
            cursor.execute(
                "select id from public.agents where org_id = %s and name = %s",
                (str(org_id), agent),
            )
            row = cursor.fetchone()
            if row is None:
                raise ScopeTargetNotFound(f"No agent {agent!r}")
            return None, str(row["id"])

    def _existing(
        self,
        org_id: UUID | str,
        scope: str,
        department_id: str | None,
        agent_id: str | None,
        digest: str,
    ) -> DocumentResult | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                select id, status, chunks from public.documents
                where org_id = %s and scope = %s
                  and department_id is not distinct from %s
                  and agent_id is not distinct from %s
                  and sha256 = %s
                """,
                (str(org_id), scope, department_id, agent_id, digest),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return DocumentResult(row["id"], row["status"], row["chunks"], False)

    def _store_chunks(
        self,
        org_id: UUID | str,
        document_id: UUID,
        scope: str,
        department_id: str | None,
        agent_id: str | None,
        text: str,
    ) -> int:
        chunks = chunk_text(text, CHUNK_CHARS)
        many = getattr(self._embedder, "embed_many", None)
        vectors = many(chunks) if many else [self._embedder.embed(c) for c in chunks]
        with self._connection.cursor() as cursor:
            cursor.executemany(
                """
                insert into public.document_chunks
                    (org_id, document_id, scope, department_id, agent_id, position, text,
                     embedding, embedding_model)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        str(org_id),
                        str(document_id),
                        scope,
                        department_id,
                        agent_id,
                        position,
                        chunk,
                        vector,
                        self._embedder.name,
                    )
                    for position, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                ],
            )
            cursor.execute(
                "update public.documents set chunks = %s where id = %s",
                (len(chunks), str(document_id)),
            )
        return len(chunks)

    def _hold(
        self, org_id: UUID | str, document_id: UUID, title: str, reasons: tuple[str, ...]
    ) -> UUID:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.approvals
                    (org_id, action_type, payload, agent_output_snapshot, idempotency_key)
                values (%s, 'document_review', %s, %s, %s)
                on conflict (org_id, idempotency_key) where idempotency_key is not null
                do update set payload = excluded.payload
                returning id
                """,
                (
                    str(org_id),
                    json.dumps(
                        {"document_id": str(document_id), "title": title, "reasons": list(reasons)}
                    ),
                    json.dumps({"document_id": str(document_id)}),
                    f"document:{document_id}",
                ),
            )
            return cursor.fetchone()["id"]


def _safe_name(filename: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename.strip()).strip("-.")
    return name[:120] or "file"
