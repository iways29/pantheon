"""Step 9: Jev re-ranks what an agent recalls before it reads it (ADR 032)."""

from datetime import date, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.brain import Brain, HashingEmbedder
from app.db import acting_as, as_service_role
from app.gateway import Gateway, UpstreamError
from app.judge import Judge
from app.judge.starter_gates import RECALL_RANK
from app.judge.store import seed_gates
from app.tools import ToolContext, ToolRuntime, seed_tools
from tests.conftest_db import Tenants, admit
from tests.scripted_jev import ScriptedJev, noul, score
from tests.test_gateway import make_agent
from tests.test_judge import set_price
from tests.test_runners import TIERS

QUERY = "Which AI startups raised a seed round?"
FACTS = {
    "lumen": "Lumen raised a $4M seed round led by Gradient Ventures.",
    "tessel": "Tessel raised a $2M seed round in September 2026.",
    "office": "The studio's office has a whiteboard wall.",
    "nvidia": "Nvidia reported record data-centre revenue.",
}


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants) -> UUID:
    agent_id = make_agent(db, tenants.org_a, name="researcher")
    set_price(db, tenants.org_a)
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    with as_service_role(db) as conn:
        conn.execute(
            "update public.agents set allowed_tools = %s where id = %s",
            (["brain_search"], str(agent_id)),
        )
    return agent_id


@pytest.fixture
def facts(db: psycopg.Connection, tenants: Tenants) -> dict[str, UUID]:
    brain = Brain(db, HashingEmbedder())
    with as_service_role(db) as conn:
        stored = {
            key: brain.insert_fact(
                org_id=tenants.org_a, claim=claim, admission=admit(conn, tenants.org_a)
            ).id
            for key, claim in FACTS.items()
        }
    return stored


def seed(db: psycopg.Connection, tenants: Tenants) -> None:
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=[RECALL_RANK])


def by_claim(relevance: dict[str, float], premise: set[str] = frozenset()) -> ScriptedJev:  # type: ignore[assignment]
    """Answers by which fact is being judged."""

    def respond(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        key = next(k for k, claim in FACTS.items() if claim == state["claim"])
        return {
            "relevance": score(relevance.get(key, 0.0), 3),
            "contradicts_premise": noul(0.9 if key in premise else 0.02),
        }

    return ScriptedJev(respond=respond)


def search(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, jev: ScriptedJev | None, limit: int = 2
) -> dict[str, Any]:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        gateway = Gateway(conn, None, TIERS, systemone=jev)  # type: ignore[arg-type]
        result = ToolRuntime(
            ToolContext(
                connection=conn,
                org_id=tenants.org_a,
                agent_id=agent,
                agent_name="researcher",
                gateway=gateway,
                brain=Brain(conn, HashingEmbedder()),
                judge=Judge(conn, gateway) if jev is not None else None,
            )
        ).call("brain_search", {"query": QUERY, "limit": limit})
    assert result.status == "ok", result.output
    return result.output


def test_unrelated_facts_are_dropped_and_the_rest_ordered_by_relevance(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, facts: dict[str, UUID]
) -> None:
    seed(db, tenants)
    jev = by_claim({"lumen": 1.2, "tessel": 2.0, "office": 0.1, "nvidia": 0.2})

    out = search(db, tenants, agent, jev, limit=2)

    assert [f["id"] for f in out["facts"]] == [str(facts["tessel"]), str(facts["lumen"])]
    assert [f["relevance"] for f in out["facts"]] == [2.0, 1.2]
    # Twice the asked-for number was judged: all four facts, one request each.
    assert len(jev.calls_for("relevance")) == 4


def test_stale_facts_sink_and_premise_contradictions_are_flagged(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, facts: dict[str, UUID]
) -> None:
    seed(db, tenants)
    with as_service_role(db) as conn:
        conn.execute(
            "update public.facts set review_after = %s where id = %s",
            (date.today() - timedelta(days=1), str(facts["tessel"])),
        )
    jev = by_claim({"lumen": 1.8, "tessel": 2.0}, premise={"lumen"})

    out = search(db, tenants, agent, jev, limit=2)

    first, second = out["facts"]
    assert first["id"] == str(facts["lumen"]) and first["contradicts_premise"] is True
    assert second["id"] == str(facts["tessel"]) and second["stale"] is True
    assert "stale" not in first and "contradicts_premise" not in second


def test_without_the_gate_or_the_judge_the_vector_order_stands(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, facts: dict[str, UUID]
) -> None:
    jev = by_claim({})
    plain = search(db, tenants, agent, jev, limit=3)
    assert len(plain["facts"]) == 3 and jev.calls == []
    assert all("relevance" not in f for f in plain["facts"])

    seed(db, tenants)
    no_judge = search(db, tenants, agent, None, limit=3)
    assert [f["id"] for f in no_judge["facts"]] == [f["id"] for f in plain["facts"]]


def test_a_failed_judgment_falls_back_to_the_vector_order(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, facts: dict[str, UUID]
) -> None:
    seed(db, tenants)
    down = ScriptedJev(failure=UpstreamError("TypeSafe is down", reason="unavailable"))

    out = search(db, tenants, agent, down, limit=3)

    assert len(out["facts"]) == 3
    assert all("relevance" not in f for f in out["facts"])
