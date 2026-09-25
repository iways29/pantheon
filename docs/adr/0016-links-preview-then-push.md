# ADR 016: Links are previewed, then pushed; fetched safely and for free

- **Status:** Accepted (owner chose plain HTTP fetch, open decision 9, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 6 (configuration backend)

## Context

The owner wants to paste a link and have its facts reach the brain, without
anything reaching the brain unseen. Fetching arbitrary URLs from a server is
also a classic way to reach internal services. The owner chose the free
option: fetch pages ourselves, no JavaScript rendering, and a paid service
only with approval later.

## Decision

- **Safe fetch** (`app/knowledge/fetch.py`): https on the default port only,
  no credentials in the URL; the host must resolve only to public addresses
  (private, loopback, link-local such as cloud metadata, reserved and
  multicast are refused); redirects followed by hand, at most five, each hop
  checked the same way; at most 2 MB in 10 seconds; HTML or plain text only.
- **Step 1, preview** (`POST /links/preview`, `scripts.knowledge link
  preview`): fetch, extract text, screen it (ADR 011). Only a clean page is
  read by a model: the chosen agent's own `extract` prompt (from the
  database) with the page handed over as quoted data, capped at 12,000
  characters. The preview is saved in `link_previews` with the text, the
  label and the proposed claims. **No fact is written.** Previewing the same
  content again returns the same preview.
- **Step 2, push** (`POST /links/{id}/push`, `scripts.knowledge link push`):
  a separate, explicit action. Each claim goes through the brain write gate
  (ADR 010) with the saved page text as evidence, `source = web:<host>` and
  `source_ref` = the final URL. A page that was not clean cannot be pushed.
  Pushing twice changes nothing. Both steps are events (`link_previewed`,
  `link_pushed`).

## Consequences

- A preview costs one screening call per chunk, one cheap-tier extraction
  call, and nothing more; a push costs the write gate's calls per claim.
  Well under a cent per page.
- Pages that build their text with JavaScript come back thin or empty; the
  preview says so. A rendering service would be a new paid service and needs
  the owner's approval.
- DNS is checked before connecting, and the connection resolves the name
  again. A DNS-rebinding attacker could in principle switch addresses in
  between; the only thing reachable that way would be read-only GETs whose
  text is then screened. Pinning the connection to the checked address is a
  later hardening step.
