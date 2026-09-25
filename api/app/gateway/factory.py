"""Building a gateway from configuration.

Kept out of app.config so configuration stays a plain description of the
environment with no knowledge of the gateway, and out of Gateway itself so the
class can be constructed with a fake transport in tests without touching env.
"""

from functools import lru_cache

import psycopg

from app.config import Settings
from app.gateway.gateway import Gateway
from app.gateway.systemone import TypeSafeTransport
from app.gateway.tiers import TIERS, TierMap
from app.gateway.transport import OpenRouterTransport
from app.tracing import tracer_from


def tier_map_from(settings: Settings) -> TierMap:
    return TierMap.from_json(settings.model_tiers)


def transport_from(settings: Settings) -> OpenRouterTransport:
    if not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is not set; see .env.example")
    return OpenRouterTransport(settings.openrouter_api_key)


def systemone_transport_from(settings: Settings) -> TypeSafeTransport:
    """The TypeSafe transport, one per process.

    Cached so its circuit breaker remembers recent failures across requests
    served by the same warm function instance. Keyed on the key and URL, so a
    changed setting gets a fresh transport.
    """
    if not settings.typesafe_api_key:
        raise ValueError("TYPESAFE_API_KEY is not set; see .env.example")
    return _systemone_transport(settings.typesafe_api_key, settings.typesafe_base_url)


@lru_cache(maxsize=4)
def _systemone_transport(api_key: str, base_url: str) -> TypeSafeTransport:
    return TypeSafeTransport(api_key, base_url=base_url)


def gateway_from(connection: psycopg.Connection, settings: Settings) -> Gateway:
    """The real gateway, wired from the environment.

    Fails at construction when the provider key or the tier map is missing,
    rather than at the first model call. A misconfiguration should surface on
    startup, not halfway through an agent run.
    """
    tiers = tier_map_from(settings)
    missing = [tier for tier in TIERS if not tiers.models.get(tier)]
    if missing:
        raise ValueError(
            f"MODEL_TIERS has no model for {missing}. "
            "Fill every tier from OpenRouter's model list; see .env.example."
        )

    # Langfuse keeps one client per public key, so building it per gateway
    # reuses the same background exporter rather than starting another.
    tracer = tracer_from(settings)
    # TypeSafe is optional here: agents that never judge need no key. The
    # judge's own factory insists on it.
    systemone = systemone_transport_from(settings) if settings.typesafe_api_key else None
    return Gateway(connection, transport_from(settings), tiers, tracer, systemone)
