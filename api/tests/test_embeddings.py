"""Real embeddings through the gateway (Step 6, ADR 013).

The model is data (tier `embedding` in model_tier_assignments), the call
passes the same checks as any model call, and the brain never compares
vectors from two different models.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest

from app.brain import Brain, GatewayEmbedder, HashingEmbedder
from app.db import acting_as, as_service_role
from app.gateway import (
    SENSITIVE_PROVIDER_PREFERENCES,
    EmbeddingResponse,
    Gateway,
    KillSwitchEngaged,
    OpenRouterTransport,
    TierNotConfigured,
    UpstreamError,
)
from tests.conftest_db import Tenants, admit
from tests.test_gateway import TIERS, RecordingTransport, make_agent, set_kill_switch

MODEL = "openai/text-embedding-3-small"


@dataclass
class EmbeddingFake(RecordingTransport):
    """A provider that also embeds, with the hashing model's vectors."""

    width: int = 1536
    embed_calls: list[dict[str, Any]] = field(default_factory=list)

    def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        dimensions: int | None = None,
        provider_preferences: dict[str, Any] | None = None,
    ) -> EmbeddingResponse:
        self.embed_calls.append(
            {
                "model": model,
                "inputs": inputs,
                "dimensions": dimensions,
                "provider_preferences": provider_preferences,
            }
        )
        hashing = HashingEmbedder(self.width)
        return EmbeddingResponse(
            model=f"{model}-served",
            vectors=[hashing.embed(text) for text in inputs],
            tokens_in=10 * len(inputs),
            cost_usd=0.0000002 * len(inputs),
            latency_ms=3,
        )


def assign(db: psycopg.Connection, org_id: UUID, model: str = MODEL, department=None) -> None:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into public.model_tier_assignments (org_id, department_id, tier, model) "
            "values (%s, %s, 'embedding', %s)",
            (str(org_id), str(department) if department else None, model),
        )


def rows(db: psycopg.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


# --- The transport -------------------------------------------------------------


def test_the_transport_sends_the_documented_request_and_orders_by_index() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": MODEL,
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                    {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 8, "total_tokens": 8, "cost": 0.00000016},
            },
        )

    transport = OpenRouterTransport(
        "sk-or-test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    response = transport.embed(model=MODEL, inputs=["a", "b"], dimensions=1536)

    body = json.loads(seen[0].content)
    assert str(seen[0].url).endswith("/api/v1/embeddings")
    assert body == {
        "model": MODEL,
        "input": ["a", "b"],
        "encoding_format": "float",
        "dimensions": 1536,
    }
    assert response.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert (response.tokens_in, response.cost_usd) == (8, 0.00000016)


def test_a_reply_with_the_wrong_number_of_vectors_is_malformed() -> None:
    reply = {"data": [{"index": 0, "embedding": [1.0]}], "usage": {}}
    transport = OpenRouterTransport(
        "k",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply))
        ),
    )
    with pytest.raises(UpstreamError) as caught:
        transport.embed(model=MODEL, inputs=["a", "b"])
    assert caught.value.reason == "malformed"


# --- The gateway ------------------------------------------------------------------


def embed(db: psycopg.Connection, tenants: Tenants, agent: UUID, fake: EmbeddingFake, **kw: Any):
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        return Gateway(conn, fake, TIERS).embed(agent_id=agent, texts=["hello world"], **kw)


