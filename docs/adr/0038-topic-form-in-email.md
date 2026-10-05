# ADR 038: Ask for tomorrow's topics from the email

- **Status:** Proposed (owner asked, 2026-10-05)
- **Date:** 2026-10-05
- **Step:** 10 upgrades

## Context

The owner asks for tomorrow's research with `scripts.department ask`, or by
answering the evening question from a laptop. They read the brief on their
phone and want to ask from there: a form in the email, with a Submit button.

## Decision

**The brief and the evening question end with a form**: which routine
(Research, Marketing), a text box, and "Submit for tomorrow". Submitting adds
a `routine_requests` row as the owner, exactly as `scripts.department ask`
does; the routine's next run uses it once.

**Forms in email work in Apple Mail and Gmail on the web, not in Gmail's
phone app or Outlook.** So the section also links to the same form as a page
on the API (`GET /r/{token}`), and the text version carries that link.

**Same safety as approval links (ADR 030).** The token is minted when the
email is sent, by the backend; only its SHA-256 hash is stored, never the
token in a row an agent can read. A form works for the list's
`approval_link_hours` and takes at most 5 topics. Each use is an event.

**Configuration is data.** `mailing_lists.request_routines` names the
routines the form offers (empty: no form; needs `approval_links_url`), set
with `scripts.mailing set <list> --requests research:morning-brief,...`.

**The emails take Pantheon's look**: navy, warm ink, gold, in tables and
inline styles (what mail clients keep).

## Consequences

- Anyone holding the email can add up to 5 topics for 48 hours. Fine while
  the list is the owner alone; `--no-requests` before adding anyone else.
- No new service or dependency; one additive migration.
