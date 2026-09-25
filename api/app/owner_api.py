"""Endpoints for the owner (Step 6, ADR 014).

Every route requires the owner's Supabase token (`OwnerPrincipal`) and acts
as the owner in the database, so RLS applies as it would to any member.
Phase 1 has one org; the owner's only membership picks it.
"""

from collections.abc import Callable, Iterator
from dataclasses import asdict
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.agents.admin import (
    AgentAdminError,
    AgentSpec,
    AgentSummary,
    create_agent,
    list_agents,
    set_enabled,
)
from app.auth import OwnerPrincipal
from app.brain.write_gate import BrainWriter
from app.config import Settings, get_settings
from app.db import acting_as, as_service_role, connect
from app.knowledge.extract import UnsupportedContent
from app.knowledge.fetch import FetchedPage, FetchRefused, fetch
from app.knowledge.library import DocumentError, Library
from app.knowledge.links import LinkError, Links, Preview
from app.knowledge.wiring import library_from, links_from, writer_from

router = APIRouter(tags=["owner"])


def get_connection(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Iterator[psycopg.Connection]:
    """One database connection per request, closed afterwards."""
    if not settings.database_url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DATABASE_URL is not configured")
    with connect(settings.database_url) as connection:
        yield connection


Connection = Annotated[psycopg.Connection, Depends(get_connection)]


def owner_org(connection: psycopg.Connection, user_id: str) -> str:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select org_id from public.org_members where user_id = %s order by created_at limit 2",
            (user_id,),
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Expected the owner to belong to exactly one org, found {len(rows)}",
        )
    return str(rows[0]["org_id"])


def _refuse(error: AgentAdminError) -> HTTPException:
    return HTTPException(error.status, str(error))


def _json(summary: AgentSummary) -> dict[str, Any]:
    data = asdict(summary)
    data["id"] = str(data["id"])
    data["allowed_tools"] = list(data["allowed_tools"])
    if data["daily_budget_usd"] is not None:
        data["daily_budget_usd"] = str(data["daily_budget_usd"])
    return data


@router.post("/agents", status_code=status.HTTP_201_CREATED)
def post_agent(
    spec: AgentSpec, principal: OwnerPrincipal, connection: Connection, response: Response
) -> dict[str, Any]:
    """Create an agent, switched off. Repeating the same request is safe."""
    org_id = owner_org(connection, principal.user_id)
    try:
        summary = create_agent(connection, user_id=principal.user_id, org_id=org_id, spec=spec)
    except AgentAdminError as error:
        raise _refuse(error) from error
    if not summary.created:
        response.status_code = status.HTTP_200_OK
    return _json(summary)


