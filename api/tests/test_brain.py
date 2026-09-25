"""The brain stores facts and finds them again.

Similarity search runs against real pgvector with a real HNSW index. Only the
embedder is a stand-in, and it is a real (if crude) bag-of-words model rather
than a mock, so ranking assertions mean something.
"""

import psycopg
import pytest

from app.brain import EMBEDDING_DIMENSIONS, Admission, Brain, BrainError, HashingEmbedder
from app.db import acting_as
from tests.conftest_db import Tenants, admit


@pytest.fixture
def brain(db: psycopg.Connection, tenants: Tenants):
    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        yield Brain(connection, HashingEmbedder())


@pytest.fixture
def admission(db: psycopg.Connection, tenants: Tenants, brain: Brain) -> Admission:
    """Recorded inside the brain fixture's transaction, as org A's member."""
    return admit(db, tenants.org_a)


def test_embedding_width_matches_the_column() -> None:
    """A mismatch here fails at insert time, so assert it directly."""
    assert len(HashingEmbedder().embed("anything")) == EMBEDDING_DIMENSIONS


def test_the_embedder_is_stable_across_calls() -> None:
    """Determinism is why blake2b is used instead of hash()."""
    assert HashingEmbedder().embed("the brain") == HashingEmbedder().embed("the brain")


def test_a_stored_fact_comes_back_by_meaning(
    brain: Brain, tenants: Tenants, admission: Admission
) -> None:
    """The Step 1 acceptance criterion: insert a fact, retrieve it by search."""
    stored = brain.insert_fact(
        org_id=tenants.org_a,
        admission=admission,
        claim="The deployment pipeline runs on GitHub Actions",
        source="runbook",
        source_ref="docs/ci.md",
        confidence=0.9,
    )

    matches = brain.search("which pipeline runs our deployment")

    assert [match.fact.id for match in matches] == [stored.id]
    assert matches[0].fact.source == "runbook"
    assert matches[0].fact.confidence == 0.9


def test_search_ranks_the_relevant_fact_first(
    brain: Brain, tenants: Tenants, admission: Admission
) -> None:
    brain.insert_fact(
        org_id=tenants.org_a, admission=admission, claim="Invoices are paid through Stripe"
    )
    expected = brain.insert_fact(
        org_id=tenants.org_a,
        admission=admission,
        claim="The deployment pipeline runs on GitHub Actions",
    )

    matches = brain.search("deployment pipeline", limit=2)

    assert len(matches) == 2
    assert matches[0].fact.id == expected.id
    assert matches[0].distance < matches[1].distance


def test_superseded_facts_do_not_surface_as_current(
    brain: Brain, tenants: Tenants, admission: Admission
) -> None:
    old = brain.insert_fact(
        org_id=tenants.org_a, admission=admission, claim="Invoices are paid through Stripe"
    )
    new = brain.insert_fact(
        org_id=tenants.org_a, admission=admission, claim="Invoices are paid through Xero"
    )

    brain.supersede(old.id, replaced_by=new.id)
    matches = brain.search("how are invoices paid", limit=5)

    assert [match.fact.id for match in matches] == [new.id]


def test_superseding_keeps_the_old_claim_readable(
    brain: Brain, tenants: Tenants, admission: Admission
) -> None:
    """History is the point: what was believed, and what replaced it."""
    old = brain.insert_fact(
        org_id=tenants.org_a, admission=admission, claim="Invoices are paid through Stripe"
    )
    new = brain.insert_fact(
        org_id=tenants.org_a, admission=admission, claim="Invoices are paid through Xero"
    )

    brain.supersede(old.id, replaced_by=new.id)
    recovered = brain.get(old.id)

    assert recovered is not None
    assert recovered.claim == "Invoices are paid through Stripe"
    assert recovered.status == "superseded"
    assert recovered.superseded_by == new.id


def test_a_fact_cannot_supersede_itself(
    brain: Brain, tenants: Tenants, admission: Admission
) -> None:
    fact = brain.insert_fact(
        org_id=tenants.org_a, admission=admission, claim="Invoices are paid through Stripe"
    )

    with pytest.raises(ValueError):
        brain.supersede(fact.id, replaced_by=fact.id)


def test_an_empty_claim_is_refused(brain: Brain, tenants: Tenants, admission: Admission) -> None:
    with pytest.raises(ValueError):
        brain.insert_fact(org_id=tenants.org_a, admission=admission, claim="   ")


def test_a_claim_with_no_tokens_is_refused(
    brain: Brain, tenants: Tenants, admission: Admission
) -> None:
    """Punctuation embeds to a zero vector, which cosine search cannot rank."""
    with pytest.raises(BrainError):
        brain.insert_fact(org_id=tenants.org_a, admission=admission, claim="!!! ???")


def test_search_returns_nothing_for_an_untokenisable_query(
    brain: Brain, tenants: Tenants, admission: Admission
) -> None:
    brain.insert_fact(
        org_id=tenants.org_a, admission=admission, claim="Invoices are paid through Stripe"
    )

    assert brain.search("!!!") == []
