"""Pantheon API.

A single FastAPI application, deployed to Vercel with the `fastapi` framework
preset. Runs are short and resumable by design: nothing here may assume a
long-lived process.
"""

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.auth import OwnerPrincipal
from app.config import Settings, get_settings
from app.internal import router as internal_router

settings = get_settings()

app = FastAPI(
    title="Pantheon API",
    version="0.1.0",
    root_path=settings.api_root_path,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


app.include_router(internal_router)


class Health(BaseModel):
    status: str
    environment: str


class AuthedHealth(Health):
    user_id: str
    email: str | None = None


@app.get("/health", response_model=Health, tags=["health"])
def health(settings: Annotated[Settings, Depends(get_settings)]) -> Health:
    """Unauthenticated liveness check, for uptime monitoring only."""
    return Health(status="ok", environment=settings.environment)


@app.get("/health/authed", response_model=AuthedHealth, tags=["health"])
def authed_health(
    principal: OwnerPrincipal,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthedHealth:
    """Health check behind owner authentication.

    This is the Step 0 acceptance route: it returns 200 only for a request
    carrying a valid Supabase token for the configured owner.
    """
    return AuthedHealth(
        status="ok",
        environment=settings.environment,
        user_id=principal.user_id,
        email=principal.email,
    )
