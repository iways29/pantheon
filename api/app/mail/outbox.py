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
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg

from app.db import as_service_role
from app.mail import theme
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
    subject: str | None = None,
    approval_links: bool = False,
    request_form: bool = False,
) -> Composed:
    """Write an email for `list_key`. Writing it twice changes nothing.

    `subject` replaces the list's own subject (both may use `{date}`).
    `approval_links`: when sent, the email ends with one-tap links to the
    approvals then pending, if its list has them switched on.
    `request_form`: and with the form that asks for tomorrow's topics, if the
    list offers any routines."""
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
    subject = (subject or mailing_list["subject"]).replace("{date}", f"{when:%A %-d %B %Y}")
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
             from_address, recipients, subject, body_text, body_html, idempotency_key,
             approval_links, request_form)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            plain_text(body),
            render_html(subject, body),
            idempotency_key,
            approval_links,
            request_form,
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
    with as_service_role(connection) as conn:
        row = conn.execute(
            "select reply_to, approval_links_url, approval_link_hours, request_routines "
            "from public.mailing_lists "
            "where org_id = %s and key = %s",
            (str(email["org_id"]), email["list_key"]),
        ).fetchone()
    reply_to = row["reply_to"] if row else None
    text, body_html = email["body_text"], email["body_html"]
    if (email["approval_links"] or email["request_form"]) and row and row["approval_links_url"]:
        text, body_html = _with_links(connection, email, row, text, body_html)
    try:
        provider_id = mailer.send(
            Outgoing(
                from_address=email["from_address"],
                recipients=list(email["recipients"]),
                subject=email["subject"],
                text=text,
                html=body_html,
                idempotency_key=f"pantheon-email-{email['id']}",
                reply_to=reply_to,
            )
        )
    except MailError as error:
        return _record(connection, email_id, "failed", error=str(error))
    return _record(connection, email_id, "sent", provider_id=provider_id)


def _with_links(
    connection: psycopg.Connection,
    email: dict[str, Any],
    mailing_list: dict[str, Any],
    text: str,
    body_html: str,
) -> tuple[str, str]:
    """The email with its approval links and its topic form added, each as
    asked. Minted now, never stored in the email row, so no agent can read a
    token."""
    from app.approvals.links import mint, section_html, section_text
    from app.mail import requests

    texts: list[str] = []
    htmls: list[str] = []
    if email["approval_links"]:
        minted = mint(
            connection,
            email=email,
            base_url=mailing_list["approval_links_url"],
            hours=mailing_list["approval_link_hours"],
        )
        if minted.links:
            texts.append(section_text(minted))
            htmls.append(section_html(minted))
    if email["request_form"]:
        form = requests.mint(
            connection,
            email=email,
            base_url=mailing_list["approval_links_url"],
            hours=mailing_list["approval_link_hours"],
            routines=list(mailing_list.get("request_routines") or []),
        )
        if form is not None:
            texts.append(requests.section_text(form))
            htmls.append(requests.section_html(form))
    if not texts:
        return text, body_html
    section = "".join(htmls)
    # Above the footer, else before </body>, else at the end.
    at = body_html.find(_FOOTER)
    if at < 0:
        at = body_html.rfind("</body>")
    if at < 0:
        at = len(body_html)
    return "\n\n".join([text, *texts]), body_html[:at] + section + body_html[at:]


_FOOTER = f'<p style="color:{theme.INK2};font-size:12px;margin:28px 0 0">'
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def plain_text(body: str) -> str:
    """The text version: models write **bold**; mail clients show the stars."""
    return _BOLD.sub(r"\1", body)


def render_html(subject: str, body: str) -> str:
    """The brief as simple HTML: paragraphs, line breaks and **bold**, all escaped."""
    paragraphs = [p.strip() for p in body.strip().split("\n\n") if p.strip()]
    inner = "\n".join(
        f'<p style="margin:0 0 14px;color:{theme.INK}">'
        + "<br>".join(
            _BOLD.sub(r"<strong>\1</strong>", html.escape(line)) for line in p.splitlines()
        )
        + "</p>"
        for p in paragraphs
    )
    # Pantheon's look (deep navy, gold), in tables and inline styles: what
    # mail clients keep. Dark by design, so a client's dark mode leaves it be.
    t = theme
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<meta name="supported-color-schemes" content="dark"></head>'
        f'<body style="margin:0;padding:0;background:{t.BG}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{t.BG}" style="background:{t.BG}">'
        '<tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{t.SURFACE}" style="max-width:620px;background:{t.SURFACE};'
        f'border:1px solid {t.LINE};border-radius:18px">'
        f'<tr><td style="padding:26px 24px 24px;font-family:{t.FONT};font-size:15px;'
        f'line-height:1.55;color:{t.INK}">'
        f'<p style="margin:0 0 6px;color:{t.GOLD};font-size:11px;letter-spacing:3px;'
        'text-transform:uppercase">&#9678;&nbsp; Pantheon</p>'
        f'<h1 style="margin:0 0 18px;font:400 24px/1.25 {t.SERIF};color:{t.INK}">'
        f"{html.escape(subject)}</h1>"
        f"{inner}{_FOOTER}Written by Pantheon.</p>"
        "</td></tr></table></td></tr></table></body></html>"
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
        "approval_links_url",
        "approval_link_hours",
        "request_routines",
        "updated_at",
    )
    return {k: row[k] for k in keys}
