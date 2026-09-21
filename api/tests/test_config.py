"""How the deployment labels itself.

Vercel injects VERCEL_ENV per deployment, so production and preview label
themselves without anyone maintaining an environment variable per target --
which is what previously left `/health` reporting a blank environment.
"""

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("VERCEL_ENV", raising=False)


def test_falls_back_to_development_off_vercel() -> None:
    assert Settings().environment == "development"


@pytest.mark.parametrize("vercel_env", ["production", "preview", "development"])
def test_uses_vercel_env_when_present(monkeypatch: pytest.MonkeyPatch, vercel_env: str) -> None:
    monkeypatch.setenv("VERCEL_ENV", vercel_env)

    assert Settings().environment == vercel_env


def test_explicit_environment_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "staging")

    assert Settings().environment == "staging"


def test_blank_environment_does_not_win(monkeypatch: pytest.MonkeyPatch) -> None:
    """A variable set to empty is how `/health` reported a blank environment."""
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "")

    assert Settings().environment == "production"


def test_model_tiers_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No guessed catalogue ships: an unconfigured gateway refuses loudly."""
    monkeypatch.delenv("MODEL_TIERS", raising=False)

    assert Settings().model_tiers == ""


def test_model_tiers_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_TIERS", '{"cheap":"vendor/small"}')

    assert Settings().model_tiers == '{"cheap":"vendor/small"}'
