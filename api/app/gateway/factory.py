"""Building a gateway from configuration.

Kept out of app.config so configuration stays a plain description of the
environment with no knowledge of the gateway, and out of Gateway itself so the
class can be constructed with a fake transport in tests without touching env.
"""

import psycopg

from app.config import Settings
from app.gateway.gateway import Gateway
from app.gateway.tiers import TIERS, TierMap
from app.gateway.transport import OpenRouterTransport


def tier_map_from(settings: Settings) -> TierMap:
    return TierMap.from_json(settings.model_tiers)


def transport_from(settings: Settings) -> OpenRouterTransport:
    if not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is not set; see .env.example")
    return OpenRouterTransport(settings.openrouter_api_key)


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

    return Gateway(connection, transport_from(settings), tiers)
