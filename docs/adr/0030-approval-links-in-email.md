# ADR 030: One-tap approval links in the brief email

- **Status:** Proposed (owner chose the feature, 2026-09-26)
- **Date:** 2026-09-27
- **Step:** after 8.3, before Step 9 (owner's order)

## Context

Approvals wait for the owner (drafts, held facts, R4 tool calls), but
deciding them needs the command line. The owner reads the brief on their
phone at 07:15 and wants to decide from there.

## Decision

**One link per pending approval, at the end of the brief email.** The link
opens a page on the API (`GET /a/{token}`) with the card: what is held, who
asked, Jev's recommendation, why it waits. **Opening decides nothing**: mail
scanners and link previews open links. Buttons post the form
(`POST /a/{token}`): Approve, Reject, or Send back with a note (tool calls).
A held fact is admitted to the brain as internal, as `scripts.brain admit`
does; making a fact public stays a CLI step.

**Tokens are minted at send time, by the backend.** 32 random bytes; only the
SHA-256 hash is stored (`approval_links`). The stored email body has no
token, so no agent can read one (agents may read `emails`, not
`approval_links`). A retry of the same email replaces its unused links.

**A link works once, until it expires, and only while its approval is
pending.** The first press claims it atomically; then `decide` runs as the
org's owner, exactly as the owner API does, and a note is remembered as the
owner's rule. Every mint and use is an event, and `decide_approval` records
the decision as before.

**Configuration is data.** `mailing_lists.approval_links_url` (the API's
https address; null means no links) and `approval_link_hours` (default 48,
1 to 168), set with `scripts.mailing set <list> --links <url>`. An email
carries links only when its writer asks (the brief does; the evening
question does not). At most 20 links; the rest are counted.

## Consequences

- Anyone holding the email can decide for 48 hours. Fine while the list is
  the owner alone; **switch links off (`--no-links`) before adding anyone
  else to a list that carries them.**
- No new service, dependency or secret; one additive migration.
- The page is plain HTML with a strict Content-Security-Policy, `no-store`
  and `no-referrer`, since the token is in the address.
- Later: the Control Center's decision desk replaces the page; the links
  can point there instead.
