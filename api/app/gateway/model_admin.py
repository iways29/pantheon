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
from app.gateway.tiers import EMBEDDING_TIER, TIERS
from app.gateway.transport import OPENROUTER_BASE_URL


class UnknownModel(GatewayError):
    code = "unknown_model"

    def __init__(self, model: str) -> None:
        super().__init__(f"{model!r} is not in OpenRouter's model catalogue")
        self.model = model


class ModelCatalogue(Protocol):
    def model_ids(self) -> frozenset[str]: ...


class ToolCatalogue(Protocol):
    def supports_tools(self, model: str) -> bool: ...


class OpenRouterCatalogue:
    """OpenRouter's public model list. Needs no key; fetched once per instance.

    Embedding models are listed separately (`/embeddings/models`), so the
    embedding tier is checked against that list instead.
    """

    def __init__(
        self,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        client: httpx.Client | None = None,
        embeddings: bool = False,
    ) -> None:
        path = "embeddings/models" if embeddings else "models"
        self._url = f"{base_url.rstrip('/')}/{path}"
        self._client = client or httpx.Client(timeout=30.0)
        self._ids: frozenset[str] | None = None
        self._params: dict[str, frozenset[str]] = {}

    def model_ids(self) -> frozenset[str]:
        if self._ids is None:
            response = self._client.get(self._url)
            response.raise_for_status()
            data = response.json()["data"]
            self._ids = frozenset(m["id"] for m in data)
            self._params = {m["id"]: frozenset(m.get("supported_parameters") or []) for m in data}
        return self._ids

    def supports_tools(self, model: str) -> bool:
        """Whether the catalogue lists `tools` among the model's parameters."""
        self.model_ids()
        return "tools" in self._params.get(model, frozenset())


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
    if tier not in (*TIERS, EMBEDDING_TIER):
        raise ValueError(f"Unknown tier {tier!r}. Known tiers: {[*TIERS, EMBEDDING_TIER]}")
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
