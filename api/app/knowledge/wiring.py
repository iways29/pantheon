"""Building a Library for real use: gateway embeddings, screening, Storage."""

from uuid import UUID

import psycopg

from app.brain import Brain, GatewayEmbedder
from app.config import Settings
from app.gateway import gateway_from
from app.judge import Judge
from app.judge.screening import Screener
from app.knowledge.library import Library
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
