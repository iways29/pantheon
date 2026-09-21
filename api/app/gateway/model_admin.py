"""Changing which model a tier resolves to, without a redeploy.

ADR 003. The mapping lives in `model_tier_assignments`; the gateway reads it
on every call. This module is the checked way to write it: a slug is confirmed
against OpenRouter's live catalogue before it is saved, so a typo fails here
rather than at the first model call of some later run.

Auditing is not done here. A trigger on the table writes the `events` row, so
the trail is complete however the change was made.
"""

from typing import Protocol
from uuid import UUID

import httpx
import psycopg

from app.gateway.errors import GatewayError
from app.gateway.tiers import TIERS
from app.gateway.transport import OPENROUTER_BASE_URL


class UnknownModel(GatewayError):
    code = "unknown_model"

    def __init__(self, model: str) -> None:
        super().__init__(f"{model!r} is not in OpenRouter's model catalogue")
        self.model = model


class ModelCatalogue(Protocol):
    def model_ids(self) -> frozenset[str]: ...


class OpenRouterCatalogue:
    """OpenRouter's public model list. Needs no key; fetched once per instance."""

    def __init__(
        self,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/models"
        self._client = client or httpx.Client(timeout=30.0)
        self._ids: frozenset[str] | None = None

    def model_ids(self) -> frozenset[str]:
        if self._ids is None:
            response = self._client.get(self._url)
            response.raise_for_status()
            self._ids = frozenset(m["id"] for m in response.json()["data"])
        return self._ids


def assign_model(
    connection: psycopg.Connection,
    *,
    org_id: UUID | str,
    tier: str,
    model: str,
    catalogue: ModelCatalogue,
    department_id: UUID | str | None = None,
) -> None:
    """Point a tier at a model, org-wide or for one department.

    Runs on the caller's connection, so RLS decides which orgs it may touch.
    Aliases are refused by a check constraint as well as by the catalogue,
    since `~` slugs are not listed there.
    """
    if tier not in TIERS:
        raise ValueError(f"Unknown tier {tier!r}. Known tiers: {list(TIERS)}")
    model = model.strip()
    if model not in catalogue.model_ids():
        raise UnknownModel(model)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into public.model_tier_assignments (org_id, department_id, tier, model)
            values (%s, %s, %s, %s)
            on conflict on constraint model_tier_assignments_scope_key
            do update set model = excluded.model
            """,
            (str(org_id), str(department_id) if department_id else None, tier, model),
        )


def clear_assignment(
    connection: psycopg.Connection,
    *,
    org_id: UUID | str,
    tier: str,
    department_id: UUID | str | None = None,
) -> None:
    """Remove a mapping. A department falls back to the org row, the org to MODEL_TIERS."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            delete from public.model_tier_assignments
            where org_id = %s and tier = %s
              and department_id is not distinct from %s
            """,
            (str(org_id), tier, str(department_id) if department_id else None),
        )
