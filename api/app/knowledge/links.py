"""Links, in two steps: preview, then push to the brain (Step 6, ADR 016).

**Preview** fetches the page safely (`fetch.py`), extracts its text, screens
it (Step 5.3) and, only if it is clean, asks the agent's own `extract` prompt
(from the database, ADR 007) for the claims the page would add, with the page
handed to the model as quoted data. The preview is saved; the brain is not
touched.

**Push** is a separate, explicit action. Each previewed claim goes through
the brain write gate (ADR 010) with the saved page text as evidence and the
URL as provenance, so what reaches the brain is exactly what was previewed.
Pushing twice changes nothing.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import psycopg

from app.agents.research import parse_claims
from app.brain.write_gate import BrainWriter, FactCandidate
from app.gateway import Gateway
from app.judge.screening import Screener, as_quoted_data
from app.knowledge.extract import extract_text
from app.knowledge.fetch import FetchedPage

#: How much page text the extraction call reads. A cost ceiling, not a
#: quality setting: long pages are cut, and their claims are still checked
#: against the full saved text when pushed.
EXTRACT_CHARS = 12_000
EXTRACT_MAX_TOKENS = 800


class LinkError(ValueError):
    status = 400


class PreviewNotFound(LinkError):
    status = 404


class NotPushable(LinkError):
    status = 409


@dataclass(frozen=True)
class Preview:
    id: UUID
    url: str
    final_url: str
    label: str
    reasons: tuple[str, ...]
    claims: tuple[str, ...]
    status: str
    results: tuple[dict[str, Any], ...] = ()
    created: bool = True


class Links:
    def __init__(
        self,
        connection: psycopg.Connection,
        *,
        gateway: Gateway,
        screener: Screener,
        writer: BrainWriter,
    ) -> None:
        self._connection = connection
        self._gateway = gateway
        self._screener = screener
        self._writer = writer

    def preview(self, page: FetchedPage, *, org_id: UUID | str, agent_id: UUID | str) -> Preview:
        """Screen a fetched page and propose its claims. Writes no facts."""
        digest = hashlib.sha256(page.content).hexdigest()
        existing = self._find(org_id, page.final_url, digest)
        if existing is not None:
            return existing

        text = extract_text(page.content, page.content_type)
        if not text.strip():
            raise LinkError("The page has no readable text (it may need JavaScript)")
        screening = self._screener.screen(
            text,
            purpose=f"Facts for the company brain from {page.final_url}",
            source_kind="web_page",
            source_ref=page.final_url,
            org_id=org_id,
            agent_id=agent_id,
        )
        claims: list[str] = []
        if screening.label == "clean":
            claims = self._extract(text, page.final_url, agent_id)

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.link_previews
                    (org_id, agent_id, url, final_url, content_sha256, text, label, reasons,
                     claims)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    str(org_id),
                    str(agent_id),
                    page.url,
                    page.final_url,
                    digest,
                    text,
                    screening.label,
                    json.dumps(list(screening.reasons)),
                    json.dumps(claims),
                ),
            )
            preview_id = cursor.fetchone()["id"]
        self._emit(
            org_id,
            agent_id,
            "link_previewed",
            {
                "preview_id": str(preview_id),
                "url": page.final_url,
                "label": screening.label,
                "claims": len(claims),
            },
        )
        return Preview(
            preview_id,
            page.url,
            page.final_url,
            screening.label,
            tuple(screening.reasons),
            tuple(claims),
            "previewed",
        )

    def push(self, preview_id: UUID | str, *, org_id: UUID | str) -> Preview:
        """Send a clean preview's claims through the brain write gate."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                "select * from public.link_previews where id = %s and org_id = %s for update",
                (str(preview_id), str(org_id)),
            )
            row = cursor.fetchone()
        if row is None:
            raise PreviewNotFound(f"No preview {preview_id}")
        if row["status"] == "pushed":
            return _preview(row, created=False)
        if row["label"] != "clean":
            raise NotPushable(f"The page was screened {row['label']}; it cannot be pushed")

        host = urlsplit(row["final_url"]).hostname or "web"
        results = []
        for claim in row["claims"]:
            result = self._writer.propose(
                FactCandidate(
                    claim=claim,
                    source=f"web:{host}",
                    source_text=row["text"],
                    source_ref=row["final_url"],
                ),
                org_id=org_id,
                agent_id=row["agent_id"],
                idempotency_key=f"link:{row['id']}:{hashlib.sha256(claim.encode()).hexdigest()[:16]}",
            )
            results.append(
                {
                    "claim": claim,
                    "outcome": result.outcome,
                    "fact_id": str(result.fact.id) if result.fact else None,
                    "reasons": list(result.reasons),
                }
            )
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                update public.link_previews
                   set status = 'pushed', results = %s, pushed_at = now()
                 where id = %s
                returning *
                """,
                (json.dumps(results), str(row["id"])),
            )
            updated = cursor.fetchone()
        self._emit(
            org_id,
            row["agent_id"],
            "link_pushed",
            {
                "preview_id": str(row["id"]),
                "url": row["final_url"],
                "outcomes": [r["outcome"] for r in results],
            },
        )
        return _preview(updated)

    def _extract(self, text: str, url: str, agent_id: UUID | str) -> list[str]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "select body from public.agent_prompts "
                "where agent_id = %s and slot = 'extract' and active",
                (str(agent_id),),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotPushable("The agent has no active `extract` prompt to read the page with")
        response = self._gateway.complete(
            agent_id=agent_id,
            max_tokens=EXTRACT_MAX_TOKENS,
            messages=[
                {"role": "system", "content": row["body"]},
                {"role": "user", "content": as_quoted_data(text[:EXTRACT_CHARS], source=url)},
            ],
        )
        return parse_claims(response.text)

    def _find(self, org_id: UUID | str, final_url: str, digest: str) -> Preview | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "select * from public.link_previews "
                "where org_id = %s and final_url = %s and content_sha256 = %s",
                (str(org_id), final_url, digest),
            )
            row = cursor.fetchone()
        return _preview(row, created=False) if row else None

    def _emit(
        self, org_id: UUID | str, agent_id: UUID | str, type_: str, payload: dict[str, Any]
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "insert into public.events (org_id, agent_id, type, payload) "
                "values (%s, %s, %s, %s)",
                (str(org_id), str(agent_id), type_, json.dumps(payload)),
            )


def _preview(row: dict[str, Any], *, created: bool = True) -> Preview:
    return Preview(
        id=row["id"],
        url=row["url"],
        final_url=row["final_url"],
        label=row["label"],
        reasons=tuple(row["reasons"]),
        claims=tuple(row["claims"]),
        status=row["status"],
        results=tuple(row["results"] or ()),
        created=created,
    )
