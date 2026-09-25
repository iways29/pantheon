"""Building a Library for real use: gateway embeddings, screening, Storage."""

from typing import Any
from uuid import UUID

import psycopg

from app.brain import Brain, GatewayEmbedder
from app.brain.write_gate import BrainWriter
from app.config import Settings
from app.gateway import gateway_from
from app.judge import Judge
from app.judge.screening import Screener
from app.knowledge.library import Library
from app.knowledge.links import Links
from app.knowledge.storage import SupabaseStorage


def library_from(
    connection: psycopg.Connection, settings: Settings, *, processor_agent_id: UUID | str
) -> Library:
    """Costs (screening, embeddings) go to `processor_agent_id`'s department."""
    if not settings.typesafe_api_key:
        raise ValueError("TYPESAFE_API_KEY is not set: documents cannot be screened")
    gateway = gateway_from(connection, settings)
    embedder = GatewayEmbedder(gateway, agent_id=processor_agent_id)
    screener = Screener(connection, Judge(connection, gateway), Brain(connection, embedder))
    files = SupabaseStorage(settings.supabase_url or "", settings.supabase_service_role_key or "")
    return Library(connection, embedder=embedder, screener=screener, files=files)


def links_from(
    connection: psycopg.Connection, settings: Settings, *, agent_id: UUID | str
) -> Links:
    """The agent reads the page, proposes its claims, and pays for both."""
    if not settings.typesafe_api_key:
        raise ValueError("TYPESAFE_API_KEY is not set: pages cannot be screened")
    gateway = gateway_from(connection, settings)
    judge = Judge(connection, gateway)
    brain = Brain(connection, GatewayEmbedder(gateway, agent_id=agent_id))
    return Links(
        connection,
        gateway=gateway,
        screener=Screener(connection, judge, brain),
        writer=BrainWriter(connection, brain, judge),
    )


def writer_from(
    connection: psycopg.Connection, settings: Settings, *, agent_id: UUID | str
) -> BrainWriter:
    """The brain's write gate for the owner's own statements; `agent_id` pays."""
    if not settings.typesafe_api_key:
        raise ValueError("TYPESAFE_API_KEY is not set: nothing can be written to the brain")
    gateway = gateway_from(connection, settings)
    brain = Brain(connection, GatewayEmbedder(gateway, agent_id=agent_id))
    return BrainWriter(connection, brain, Judge(connection, gateway))


def agent_services() -> dict[str, Any]:
    """Services an agent run's tools need beyond its session (ADR 020): the
    safe fetcher, and link previews built on the run's own session."""
    from app.knowledge.fetch import fetch

    def links(session: Any) -> Links | None:  # noqa: ANN401
        if session.writer is None:  # no TypeSafe: pages cannot be screened
            return None
        judge = Judge(session.connection, session.gateway)
        return Links(
            session.connection,
            gateway=session.gateway,
            screener=Screener(session.connection, judge, session.brain),
            writer=session.writer,
        )

    return {"fetcher": fetch, "links": links}
