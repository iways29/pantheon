"""Approvals: held actions, the owner's decisions, and resuming (ADR 021).

A held tool call pauses its run at that call (`ApprovalPending`). The owner
decides with `decide`: approve (optionally with edited arguments), cancel,
or redirect with a note. The database moves the approval, the held call, the
task and the run together (`public.decide_approval`), and the scheduler wakes
the run, which replays the call by its idempotency key:

- approved: the tool runs once, with the approved arguments;
- redirected: the agent reads the owner's note instead of a result;
- cancelled: the task and run are cancelled and never wake.

A decision with a note, and any standing rule, is also written to the brain
as a fact from the owner (right-hand idea 4), through the write gate, so
later proposals can be checked against it.
"""

from typing import Any, Literal
from uuid import UUID

import psycopg

from app.brain.write_gate import BrainWriter, FactCandidate, WriteResult
from app.db import acting_as
from app.gateway import GatewayError

Decision = Literal["approve", "cancel", "redirect"]


class ApprovalPending(GatewayError):
    """Raised out of a tool call that was held: the run pauses there."""

    code = "awaiting_approval"

    def __init__(self, approval_id: UUID | str, tool: str) -> None:
        super().__init__(f"{tool} is held for the owner's approval ({approval_id})")
        self.approval_id = str(approval_id)


class ApprovalError(RuntimeError):
    status = 400


class ApprovalNotFound(ApprovalError):
    status = 404


def list_pending(
    connection: psycopg.Connection, *, user_id: UUID | str, limit: int = 50
) -> list[dict[str, Any]]:
    """Pending approvals with their decision card, newest first."""
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select ap.id, ap.action_type, ap.action_key, ap.payload, ap.created_at,
                   ap.agent_id, a.name as agent, ap.task_id, t.title as task_title,
                   ap.recommendation, ap.recommendation_probs, ap.explanation,
                   ap.facts_checked, ap.similar_decisions, ap.conflicts
            from public.approvals ap
            left join public.agents a on a.id = ap.agent_id
            left join public.tasks t on t.id = ap.task_id
            where ap.status = 'pending'
            order by ap.created_at desc
            limit %s
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


def decide(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    approval_id: UUID | str,
    decision: Decision,
    note: str | None = None,
    edited_arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The owner's decision. Deciding twice changes nothing."""
    import json

    if decision == "redirect" and not (note or "").strip():
        raise ApprovalError("A redirect needs a note telling the agent what to do instead")
    if edited_arguments is not None and decision != "approve":
        raise ApprovalError("Only an approval can carry edited arguments")
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        try:
            cursor.execute(
                "select * from public.decide_approval(%s, %s, %s, %s)",
                (
                    str(approval_id),
                    decision,
                    note,
                    json.dumps(edited_arguments) if edited_arguments is not None else None,
                ),
            )
        except psycopg.errors.NoDataFound as error:
            raise ApprovalNotFound(f"No approval {approval_id}") from error
        return dict(cursor.fetchone())


def remember(
    writer: BrainWriter,
    *,
    org_id: UUID | str,
    agent_id: UUID | str,
    statement: str,
    ref: str | None = None,
) -> WriteResult:
    """Write an owner decision or standing rule to the brain (idea 4).

    It goes through the write gate like any fact, with the owner as its
    source and the owner's own words as its evidence. `agent_id` pays for
    the judgment: the agent whose action prompted it.
    """
    text = " ".join(statement.split())
    return writer.propose(
        FactCandidate(claim=text, source="owner", source_text=text, source_ref=ref),
        org_id=org_id,
        agent_id=agent_id,
        idempotency_key=f"owner:{ref}" if ref else None,
    )


def decision_statement(approval: dict[str, Any], decision: Decision, note: str | None) -> str:
    """How an owner decision reads as a fact."""
    tool = (approval.get("payload") or {}).get("tool") or approval.get("action_key") or "action"
    verb = {"approve": "approved", "cancel": "rejected", "redirect": "rejected"}[decision]
    sentence = f"The owner {verb} a request to use {tool}"
    return f"{sentence}: {note.strip()}" if note and note.strip() else f"{sentence}."
