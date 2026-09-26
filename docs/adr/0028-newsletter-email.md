# ADR 028: The morning brief by email, through Resend, to a mailing list in the database

- **Status:** Accepted (owner's go, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 8.2 (the morning brief), groundwork for the Control Center's newsletter section (Step 11)

## Context

The owner wants the morning brief in their inbox, sent from their own domain
through Resend, which they already use. More recipients will be added later
from a form, so who gets it cannot be in code. Sending email is an external,
irreversible action: by the hard rules it needs the owner's approval unless
the owner decides otherwise.

## Decision

**Resend's REST API, not its MCP server.** One `POST /emails` per email with
an `Idempotency-Key` (Resend keeps it 24 hours). The key is
`pantheon-email-<email id>`, so a serverless retry never delivers twice. The
API key is `RESEND_API_KEY`, server-side only. Resend is already the owner's
paid service, so nothing new is bought; the MCP route would add a sign-in
and a tool call for what is one fixed request.

**`mailing_lists` (owner configuration).** Per list: sender, reply-to,
recipients (at most 50, Resend's limit), subject with `{date}`, time zone,
`send_without_approval` (off by default), on/off. Only a person may change
it (RLS); every change is an event. Edited with `scripts.mailing`,
`PUT /mailing-lists/{key}`, and later the Control Center form.

**`emails` (the outbox).** One row per email, unique by idempotency key.
Outside the backend, the database fixes an email's sender and recipients to
its list's and allows `ready` only when the list goes out on its own;
otherwise it must be `held` behind a pending `send_email` approval. An agent
therefore cannot choose recipients or skip approval. Only the backend moves
an email to `sending`, `sent` or `failed`.

**Approval and the kill.** A trigger moves the email with its card: approved
makes it `ready` (the approve call then sends it); a cancel or the kill
cancels it.

**The brief.** The Executive charter's routine names the list
(`mailing_list: morning-brief`). After writing the brief, the `digest` runner
writes the email in its own transaction and sends it once that has
committed, never inside it. A failed send is recorded on the email (at most
three attempts, retried with `scripts.mailing send` or
`POST /emails/{id}/send`); the brief itself still succeeds.

## Consequences

- Resend's free tier covers phase 1 (one email a day).
- The sending domain must be verified in Resend, or the send fails with
  Resend's reason stored on the email.
- Until the owner turns on `send_without_approval`, each brief waits on an
  approval card, and there is no approvals screen before Step 10: approve
  with `scripts.approvals approve <id>` or the API.
