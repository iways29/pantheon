import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from tests.conftest import AUDIENCE, OTHER_ID, OWNER_ID, SECRET


def make_token(
    subject: str = OWNER_ID,
    *,
    secret: str = SECRET,
    audience: str = AUDIENCE,
    expires_in: int = 3600,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": subject,
            "aud": audience,
            "role": "authenticated",
            "email": "owner@example.com",
            "iat": now,
            "exp": now + expires_in,
        },
        secret,
        algorithm="HS256",
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_public_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}


def test_authed_health_returns_200_for_the_owner(client: TestClient) -> None:
    response = client.get("/health/authed", headers=auth(make_token()))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["user_id"] == OWNER_ID
    assert body["email"] == "owner@example.com"


def test_authed_health_rejects_a_missing_token(client: TestClient) -> None:
    assert client.get("/health/authed").status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        make_token(secret="wrong-secret-also-long-enough-abcdefghij"),
        make_token(audience="some-other-audience"),
        make_token(expires_in=-60),
    ],
    ids=["malformed", "wrong-signature", "wrong-audience", "expired"],
)
def test_authed_health_rejects_bad_tokens(client: TestClient, token: str) -> None:
    assert client.get("/health/authed", headers=auth(token)).status_code == 401


def test_authed_health_rejects_a_valid_token_for_another_user(client: TestClient) -> None:
    """Phase 1 has one operator: a valid signup is still not the owner."""
    response = client.get("/health/authed", headers=auth(make_token(OTHER_ID)))

    assert response.status_code == 403


def test_authed_health_fails_loudly_when_verification_is_unconfigured() -> None:
    """With no secret and no JWKS source, the API must not accept tokens."""
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="test", owner_user_id=OWNER_ID
    )
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/health/authed", headers=auth(make_token())
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
