"""Endpoints for the owner (Step 6, ADR 014).

Every route requires the owner's Supabase token (`OwnerPrincipal`) and acts
as the owner in the database, so RLS applies as it would to any member.
Phase 1 has one org; the owner's only membership picks it.
"""

from collections.abc import Callable, Iterator
from dataclasses import asdict
from typing import Annotated, Any
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
from app.config import Settings, get_settings
from app.db import acting_as, as_service_role, connect
from app.knowledge.extract import UnsupportedContent
from app.knowledge.fetch import FetchedPage, FetchRefused, fetch
from app.knowledge.library import DocumentError, Library
from app.knowledge.links import LinkError, Links, Preview
from app.knowledge.wiring import library_from, links_from

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
