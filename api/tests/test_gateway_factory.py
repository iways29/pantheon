"""Wiring a real gateway from configuration.

A misconfiguration should surface when the gateway is built, not halfway
through an agent run, so these assert on construction rather than on a call.
"""

import pytest

from app.config import Settings
from app.gateway import TierNotConfigured, gateway_from, tier_map_from, transport_from

COMPLETE_TIERS = '{"cheap":"vendor/small","standard":"vendor/mid","frontier":"vendor/large"}'


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"openrouter_api_key": "test-key", "model_tiers": COMPLETE_TIERS}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_tiers_are_parsed_from_configuration() -> None:
    tiers = tier_map_from(settings())

    assert tiers.model_for("cheap") == "vendor/small"
    assert tiers.model_for("frontier") == "vendor/large"


def test_an_unset_tier_map_yields_no_models() -> None:
    tiers = tier_map_from(settings(model_tiers=""))

    with pytest.raises(TierNotConfigured):
        tiers.model_for("cheap")


def test_malformed_tier_json_is_rejected_with_the_variable_named() -> None:
    with pytest.raises(ValueError, match="MODEL_TIERS"):
        tier_map_from(settings(model_tiers="{not json"))


def test_an_unknown_tier_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown tiers"):
        tier_map_from(settings(model_tiers='{"gold":"vendor/x"}'))


def test_a_missing_api_key_names_the_variable() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        transport_from(settings(openrouter_api_key=None))


def test_building_a_gateway_needs_every_tier_filled() -> None:
    """A half-filled tier map is the likely mistake, so it fails at startup."""
    with pytest.raises(ValueError, match="MODEL_TIERS"):
        gateway_from(None, settings(model_tiers='{"cheap":"vendor/small"}'))  # type: ignore[arg-type]


def test_a_fully_configured_gateway_builds() -> None:
    gateway = gateway_from(None, settings())  # type: ignore[arg-type]

    assert gateway is not None