@router.get("/agents")
def get_agents(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    org_id = owner_org(connection, principal.user_id)
    return [_json(s) for s in list_agents(connection, user_id=principal.user_id, org_id=org_id)]


@router.post("/agents/{name}/enable")
def enable_agent(name: str, principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    return _switch(name, True, principal.user_id, connection)


@router.post("/agents/{name}/disable")
def disable_agent(name: str, principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    return _switch(name, False, principal.user_id, connection)


def _switch(name: str, on: bool, user_id: str, connection: psycopg.Connection) -> dict[str, Any]:
    org_id = owner_org(connection, user_id)
    try:
        summary = set_enabled(connection, user_id=user_id, org_id=org_id, name=name, enabled=on)
    except AgentAdminError as error:
        raise _refuse(error) from error
    return _json(summary)


MakeLibrary = Callable[[psycopg.Connection, UUID], Library]
MakeLinks = Callable[[psycopg.Connection, UUID], Links]
Fetcher = Callable[[str], FetchedPage]


def get_library_factory(settings: Annotated[Settings, Depends(get_settings)]) -> MakeLibrary:
    """Overridden in tests with a scripted screener and an in-memory store."""
    return lambda connection, agent_id: library_from(
        connection, settings, processor_agent_id=agent_id
    )


LibraryFactory = Annotated[MakeLibrary, Depends(get_library_factory)]


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def post_document(
    request: Request,
    principal: OwnerPrincipal,
    connection: Connection,
    make_library: LibraryFactory,
    response: Response,
    title: Annotated[str, Query(min_length=1)],
    filename: Annotated[str, Query(min_length=1)],
    scope: Annotated[str, Query(pattern="^(company|department|agent)$")],
    processed_by: Annotated[str, Query(description="Agent whose budget pays for screening")],
    department: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Upload a document as the raw request body, typed by Content-Type.

    Screened before it is chunked (ADR 015). Uploading the same file to the
    same scope again returns the existing document (200).
    """
    org_id = owner_org(connection, principal.user_id)
    content = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select id from public.agents where org_id = %s and name = %s",
            (org_id, processed_by),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent {processed_by!r}")
        library = make_library(conn, row["id"])
        try:
            result = library.add(
                org_id=org_id,
                content=content,
                filename=filename,
                content_type=content_type,
                title=title,
                scope=scope,  # type: ignore[arg-type]
                department=department,
                agent=agent,
                processor_agent_id=row["id"],
            )
        except DocumentError as error:
            raise HTTPException(error.status, str(error)) from error
        except UnsupportedContent as error:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(error)) from error
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return {
        "id": str(result.id),
        "status": result.status,
        "chunks": result.chunks,
        "created": result.created,
        "reasons": list(result.reasons),
        "approval_id": str(result.approval_id) if result.approval_id else None,
    }


def get_links_factory(settings: Annotated[Settings, Depends(get_settings)]) -> MakeLinks:
    return lambda connection, agent_id: links_from(connection, settings, agent_id=agent_id)


def get_fetcher() -> Fetcher:
    return fetch


class PreviewRequest(BaseModel):
    url: str
    #: The agent that reads the page and proposes its claims (and pays).
    agent: str


def _preview_json(preview: Preview) -> dict[str, Any]:
    return {
        "id": str(preview.id),
        "url": preview.url,
        "final_url": preview.final_url,
        "label": preview.label,
        "reasons": list(preview.reasons),
        "claims": list(preview.claims),
        "status": preview.status,
        "results": list(preview.results),
        "created": preview.created,
    }


@router.post("/links/preview", status_code=status.HTTP_201_CREATED)
def preview_link(
    body: PreviewRequest,
    principal: OwnerPrincipal,
    connection: Connection,
    make_links: Annotated[MakeLinks, Depends(get_links_factory)],
    fetcher: Annotated[Fetcher, Depends(get_fetcher)],
    response: Response,
) -> dict[str, Any]:
    """Fetch, screen and propose claims. Writes nothing to the brain."""
    org_id = owner_org(connection, principal.user_id)
    try:
        page = fetcher(body.url)
    except FetchRefused as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    with acting_as(connection, user_id=principal.user_id) as conn:
        agent_id = _agent_id(conn, org_id, body.agent)
        links = make_links(conn, agent_id)
        try:
            preview = links.preview(page, org_id=org_id, agent_id=agent_id)
        except LinkError as error:
            raise HTTPException(error.status, str(error)) from error
    if not preview.created:
        response.status_code = status.HTTP_200_OK
    return _preview_json(preview)


@router.post("/links/{preview_id}/push")
def push_link(
    preview_id: str,
    principal: OwnerPrincipal,
    connection: Connection,
    make_links: Annotated[MakeLinks, Depends(get_links_factory)],
) -> dict[str, Any]:
    """Send a clean preview's claims through the brain write gate."""
    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select agent_id from public.link_previews where id = %s and org_id = %s",
            (preview_id, org_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No preview {preview_id}")
        links = make_links(conn, row["agent_id"])
        try:
            return _preview_json(links.push(preview_id, org_id=org_id))
        except LinkError as error:
            raise HTTPException(error.status, str(error)) from error


def _agent_id(connection: psycopg.Connection, org_id: str, name: str) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            "select id from public.agents where org_id = %s and name = %s", (org_id, name)
        )
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent {name!r}")
    return row["id"]


# --- Orders and task trees (Step 7.3, ADR 019) -------------------------------


class OrderRequest(BaseModel):
    agent: str
    title: str
    instructions: str = ""
    input: dict[str, Any] = {}
    max_cost_usd: float | None = None
    #: Optional; by default the same order twice is one task.
    idempotency_key: str | None = None


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def post_order(
    body: OrderRequest, principal: OwnerPrincipal, connection: Connection, response: Response
) -> dict[str, Any]:
    """Give an agent an order. The scheduler starts it on its next tick."""
    from decimal import Decimal

    from app.tasks import TaskError, order

    org_id = owner_org(connection, principal.user_id)
    try:
        task = order(
            connection,
            user_id=principal.user_id,
            org_id=org_id,
            agent=body.agent,
            title=body.title,
            instructions=body.instructions,
            input=body.input,
            max_cost_usd=None if body.max_cost_usd is None else Decimal(str(body.max_cost_usd)),
            idempotency_key=body.idempotency_key,
        )
    except TaskError as error:
        raise HTTPException(error.status, str(error)) from error
    if not task.created:
        response.status_code = status.HTTP_200_OK
    return {"id": str(task.id), "status": task.status, "created": task.created}


@router.get("/tasks/{task_id}")
def get_task_tree(
    task_id: UUID, principal: OwnerPrincipal, connection: Connection
) -> list[dict[str, Any]]:
    """The task, everything under it, and what each level cost."""
    from app.tasks import tree

    rows = tree(connection, user_id=principal.user_id, root_task_id=task_id)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No task {task_id}")
    return [
        {
            **row,
            "id": str(row["id"]),
            "parent_task_id": str(row["parent_task_id"]) if row["parent_task_id"] else None,
            "own_cost_usd": str(row["own_cost_usd"]),
            "tree_cost_usd": str(row["tree_cost_usd"]),
        }
        for row in rows
    ]


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: UUID, principal: OwnerPrincipal, connection: Connection) -> dict[str, Any]:
    from app.tasks import cancel

    return {"cancelled": cancel(connection, user_id=principal.user_id, task_id=task_id)}


# --- Approvals and the decision desk (Step 7.5, ADR 021) ---------------------

MakeWriter = Callable[[psycopg.Connection, UUID], BrainWriter]


def get_writer_factory(settings: Annotated[Settings, Depends(get_settings)]) -> MakeWriter:
    """Overridden in tests with a scripted TypeSafe."""
    return lambda connection, agent_id: writer_from(connection, settings, agent_id=agent_id)


WriterFactory = Annotated[MakeWriter, Depends(get_writer_factory)]


def _approval_json(row: dict[str, Any]) -> dict[str, Any]:
    return {k: str(v) if isinstance(v, UUID) else v for k, v in row.items()}


@router.get("/approvals")
def get_approvals(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    """What is waiting for the owner, each with its decision card."""
    from app.approvals import list_pending

    owner_org(connection, principal.user_id)
    return [
        _approval_json(row) | {"created_at": row["created_at"].isoformat()}
        for row in list_pending(connection, user_id=principal.user_id)
    ]


class ApproveRequest(BaseModel):
    #: Replaces the held call's arguments, when the owner edited them.
    edited_arguments: dict[str, Any] | None = None
    note: str | None = None


class RejectRequest(BaseModel):
    #: cancel: the task stops. redirect: the agent carries on, told `note`.
    mode: Literal["cancel", "redirect"] = "cancel"
    note: str | None = None


@router.post("/approvals/{approval_id}/approve")
def approve(
    approval_id: UUID,
    body: ApproveRequest,
    principal: OwnerPrincipal,
    connection: Connection,
    make_writer: WriterFactory,
) -> dict[str, Any]:
    return _decide(
        connection,
        principal.user_id,
        approval_id,
        "approve",
        body.note,
        body.edited_arguments,
        make_writer,
    )


@router.post("/approvals/{approval_id}/reject")
def reject(
    approval_id: UUID,
    body: RejectRequest,
    principal: OwnerPrincipal,
    connection: Connection,
    make_writer: WriterFactory,
) -> dict[str, Any]:
    return _decide(
        connection, principal.user_id, approval_id, body.mode, body.note, None, make_writer
    )


def _decide(
    connection: psycopg.Connection,
    user_id: str,
    approval_id: UUID,
    decision: Any,  # noqa: ANN401 - one of approvals.Decision
    note: str | None,
    edited: dict[str, Any] | None,
    make_writer: MakeWriter,
) -> dict[str, Any]:
    from app.approvals import ApprovalError, decide, decision_statement, remember

    org_id = owner_org(connection, user_id)
    try:
        row = decide(
            connection,
            user_id=user_id,
            approval_id=approval_id,
            decision=decision,
            note=note,
            edited_arguments=edited,
        )
    except ApprovalError as error:
        raise HTTPException(error.status, str(error)) from error
    remembered = None
    # A decision with a reason becomes an owner fact (right-hand idea 4), so
    # later proposals are checked against it.
    if note and note.strip() and row.get("agent_id"):
        with acting_as(connection, user_id=user_id) as conn:
            result = remember(
                make_writer(conn, row["agent_id"]),
                org_id=org_id,
                agent_id=row["agent_id"],
                statement=decision_statement(row, decision, note),
                ref=f"approval:{approval_id}",
            )
        remembered = result.outcome
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "recommendation": row["recommendation"],
        "remembered": remembered,
    }


class PolicyRequest(BaseModel):
    #: A standing rule in plain English, e.g. "Never email a customer on a Sunday."
    statement: str
    #: The agent whose budget pays for checking it.
    agent: str


@router.post("/policies", status_code=status.HTTP_201_CREATED)
def post_policy(
    body: PolicyRequest,
    principal: OwnerPrincipal,
    connection: Connection,
    make_writer: WriterFactory,
) -> dict[str, Any]:
    """A standing rule, written to the brain as the owner's (right-hand idea 4)."""
    import hashlib

    from app.approvals import remember

    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn:
        agent_id = _agent_id(conn, org_id, body.agent)
        digest = hashlib.sha256(body.statement.strip().lower().encode()).hexdigest()[:24]
        result = remember(
            make_writer(conn, agent_id),
            org_id=org_id,
            agent_id=agent_id,
            statement=body.statement,
            ref=f"policy:{digest}",
        )
    return {
        "outcome": result.outcome,
        "fact_id": str(result.fact.id) if result.fact else None,
        "reasons": list(result.reasons),
    }


# --- Autonomy and safety limits (Step 7.6, ADR 022) ---------------------------


class LevelRequest(BaseModel):
    level: Literal["L0", "L1", "L2", "L3"]


@router.post("/agents/{name}/autonomy")
def put_autonomy(
    name: str, body: LevelRequest, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """Only the owner moves an agent up or down the ladder."""
    from app.agents.autonomy import set_level

    org_id = owner_org(connection, principal.user_id)
    try:
        level = set_level(
            connection, user_id=principal.user_id, org_id=org_id, name=name, level=body.level
        )
    except AgentAdminError as error:
        raise _refuse(error) from error
    return {"name": name, "autonomy_level": level}


@router.get("/autonomy/suggestions")
def get_suggestions(
    principal: OwnerPrincipal,
    connection: Connection,
    everything: Annotated[bool, Query(alias="all")] = False,
) -> list[dict[str, Any]]:
    """Promotions the approval history supports. Never applied automatically."""
    from app.agents.autonomy import suggestions

    owner_org(connection, principal.user_id)
    return [
        {**row, "agreement": None if row["agreement"] is None else float(row["agreement"])}
        for row in suggestions(connection, user_id=principal.user_id, eligible_only=not everything)
    ]


class ResumeRequest(BaseModel):
    reason: Literal["kill_switch", "budget_exceeded", "agent_disabled", "department_disabled"] = (
        "kill_switch"
    )


@router.post("/runs/resume")
def resume_runs(
    body: ResumeRequest, principal: OwnerPrincipal, connection: Connection
) -> dict[str, Any]:
    """Runs paused by the kill switch stay paused until the owner says go."""
    from app.agents.autonomy import resume_paused_runs

    org_id = owner_org(connection, principal.user_id)
    try:
        count = resume_paused_runs(
            connection, user_id=principal.user_id, org_id=org_id, reason=body.reason
        )
    except psycopg.errors.CheckViolation as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error).splitlines()[0]) from error
    return {"resumed": count}