def test_embedding_uses_the_assigned_model_and_is_on_the_ledger(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    assign(db, tenants.org_a)
    fake = EmbeddingFake()

    response = embed(db, tenants, agent, fake)

    assert fake.embed_calls[0]["model"] == MODEL
    assert fake.embed_calls[0]["dimensions"] == 1536
    assert response.requested_model == MODEL
    calls = rows(
        db,
        "select model, tokens_in, cost_usd from public.model_calls where agent_id = %s",
        str(agent),
    )
    assert calls[0]["model"] == f"{MODEL}-served" and calls[0]["tokens_in"] == 10
    events = rows(
        db,
        "select payload from public.events where agent_id = %s and type = 'model_call'",
        str(agent),
    )
    assert events[0]["payload"]["tier"] == "embedding"


def test_a_department_can_use_its_own_embedding_model(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    department = rows(db, "select department_id from public.agents where id = %s", str(agent))[0][
        "department_id"
    ]
    assign(db, tenants.org_a)
    assign(db, tenants.org_a, "qwen/qwen3-embedding-8b", department)
    fake = EmbeddingFake()

    embed(db, tenants, agent, fake)

    assert fake.embed_calls[0]["model"] == "qwen/qwen3-embedding-8b"


def test_with_no_embedding_model_assigned_nothing_is_guessed(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    fake = EmbeddingFake()

    with pytest.raises(TierNotConfigured):
        embed(db, tenants, agent, fake)
    assert fake.embed_calls == []


def test_the_kill_switch_stops_embedding(db: psycopg.Connection, tenants: Tenants) -> None:
    agent = make_agent(db, tenants.org_a)
    assign(db, tenants.org_a)
    set_kill_switch(db, tenants.org_a, on=True)
    fake = EmbeddingFake()

    with pytest.raises(KillSwitchEngaged):
        embed(db, tenants, agent, fake)
    assert fake.embed_calls == []


def test_sensitive_text_is_embedded_only_by_zero_retention_providers(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    assign(db, tenants.org_a)
    fake = EmbeddingFake()

    embed(db, tenants, agent, fake, sensitive=True)

    assert fake.embed_calls[0]["provider_preferences"] == SENSITIVE_PROVIDER_PREFERENCES


def test_vectors_of_the_wrong_width_are_refused_but_still_costed(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    assign(db, tenants.org_a)

    # Caught inside the caller's transaction, so the ledger row survives.
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        with pytest.raises(UpstreamError, match="width"):
            Gateway(conn, EmbeddingFake(width=768), TIERS).embed(agent_id=agent, texts=["x"])

    assert len(rows(db, "select 1 from public.model_calls where agent_id = %s", str(agent))) == 1


# --- The brain ---------------------------------------------------------------------


def test_search_never_compares_vectors_from_two_models_until_reembedded(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    assign(db, tenants.org_a)
    fake = EmbeddingFake()

    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        old = Brain(conn, HashingEmbedder()).insert_fact(
            org_id=tenants.org_a,
            claim="Invoices are paid through Stripe",
            admission=admit(conn, tenants.org_a),
        )
        brain = Brain(conn, GatewayEmbedder(Gateway(conn, fake, TIERS), agent_id=agent))
        assert brain.search("invoices paid") == [], "a hashing vector is not comparable"

        stale = brain.stale_embeddings(MODEL)
        assert [f.id for f in stale] == [old.id]
        assert brain.reembed(stale) == 1

        found = brain.search("invoices paid")
        assert [m.fact.id for m in found] == [old.id]
        assert found[0].fact.embedding_model == MODEL
        assert brain.stale_embeddings(MODEL) == []


def test_facts_record_the_model_that_embedded_them(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    assign(db, tenants.org_a)

    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        brain = Brain(conn, GatewayEmbedder(Gateway(conn, EmbeddingFake(), TIERS), agent_id=agent))
        fact = brain.insert_fact(
            org_id=tenants.org_a, claim="Acme makes turbines", admission=admit(conn, tenants.org_a)
        )

    assert fact.embedding_model == MODEL, "the assigned model, not the served name"


def test_the_gateway_embedder_batches_large_requests(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    assign(db, tenants.org_a)
    fake = EmbeddingFake()

    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        vectors = GatewayEmbedder(Gateway(conn, fake, TIERS), agent_id=agent).embed_many(
            [f"text {i}" for i in range(130)]
        )

    assert len(vectors) == 130
    assert [len(c["inputs"]) for c in fake.embed_calls] == [64, 64, 2]
