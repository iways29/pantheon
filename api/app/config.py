"""Runtime configuration, read from the environment.

Secrets are server-side only; nothing here is ever exposed to the browser.
"""

import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vercel sets VERCEL_ENV to production, preview or development per
    # deployment, so previews label themselves correctly without anyone
    # maintaining a per-environment variable. An explicit ENVIRONMENT still
    # wins, for local runs and any non-Vercel host.
    environment: str = ""

    @field_validator("environment", mode="after")
    @classmethod
    def _resolve_environment(cls, value: str) -> str:
        return value or os.getenv("VERCEL_ENV") or "development"

    # Supabase project, used to locate the JWKS endpoint and to verify tokens.
    supabase_url: str | None = None

    # Token verification. Exactly one of these paths is used at runtime:
    # a symmetric shared secret (HS256) when `supabase_jwt_secret` is set,
    # otherwise asymmetric verification against the project's JWKS endpoint.
    supabase_jwt_secret: str | None = None
    supabase_jwks_url: str | None = None
    supabase_jwt_audience: str = "authenticated"

    # Server-side Supabase key for Storage uploads (documents, ADR 015). The
    # legacy `service_role` JWT or a new `sb_secret_...` key. Never sent to
    # the browser.
    supabase_service_role_key: str | None = None

    # Phase 1 has exactly one operator. Any other authenticated subject is
    # rejected, so a stray Supabase signup cannot reach the API.
    owner_user_id: str | None = None

    # --- Model gateway ---------------------------------------------------
    # The only provider credential. Server-side only, and never prefixed
    # NEXT_PUBLIC_, which would compile it into the browser bundle.
    openrouter_api_key: str | None = None

    # Which model each tier resolves to, as JSON:
    #   {"cheap": "<slug>", "standard": "<slug>", "frontier": "<slug>"}
    # Slugs come from OpenRouter's model list. Deliberately no default: a
    # guessed catalogue would be configuration that lies, and an unset tier
    # refuses loudly rather than falling back to something expensive.
    model_tiers: str = ""

    # --- TypeSafe Jev, the judge (ADR 009) ----------------------------------
    # Server-side only. The model each gate uses is pinned in the database
    # (judge_gates.model), not here.
    typesafe_api_key: str | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"

    @field_validator("typesafe_base_url", mode="after")
    @classmethod
    def _default_typesafe_url(cls, value: str) -> str:
        # A blank TYPESAFE_BASE_URL= line in .env means "the default".
        return value.strip() or "https://api.typesafe.ai"

    # --- Tracing (ADR 004) -----------------------------------------------
    # Langfuse Cloud. Tracing is off unless both keys are set. Server-side
    # only, like every other credential here.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # The project's region, e.g. https://cloud.langfuse.com (EU) or
    # https://us.cloud.langfuse.com (US).
    langfuse_base_url: str | None = None

    # --- Scheduled runs (ADR 008) ------------------------------------------
    # The database the API's runs live in. On Vercel this is Supabase's
    # transaction pooler (ADR 005), the only endpoint reachable over IPv4.
    database_url: str | None = None

    # Shared secret the database's scheduler presents when it asks this API to
    # advance a run. The same value is stored in Supabase Vault as
    # `pantheon_trigger_secret`. Unset means the endpoint refuses every
    # request, so a missing secret can never leave it open.
    trigger_secret: str | None = None

    # Comma-separated browser origins allowed to call this API.
    cors_allow_origins: str = ""

    # This API's public address, e.g. https://api.example.com (with any root
    # path). MCP sign-ins return the owner's browser to
    # <PUBLIC_API_URL>/mcp/oauth/callback (ADR 025).
    public_api_url: str = ""

    # Set when the API is mounted under a path prefix (e.g. "/api" behind a
    # single-project rewrite). Empty when it serves its own domain root.
    api_root_path: str = ""

    @property
    def jwks_url(self) -> str | None:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return None

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
