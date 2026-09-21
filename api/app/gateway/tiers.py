"""Tier to model mapping, in one place.

Agents choose a tier, never a model. Retiering an agent, or repointing a whole
tier at a different model, is a configuration change: no agent code moves.

Deliberately no built-in defaults. OpenRouter's model slugs are its own and
change as models come and go, so shipping a guessed catalogue would be a
config file that lies. An unconfigured tier refuses loudly instead.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from app.gateway.errors import TierNotConfigured

#: Ordered cheapest first. The order is meaningful for cost reporting later,
#: which compares what a task cost against what a cheaper tier would have.
TIERS: tuple[str, ...] = ("cheap", "standard", "frontier")


@dataclass(frozen=True)
class TierMap:
    models: Mapping[str, str]

    def __post_init__(self) -> None:
        unknown = set(self.models) - set(TIERS)
        if unknown:
            raise ValueError(f"Unknown tiers: {sorted(unknown)}. Known tiers: {list(TIERS)}")

    def model_for(self, tier: str) -> str:
        model = self.models.get(tier)
        if not model:
            raise TierNotConfigured(tier)
        return model

    @classmethod
    def from_json(cls, raw: str | None) -> "TierMap":
        """Parse the MODEL_TIERS environment variable.

        Shape: {"cheap": "<slug>", "standard": "<slug>", "frontier": "<slug>"}
        Slugs come from OpenRouter's model list; see .env.example.
        """
        if not raw or not raw.strip():
            return cls(models={})

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"MODEL_TIERS is not valid JSON: {error}") from error

        if not isinstance(parsed, dict):
            raise ValueError("MODEL_TIERS must be a JSON object of tier to model slug")

        return cls(models={str(k): str(v) for k, v in parsed.items()})
