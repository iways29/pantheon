"""One-tap approval links in emails (owner, 2026-09-26).

The brief email ends with one link per approval waiting for the owner. The
link opens a page with the card; only pressing a button decides (mail
scanners open links, so a GET never decides).

- `mint` runs when an email is sent, as the backend: the tokens exist only
  in the email that went out, never in a row an agent can read. Only their
  SHA-256 hashes are stored.
- `lookup` finds a live link: not used, not expired, its approval pending.
- `claim` uses a link once. The caller then decides through `decide`, as the
  link's person, exactly as the owner API does.
"""

import hashlib
import html
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from app.db import as_service_role

#: How many approvals an email links at most; the rest are counted.
MAX_LINKS = 20


@dataclass(frozen=True)
class Link:
    approval_id: UUID
    title: str
    url: str


@dataclass(frozen=True)
class Minted:
    links: list[Link]
    more: int
    hours: int


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def title(approval: dict[str, Any]) -> str:
    """One short line naming what is held."""
    payload = approval.get("payload") or {}
    if approval["action_type"] == "fact_write":
        return f"Fact: {payload.get('claim', '')}"[:160]
    if approval["action_type"] == "send_email":
        return f"Email: {payload.get('subject', '')}"[:160]
    agent = f" ({approval['agent']})" if approval.get("agent") else ""
    what = payload.get("order") or payload.get("tool") or approval.get("action_key") or "action"
    return f"{what}{agent}"[:160]


def mint(
    connection: psycopg.Connection, *, email: dict[str, Any], base_url: str, hours: int
) -> Minted:
    """Fresh links for the approvals pending now, for this email. Links minted
    for an earlier try at sending the same email stop working."""
    now = datetime.now(UTC)
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select user_id from public.org_members where org_id = %s and role = 'owner' "
            "order by created_at limit 1",
            (str(email["org_id"]),),
        )
        owner = cursor.fetchone()
        if owner is None:
            return Minted([], 0, hours)
        cursor.execute(
            "delete from public.approval_links where email_id = %s and used_at is null",
            (str(email["id"]),),
        )
        cursor.execute(
            """
            select ap.id, ap.action_type, ap.action_key, ap.payload, a.name as agent
              from public.approvals ap left join public.agents a on a.id = ap.agent_id
             where ap.org_id = %s and ap.status = 'pending' and ap.id is distinct from %s
             order by ap.created_at
            """,
            (str(email["org_id"]), email.get("approval_id")),
        )
        pending = cursor.fetchall()
        links = []
        for approval in pending[:MAX_LINKS]:
            token = secrets.token_urlsafe(32)
            cursor.execute(
                "insert into public.approval_links "
                "(org_id, approval_id, email_id, user_id, token_hash, expires_at) "
                "values (%s, %s, %s, %s, %s, %s)",
                (
                    str(email["org_id"]),
                    str(approval["id"]),
                    str(email["id"]),
                    str(owner["user_id"]),
                    token_hash(token),
                    now + timedelta(hours=hours),
                ),
            )
            links.append(Link(approval["id"], title(approval), f"{base_url.rstrip('/')}/a/{token}"))
        if links:
            cursor.execute(
                "insert into public.events (org_id, type, payload) "
                "values (%s, 'approval_links_minted', %s)",
                (
                    str(email["org_id"]),
                    json.dumps(
                        {
                            "email_id": str(email["id"]),
                            "approvals": [str(link.approval_id) for link in links],
                            "hours": hours,
                        }
                    ),
                ),
            )
    return Minted(links, max(len(pending) - MAX_LINKS, 0), hours)


def section_text(minted: Minted) -> str:
    lines = [f"Decide from here (each link works once, for {minted.hours} hours):"]
    lines += [f"- {link.title}\n  {link.url}" for link in minted.links]
    if minted.more:
        lines.append(f"And {minted.more} more: scripts.approvals list")
    return "\n".join(lines)


def section_html(minted: Minted) -> str:
    items = "".join(
        f'<li style="margin-bottom:6px">{html.escape(link.title)}<br>'
        f'<a href="{html.escape(link.url)}">Open to decide</a></li>'
        for link in minted.links
    )
    more = (
        f"<p>And {minted.more} more: <code>scripts.approvals list</code></p>" if minted.more else ""
    )
    return (
        '<h3 style="font-size:16px;margin-top:24px">Decide from here</h3>'
        f'<p style="color:#555;font-size:13px">Each link works once, for {minted.hours} hours. '
        "Opening it decides nothing; the buttons on the page do.</p>"
        f"<ul>{items}</ul>{more}"
    )


def lookup(connection: psycopg.Connection, token: str) -> dict[str, Any] | None:
    """The link and its approval card, while the link can still be used."""
    with as_service_role(connection) as conn:
        return conn.execute(
            """
            select l.id as link_id, l.user_id, l.org_id, l.expires_at,
                   ap.id, ap.action_type, ap.action_key, ap.payload, ap.explanation,
                   ap.recommendation, ap.run_id, ap.task_id, ap.created_at,
                   a.name as agent, t.title as task_title
              from public.approval_links l
              join public.approvals ap on ap.id = l.approval_id
              left join public.agents a on a.id = ap.agent_id
              left join public.tasks t on t.id = ap.task_id
             where l.token_hash = %s and l.used_at is null and l.expires_at > now()
               and ap.status = 'pending'
            """,
            (token_hash(token),),
        ).fetchone()


def claim(connection: psycopg.Connection, token: str, decision: str) -> dict[str, Any] | None:
    """Use the link: only the first press counts. Returns the link, or None."""
    with as_service_role(connection) as conn:
        return conn.execute(
            """
            update public.approval_links l
               set used_at = now(), decision = %s
              from public.approvals ap
             where l.token_hash = %s and l.used_at is null and l.expires_at > now()
               and ap.id = l.approval_id and ap.status = 'pending'
            returning l.id, l.org_id, l.user_id, l.approval_id
            """,
            (decision, token_hash(token)),
        ).fetchone()
