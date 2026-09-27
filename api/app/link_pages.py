"""The page an approval link in an email opens (owner, 2026-09-26).

GET shows the card and the buttons; it decides nothing, because mail
scanners and link previews open links. POST (a button) decides, once,
through the same path as the owner API: `decide`, then, for a held fact,
admitting it to the brain. See `app.approvals.links`.
"""

import html
import json
from typing import Any
from urllib.parse import parse_qs

import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.approvals.links import claim, lookup, title
from app.db import acting_as, as_service_role
from app.owner_api import Connection, MailerDep, MakeWriter, WriterFactory, _decide

router = APIRouter(include_in_schema=False)

#: The token is in the address: keep it out of caches, referrers and indexes.
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}

GONE = (
    "This link has expired, was already used, or its request was already decided. "
    "See what is waiting with scripts.approvals list."
)

#: What each button means, per kind of approval.
BUTTONS: dict[str, list[tuple[str, str]]] = {
    "fact_write": [("approve", "Admit to the brain"), ("cancel", "Reject")],
    "send_email": [("approve", "Send it"), ("cancel", "Don't send")],
}
DEFAULT_BUTTONS = [
    ("approve", "Approve"),
    ("cancel", "Reject"),
    ("redirect", "Send back with my note"),
]


def _page(body: str, code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Pantheon decision</title></head>"
        '<body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:16px;line-height:1.5;color:#1a1a1a;max-width:620px;margin:0 auto;padding:16px">'
        f"{body}</body></html>",
        status_code=code,
        headers=HEADERS,
    )


def _message(text: str, code: int = 200) -> HTMLResponse:
    return _page(f"<p>{html.escape(text)}</p>", code)


def _detail(card: dict[str, Any]) -> str:
    payload = card.get("payload") or {}
    if card["action_type"] == "fact_write":
        lines = [f"Source: {payload.get('source') or '-'}"]
        lines += [f"Why held: {r}" for r in payload.get("reasons") or []]
        return "\n".join(lines)
    if card["action_type"] == "send_email":
        return f"To: {', '.join(payload.get('to') or [])}\n\n{payload.get('text', '')}"[:3000]
    if payload.get("tool"):
        return f"{payload['tool']}\n{json.dumps(payload.get('arguments'), indent=2)}"[:3000]
    return str(payload.get("order") or card.get("explanation") or "")[:3000]


@router.get("/a/{token}")
def show(token: str, connection: Connection) -> HTMLResponse:
    card = lookup(connection, token)
    if card is None:
        return _message(GONE, 410)
    rows = [
        ("Who", card.get("agent") or "-"),
        ("Task", card.get("task_title") or "-"),
        ("Recommendation", card.get("recommendation") or "-"),
        ("Why it waits", card.get("explanation") or "-"),
        ("Expires", f"{card['expires_at']:%Y-%m-%d %H:%M} UTC"),
    ]
    table = "".join(
        f'<tr><th style="text-align:left;padding-right:12px;vertical-align:top">{k}</th>'
        f"<td>{html.escape(str(v))}</td></tr>"
        for k, v in rows
    )
    buttons = "".join(
        f'<button name="decision" value="{value}" style="font-size:16px;padding:10px 14px;'
        f'margin:4px 8px 4px 0">{html.escape(label)}</button>'
        for value, label in BUTTONS.get(card["action_type"], DEFAULT_BUTTONS)
    )
    return _page(
        f'<h2 style="font-size:19px">{html.escape(title(card))}</h2>'
        f"<table>{table}</table>"
        f'<pre style="white-space:pre-wrap;background:#f4f4f4;padding:8px">'
        f"{html.escape(_detail(card))}</pre>"
        '<form method="post">'
        '<label for="note">Note (optional; a note is remembered as your rule):</label><br>'
        '<textarea id="note" name="note" rows="3" maxlength="1000" style="width:100%"></textarea>'
        f"<p>{buttons}</p></form>"
        '<p style="color:#777;font-size:13px">This link works once.</p>'
    )


@router.post("/a/{token}")
async def decide_by_link(
    token: str,
    request: Request,
    connection: Connection,
    make_writer: WriterFactory,
    mailer: MailerDep,
) -> HTMLResponse:
    form = parse_qs((await request.body()).decode("utf-8", "replace"), max_num_fields=4)
    decision = (form.get("decision") or [""])[0]
    note = (form.get("note") or [""])[0].strip()[:1000] or None
    if decision not in ("approve", "cancel", "redirect"):
        return _message("Pick one of the buttons.", 400)
    if decision == "redirect" and not note:
        return _message("Sending it back needs a note: go back and write one.", 400)
    card = lookup(connection, token)
    if card is None:
        return _message(GONE, 410)
    if decision == "redirect" and card["action_type"] in BUTTONS:
        return _message("That button is not offered for this request.", 400)
    link = claim(connection, token, decision)
    if link is None:
        return _message(GONE, 410)
    user_id = str(link["user_id"])
    try:
        result = _decide(
            connection,
            user_id,
            link["approval_id"],
            decision,
            note,
            None,
            make_writer,
            mailer if decision == "approve" else None,
        )
    except HTTPException as error:
        return _message(f"Could not decide: {error.detail}", error.status_code)
    _event(connection, link, decision)
    said = {"approve": "Approved", "cancel": "Rejected", "redirect": "Sent back with your note"}
    lines = [f"{said[decision]}: {title(card)}"]
    if card["action_type"] == "fact_write" and result["status"] == "approved":
        lines.append(_admit(connection, user_id, link, make_writer))
    if result.get("email_status"):
        lines.append(f"Email: {result['email_status']}.")
    if result.get("remembered"):
        lines.append(f"Your note was sent to the brain ({result['remembered']}).")
    return _page("".join(f"<p>{html.escape(line)}</p>" for line in lines))


def _admit(
    connection: psycopg.Connection, user_id: str, link: dict[str, Any], make_writer: MakeWriter
) -> str:
    """Store the approved fact, as `scripts.brain admit` does (internal). Any
    judging it needs is paid by the agent that proposed it, else the researcher."""
    with as_service_role(connection) as conn:
        approval = conn.execute(
            """
            select ap.*, coalesce(r.agent_id,
                   (select a.id from public.agents a where a.org_id = ap.org_id
                     order by a.name <> 'researcher', a.created_at limit 1)) as payer
              from public.approvals ap left join public.runs r on r.id = ap.run_id
             where ap.id = %s
            """,
            (str(link["approval_id"]),),
        ).fetchone()
    if approval["payer"] is None:
        return "Approved, but not stored: run scripts.brain admit to store it."
    try:
        with acting_as(connection, user_id=user_id) as conn:
            result = make_writer(conn, approval["payer"]).admit_approved(
                dict(approval), agent_id=approval["payer"]
            )
    except ValueError as error:
        return f"Approved, but not stored: {error}"
    return f"Stored in the brain ({result.outcome})."


def _event(connection: psycopg.Connection, link: dict[str, Any], decision: str) -> None:
    with as_service_role(connection) as conn:
        conn.execute(
            "insert into public.events (org_id, type, payload) "
            "values (%s, 'approval_link_used', %s)",
            (
                str(link["org_id"]),
                json.dumps(
                    {
                        "link_id": str(link["id"]),
                        "approval_id": str(link["approval_id"]),
                        "decision": decision,
                        "decided_by": str(link["user_id"]),
                    }
                ),
            ),
        )
