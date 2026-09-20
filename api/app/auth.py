"""Supabase Auth token verification.

Tokens are verified locally rather than by calling Supabase on every request:
a network round trip per request costs both latency and money, and phase 1 is
judged against a $50-200/month budget.

Supabase projects sign access tokens either with a per-project HS256 secret or
with an asymmetric key published at the project's JWKS endpoint. Both are
supported here and selected by configuration, so switching signing keys is an
environment change rather than a code change.
"""

from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from pydantic import BaseModel

from app.config import Settings, get_settings

_ASYMMETRIC_ALGORITHMS = ["RS256", "ES256"]

_bearer = HTTPBearer(auto_error=False)


class Principal(BaseModel):
    """The authenticated caller, as asserted by a verified Supabase token."""

    user_id: str
    email: str | None = None
    role: str | None = None


class AuthConfigurationError(RuntimeError):
    """Raised when the API cannot verify tokens because it is misconfigured."""


@lru_cache
def _jwk_client(jwks_url: str) -> PyJWKClient:
    # Cached so signing keys are fetched once per warm instance, not per request.
    return PyJWKClient(jwks_url)


def _decode(token: str, settings: Settings) -> dict[str, Any]:
    if settings.supabase_jwt_secret:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience=settings.supabase_jwt_audience,
        )

    jwks_url = settings.jwks_url
    if not jwks_url:
        raise AuthConfigurationError(
            "Set SUPABASE_JWT_SECRET, or SUPABASE_URL/SUPABASE_JWKS_URL for asymmetric keys."
        )

    signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=_ASYMMETRIC_ALGORITHMS,
        audience=settings.supabase_jwt_audience,
    )


def require_owner(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    """Authenticate the request and confirm the caller is the single owner.

    Phase 1 has exactly one operator, so a valid token is necessary but not
    sufficient: the subject must match the configured owner.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = _decode(credentials.credentials, settings)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has no subject",
        )

    if settings.owner_user_id and user_id != settings.owner_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not the owner of this deployment",
        )

    return Principal(
        user_id=user_id,
        email=claims.get("email"),
        role=claims.get("role"),
    )


OwnerPrincipal = Annotated[Principal, Depends(require_owner)]
