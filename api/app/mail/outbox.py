"""Emails: written by an agent, approved by the owner if the list says so,
sent once by the backend (ADR 028).

- `compose` writes the email for a mailing list, inside the agent's own
  transaction. The database fixes its sender and recipients to the list's,
  and it is `ready` only when the owner let the list's emails go out on their
  own; otherwise it is `held` behind a `send_email` approval card.
- The owner's approval moves the email to `ready` (a database trigger); a
  cancel, or the kill, cancels it.
- `send` claims a ready (or failed) email, sends it through the mailer with
  the email's idempotency key, and records the result. Only the backend does
  this.
"""

import html
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg

from app.db import as_service_role
from app.mail.resend import Mailer, MailError, Outgoing

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Composed:
    email_id: UUID | None
    status: str  # held | ready | skipped
    reason: str | None = None
    approval_id: UUID | None = None


def compose(
    cursor: psycopg.Cursor,
    *,
    org_id: UUID,
    list_key: str,
    body: str,
    idempotency_key: str,
    task_id: UUID | None = None,
    run_id: UUID | None = None,
    agent_id: UUID | None = None,
    now: datetime | None = None,
) -> Composed:
    """Write an email for `list_key`. Writing it twice changes nothing."""
    cursor.execute(
        "select * from public.mailing_lists where org_id = %s and key = %s",
        (str(org_id), list_key),
    )
    mailing_list = cursor.fetchone()
    if mailing_list is None:
        return Composed(None, "skipped", f"no mailing list {list_key!r}")
    if not mailing_list["enabled"]:
        return Composed(None, "skipped", f"mailing list {list_key!r} is switched off")
    if not mailing_list["recipients"]:
        return Composed(None, "skipped", f"mailing list {list_key!r} has no recipients")
    cursor.execute(
        "select id, status, approval_id from public.emails "
        "where org_id = %s and idempotency_key = %s",
        (str(org_id), idempotency_key),
    )
    existing = cursor.fetchone()
    if existing is not None:
        return Composed(existing["id"], existing["status"], approval_id=existing["approval_id"])

    when = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(mailing_list["timezone"]))
    subject = mailing_list["subject"].replace("{date}", f"{when:%A %-d %B %Y}")
    approval_id = None
    status = "ready"
    if not mailing_list["send_without_approval"]:
        status = "held"
        cursor.execute(
            """
            insert into public.approvals
                (org_id, run_id, agent_id, task_id, action_type, action_key, payload,
                 agent_output_snapshot, idempotency_key, explanation)
            values (%s, %s, %s, %s, 'send_email', %s, %s, %s, %s, %s)
            returning id
            """,
            (
                str(org_id),
                str(run_id) if run_id else None,
                str(agent_id) if agent_id else None,
                str(task_id) if task_id else None,
                f"email:{list_key}",
                json.dumps(
                    {
                        "list": list_key,
                        "from": mailing_list["from_address"],
                        "to": mailing_list["recipients"],
                        "subject": subject,
                        "text": body,
                    }
                ),
                json.dumps({"subject": subject, "text": body}),
                f"approval:{idempotency_key}",
                f"Send {subject!r} to {len(mailing_list['recipients'])} recipient(s)?",
            ),
        )
        approval_id = cursor.fetchone()["id"]
    cursor.execute(
        """
        insert into public.emails
            (org_id, list_key, task_id, run_id, agent_id, approval_id, status,
             from_address, recipients, subject, body_text, body_html, idempotency_key)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            str(org_id),
            list_key,
            str(task_id) if task_id else None,
            str(run_id) if run_id else None,
            str(agent_id) if agent_id else None,
            str(approval_id) if approval_id else None,
            status,
            mailing_list["from_address"],
            mailing_list["recipients"],
            subject,
            body,
            render_html(subject, body),
            idempotency_key,
        ),
    )
    return Composed(cursor.fetchone()["id"], status, approval_id=approval_id)


def send(connection: psycopg.Connection, mailer: Mailer | None, email_id: UUID | str) -> str:
    """Send one ready email; returns its status afterwards. Safe to call twice:
    only one caller claims it, and a sent email is never sent again."""
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            update public.emails set status = 'sending', attempts = attempts + 1
             where id = %s and (status = 'ready' or (status = 'failed' and attempts < %s))
            returning *
            """,
            (str(email_id), MAX_ATTEMPTS),
        )
        email = cursor.fetchone()
    if email is None:
        return _status(connection, email_id)
    if mailer is None:
        return _record(connection, email_id, "failed", error="RESEND_API_KEY is not configured")
    reply_to = None
    with as_service_role(connection) as conn:
        row = conn.execute(
            "select reply_to from public.mailing_lists where org_id = %s and key = %s",
            (str(email["org_id"]), email["list_key"]),
        ).fetchone()
        reply_to = row["reply_to"] if row else None
    try:
        provider_id = mailer.send(
            Outgoing(
                from_address=email["from_address"],
                recipients=list(email["recipients"]),
                subject=email["subject"],
                text=email["body_text"],
                html=email["body_html"],
                idempotency_key=f"pantheon-email-{email['id']}",
                reply_to=reply_to,
            )
        )
    except MailError as error:
        return _record(connection, email_id, "failed", error=str(error))
    return _record(connection, email_id, "sent", provider_id=provider_id)


def render_html(subject: str, body: str) -> str:
    """The brief as simple HTML: paragraphs and line breaks, everything escaped."""
    paragraphs = [p.strip() for p in body.strip().split("\n\n") if p.strip()]
    inner = "\n".join(
        f"<p>{'<br>'.join(html.escape(line) for line in p.splitlines())}</p>" for p in paragraphs
    )
    return (
        '<!doctype html><html><body style="font-family:-apple-system,Segoe UI,Helvetica,'
        "Arial,sans-serif;font-size:15px;line-height:1.5;color:#1a1a1a;max-width:620px;"
        f'margin:0 auto;padding:16px"><h2 style="font-size:18px">{html.escape(subject)}</h2>'
        f'{inner}<p style="color:#777;font-size:12px;margin-top:24px">Written by Pantheon.</p>'
        "</body></html>"
    )


def _record(
    connection: psycopg.Connection,
    email_id: UUID | str,
    status: str,
    *,
    error: str | None = None,
    provider_id: str | None = None,
) -> str:
    with as_service_role(connection) as conn:
        conn.execute(
            "update public.emails set status = %s, error = %s, provider_id = %s, "
            "sent_at = case when %s = 'sent' then now() end where id = %s",
            (status, error, provider_id, status, str(email_id)),
        )
    return status


def _status(connection: psycopg.Connection, email_id: UUID | str) -> str:
    with as_service_role(connection) as conn:
        row = conn.execute(
            "select status from public.emails where id = %s", (str(email_id),)
        ).fetchone()
    return row["status"] if row else "missing"


def list_view(row: dict[str, Any]) -> dict[str, Any]:
    """A mailing list as the owner sees it."""
    keys = (
        "key",
        "name",
        "from_address",
        "reply_to",
        "recipients",
        "subject",
        "timezone",
        "send_without_approval",
        "enabled",
        "updated_at",
    )
    return {k: row[k] for k in keys}
