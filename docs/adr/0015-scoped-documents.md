# ADR 015: Documents are screened, then scoped to company, department or agent by RLS

- **Status:** Accepted (owner chose three scopes, open decision 7, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 6 (configuration backend)

## Context

Agents need reference material (the company brief, the voice guide, a
department's playbook) that is not the same as checked facts. The owner
chose three scopes over two: company, department and agent.

## Decision

- **Two tables.** `documents` (source material, kept apart from `facts`) and
  `document_chunks` (about 1,500 characters each, embedded through the
  gateway, ADR 013). The original file goes to a private Supabase Storage
  bucket, `documents`, created by the migration where the storage schema
  exists.
- **Scope is enforced by RLS, not by code.** A session can say it acts for an
  agent (`acting_as(..., agent_id=...)` sets `pantheon.agent_id`). Such a
  session reads company chunks, its own department's and its own; a person's
  session reads everything in the org. Naming an agent only ever narrows
  what is visible, so there is nothing to gain by naming another one. Every
  agent run now acts for its agent. Only a person's session may add
  documents.
- **Screened before chunked.** Text is extracted (standard library only:
  text, Markdown, CSV, JSON, HTML with HTML comments kept, so a hidden
  instruction is screened), then run through `content_screen` (ADR 011).
  Clean: chunked and embedded. Review: a pending `document_review` approval.
  Quarantined: kept for the record, never chunked, so no agent ever sees it.
- **Idempotent.** The same content in the same scope is the same document; a
  re-upload returns it and costs nothing.
- **Facts link back.** A fact proposed from a document carries its
  `document_id` through the write gate (ADR 010).
- **Uploads** are `POST /documents` with the file as the raw body and its
  details in the query string (no new dependency), or
  `python -m scripts.knowledge add`. Screening and embedding are paid by a
  named agent's department (`processed_by`).

## Consequences

- PDF and Word files are refused until the owner approves a parser library.
- A 20-page text document costs about $0.0005 to screen and embed.
- Going live needs `SUPABASE_SERVICE_ROLE_KEY` in Vercel. Supabase is
  retiring the legacy `service_role` key at the end of 2026; the client
  accepts the new `sb_secret_...` keys too.
- Agents do not read documents yet; Step 7.2's tools will call
  `Library.search` inside the agent's scoped session.
