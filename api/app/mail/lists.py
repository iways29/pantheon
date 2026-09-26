"""Mailing lists: who gets which email (owner configuration, ADR 028).

Changed by a person only (the database refuses an agent), and every change
is an event. The owner API and `python -m scripts.mailing` use this; the
recipients form in the Control Center will too.
"""

from typing import Any
from uuid import UUID

import psycopg
from pydantic import BaseModel, Field

from app.db import acting_as
from app.mail.outbox import list_view


class MailingListChange(BaseModel):
    """Fields to change; anything left out stays as it is."""

    name: str | None = None
    from_address: str | None = None
    reply_to: str | None = None
    recipients: list[str] | None = Field(default=None, max_length=50)
    subject: str | None = None
    timezone: str | None = None
    send_without_approval: bool | None = None
    enabled: bool | None = None


def get_lists(connection: psycopg.Connection, *, user_id: UUID | str) -> list[dict[str, Any]]:
    with acting_as(connection, user_id=str(user_id)) as conn:
        rows = conn.execute("select * from public.mailing_lists order by key").fetchall()
    return [list_view(r) for r in rows]


def set_list(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    key: str,
    change: MailingListChange,
) -> dict[str, Any]:
    """Create the list or change the given fields. A new list needs a sender."""
    fields = change.model_dump(exclude_none=True)
    if "recipients" in fields:
        fields["recipients"] = [r.strip().lower() for r in fields["recipients"] if r.strip()]
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select id from public.mailing_lists where org_id = %s and key = %s",
            (str(org_id), key),
        )
        if cursor.fetchone() is None:
            if not fields.get("from_address"):
                raise ValueError("A new mailing list needs a from address")
            fields.setdefault("name", key.replace("-", " ").capitalize())
            columns = ["org_id", "key", *fields]
            cursor.execute(
                f"insert into public.mailing_lists ({', '.join(columns)}) "
                f"values ({', '.join(['%s'] * len(columns))}) returning *",
                (str(org_id), key, *fields.values()),
            )
        elif fields:
            cursor.execute(
                f"update public.mailing_lists set {', '.join(f'{c} = %s' for c in fields)} "
                "where org_id = %s and key = %s returning *",
                (*fields.values(), str(org_id), key),
            )
        else:
            cursor.execute(
                "select * from public.mailing_lists where org_id = %s and key = %s",
                (str(org_id), key),
            )
        return list_view(cursor.fetchone())
