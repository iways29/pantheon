"""Ask for tomorrow's topics from the email itself (owner, 2026-10-05).

The brief and the evening question end with a small form: a text box, which
routine it is for, and Submit. Submitting adds a `routine_requests` row, as
`scripts.department ask` does; the routine's next run uses it once.

- `mint` runs when an email is sent, as the backend: the token exists only in
  the email that went out. Only its SHA-256 hash is stored.
- `lookup` finds a live link: not expired, uses left.
- `use` spends one use and adds the request, as the link's person.

Forms work inside Apple Mail and Gmail on the web; Gmail's phone app and
Outlook do not send them, so the section also links to the same form as a
page (`GET /r/{token}`).
"""

import hashlib
import html
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from app.db import acting_as, as_service_role
from app.mail.theme import BUTTON, CARD, GOLD, INK, INK2, LINE, PANEL

#: Topics one email's form can send.
MAX_USES = 5


@dataclass(frozen=True)
class RequestForm:
    url: str
    routines: list[str]
    hours: int


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def label(routine: str) -> str:
    """research:morning-brief -> Research."""
    return routine.split(":", 1)[0].replace("-", " ").replace("_", " ").capitalize()


def mint(
    connection: psycopg.Connection,
    *,
    email: dict[str, Any],
    base_url: str,
    hours: int,
    routines: list[str],
) -> RequestForm | None:
    """A fresh form link for this email. One minted for an earlier try at
    sending the same email stops working."""
    if not routines:
        return None
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select user_id from public.org_members where org_id = %s and role = 'owner' "
            "order by created_at limit 1",
            (str(email["org_id"]),),
        )
        owner = cursor.fetchone()
        if owner is None:
            return None
        cursor.execute(
            "delete from public.request_links where email_id = %s and uses = 0",
            (str(email["id"]),),
        )
        token = secrets.token_urlsafe(32)
        cursor.execute(
            "insert into public.request_links "
            "(org_id, email_id, user_id, token_hash, routines, expires_at, max_uses) "
            "values (%s, %s, %s, %s, %s, %s, %s)",
            (
                str(email["org_id"]),
                str(email["id"]),
                str(owner["user_id"]),
                token_hash(token),
                routines,
                datetime.now(UTC) + timedelta(hours=hours),
                MAX_USES,
            ),
        )
    return RequestForm(f"{base_url.rstrip('/')}/r/{token}", list(routines), hours)


def lookup(connection: psycopg.Connection, token: str) -> dict[str, Any] | None:
    with as_service_role(connection) as conn:
        return conn.execute(
            "select * from public.request_links where token_hash = %s "
            "and expires_at > now() and uses < max_uses",
            (token_hash(token),),
        ).fetchone()


def use(connection: psycopg.Connection, token: str, routine: str, request: str) -> str | None:
    """Spend one use and add the request. None when the link is spent, gone
    or the routine is not one it offers; else the request's id."""
    with as_service_role(connection) as conn:
        link = conn.execute(
            "update public.request_links set uses = uses + 1, last_used_at = now() "
            "where token_hash = %s and expires_at > now() and uses < max_uses "
            "and %s = any(routines) returning *",
            (token_hash(token), routine),
        ).fetchone()
    if link is None:
        return None
    # As the owner, like `scripts.department ask`: the audit names them.
    with acting_as(connection, user_id=str(link["user_id"])) as conn:
        row = conn.execute(
            "insert into public.routine_requests (org_id, routine_key, request) "
            "values (%s, %s, %s) returning id",
            (str(link["org_id"]), routine, request),
        ).fetchone()
    with as_service_role(connection) as conn:
        conn.execute(
            "insert into public.events (org_id, type, payload) "
            "values (%s, 'request_link_used', %s)",
            (
                str(link["org_id"]),
                json.dumps(
                    {
                        "link_id": str(link["id"]),
                        "routine": routine,
                        "request_id": str(row["id"]),
                        "uses": link["uses"],
                    }
                ),
            ),
        )
    return str(row["id"])


def section_text(form: RequestForm) -> str:
    names = " or ".join(label(r) for r in form.routines)
    return f"Ask for tomorrow: a topic for {names}, added to its next morning run.\n{form.url}"


def form_html(url: str, routines: list[str], *, note: str = "") -> str:
    """The form itself: in the email, and on the page it links to."""
    choices = "".join(
        f'<label style="display:inline-block;margin:0 14px 8px 0;color:{INK};font-size:14px">'
        f'<input type="radio" name="routine" value="{html.escape(r)}"'
        f'{" checked" if i == 0 else ""} style="accent-color:{GOLD}"> {html.escape(label(r))}'
        "</label>"
        for i, r in enumerate(routines)
    )
    return (
        f'<form method="post" action="{html.escape(url)}" style="margin:0">'
        f"{choices}"
        '<textarea name="request" rows="3" maxlength="1000" required '
        'placeholder="e.g. Who is building AI for insurance claims?" '
        f'style="display:block;width:100%;box-sizing:border-box;margin:4px 0 12px;'
        f"padding:12px;border-radius:10px;border:1px solid {LINE};background:{PANEL};"
        f'color:{INK};font:15px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif"></textarea>'
        f'<button type="submit" style="{BUTTON}">Submit for tomorrow</button>'
        f"{note}</form>"
    )


def section_html(form: RequestForm) -> str:
    fallback = (
        f'<p style="margin:12px 0 0;color:{INK2};font-size:12px">'
        "Form not sending from here? "
        f'<a href="{html.escape(form.url)}" style="color:{GOLD}">Open it as a page</a>. '
        f"Works for {form.hours} hours, up to {MAX_USES} topics.</p>"
    )
    return (
        f'<div style="{CARD}">'
        f'<p style="margin:0 0 4px;color:{GOLD};font-size:11px;letter-spacing:2px;'
        'text-transform:uppercase">Ask for tomorrow</p>'
        f'<p style="margin:0 0 14px;color:{INK};font-size:16px">'
        "What should the team look at next? It joins the next morning run.</p>"
        f"{form_html(form.url, form.routines, note=fallback)}"
        "</div>"
    )
