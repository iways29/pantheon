"""The asymmetric verification path, exercised without network access.

Supabase projects can sign access tokens with an asymmetric key published at a
JWKS endpoint instead of a shared HS256 secret. These tests stand in a local
RSA key for that endpoint so the branch is covered by CI.
"""

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.config import Settings, get_settings
from app.main import app
from tests.conftest import AUDIENCE, OTHER_ID, OWNER_ID

JWKS_URL = "https://project.example.supabase.co/auth/v1/.well-known/jwks.json"


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_client(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> TestClient:
    class FakeSigningKey:
        key = rsa_key.public_key()

    class FakeJWKClient:
        def __init__(self, _url: str) -> None: ...

        def get_signing_key_from_jwt(self, _token: str) -> Any:
            return FakeSigningKey()

    monkeypatch.setattr(auth_module, "_jwk_client", FakeJWKClient)

    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="test",
        supabase_jwks_url=JWKS_URL,
        supabase_jwt_audience=AUDIENCE,
        owner_user_id=OWNER_ID,
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def rs256_token(rsa_key: rsa.RSAPrivateKey, subject: str = OWNER_ID) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": subject,
            "aud": AUDIENCE,
            "role": "authenticated",
            "email": "owner@example.com",
            "iat": now,
            "exp": now + 3600,
        },
        rsa_key,
        algorithm="RS256",
    )


def test_jwks_url_is_derived_from_the_supabase_url() -> None:
    settings = Settings(supabase_url="https://project.example.supabase.co/")

    assert settings.jwks_url == JWKS_URL


def test_rs256_token_is_accepted(jwks_client: TestClient, rsa_key: rsa.RSAPrivateKey) -> None:
    token = rs256_token(rsa_key)

    response = jwks_client.get("/health/authed", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["user_id"] == OWNER_ID


def test_rs256_token_for_another_user_is_rejected(
    jwks_client: TestClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    token = rs256_token(rsa_key, OTHER_ID)

    response = jwks_client.get("/health/authed", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
