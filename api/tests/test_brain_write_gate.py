"""The brain write gate, against the real database and a scripted TypeSafe.

Step 5.2 acceptance: a new fact is checked before insertion; a duplicate is
skipped; a contradiction becomes disputed or goes to review; a fabricated
quote is rejected without a model call; the judgment behind every fact is
queryable; and with the service down, brain writes fail closed.
"""

from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from app.brain import Admission, Brain, HashingEmbedder
from app.brain.write_gate import BrainWriter, FactCandidate, WriteResult, _evidence
from app.db import acting_as, as_service_role
from app.gateway import Gateway, UpstreamError
from app.judge import Judge
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from tests.conftest_db import Tenants
from tests.scripted_jev import ScriptedJev
from tests.test_gateway import TIERS, RecordingTransport, make_agent
from tests.test_judge import set_price

FOUNDED = "Acme Corp was founded in 2019 in Leeds."
SOURCE_TEXT = (
    "Acme Corp was founded in 2019 in Leeds. It makes small wind turbines. "
    "The company employs 40 people."
)


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants) -> UUID:
    agent_id = make_agent(db, tenants.org_a)
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=STARTER_GATES)
    return agent_id


def propose(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,
    jev: ScriptedJev,
    claim: str = FOUNDED,
    **fields: Any,
) -> WriteResult:
    run_key = fields.pop("idempotency_key", None)
    candidate = FactCandidate(
        claim=claim,
        source=fields.pop("source", "agent:research"),
        source_text=fields.pop("source_text", SOURCE_TEXT),
        **fields,
    )
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        gateway = Gateway(conn, RecordingTransport(), TIERS, systemone=jev)
        writer = BrainWriter(conn, Brain(conn, HashingEmbedder()), Judge(conn, gateway))
        return writer.propose(
            candidate, org_id=tenants.org_a, agent_id=agent, idempotency_key=run_key
        )


def rows(db: psycopg.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def facts(db: psycopg.Connection, tenants: Tenants) -> list[dict[str, Any]]:
    return rows(
        db,
        "select * from public.facts where org_id = %s order by created_at",
        str(tenants.org_a),
    )


# --- A new fact is checked before insertion ----------------------------------


def test_a_clean_supported_fact_is_accepted_after_one_judgment(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    jev = ScriptedJev()

    result = propose(db, tenants, agent, jev)

    assert result.outcome == "accepted"
    assert result.fact is not None and result.fact.status == "active"
    assert len(jev.calls) == 1, "no existing facts, so no neighbour judgments"
    state = jev.calls[0]["state"]
    assert state["claim"] == FOUNDED
    assert state["evidence"] == SOURCE_TEXT
    assert set(jev.calls[0]["questions"]) == {
        "standalone",
        "opinion",
        "personal_or_secret",
        "ai_instruction",
        "evidence_instruction",
        "volatile",
        "support",
    }


def test_the_judgment_behind_every_fact_is_queryable(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    result = propose(db, tenants, agent, ScriptedJev())

    behind = rows(
        db,
        """
        select j.question_id, j.output
        from public.facts f
        join public.judgments j on j.request_id = f.admitted_by
        where f.id = %s
        order by j.question_id
        """,
        str(result.fact.id),  # type: ignore[union-attr]
    )
    assert [r["question_id"] for r in behind] == [
        "ai_instruction",
        "evidence_instruction",
        "opinion",
        "personal_or_secret",
        "standalone",
        "support",
        "volatile",
    ]
    support = next(r for r in behind if r["question_id"] == "support")
    assert support["output"]["choice"] == "supports"

    decided = rows(
        db,
        "select payload from public.events where type = 'fact_write_decided' and agent_id = %s",
        str(agent),
    )
    assert decided[0]["payload"]["outcome"] == "accepted"
    assert decided[0]["payload"]["fact_id"] == str(result.fact.id)  # type: ignore[union-attr]


def test_a_fact_that_is_likely_to_change_gets_a_review_date(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    result = propose(
        db,
        tenants,
        agent,
        ScriptedJev(nouls={"volatile": 0.8}),
        claim="The company employs 40 people.",
    )

    assert result.fact is not None
    assert result.fact.review_after == date.today() + timedelta(days=90)


def test_evidence_that_instructs_the_checker_goes_to_a_person(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    result = propose(db, tenants, agent, ScriptedJev(nouls={"evidence_instruction": 0.9}))

    assert result.outcome == "review" and result.fact is None


# --- Duplicates are skipped ---------------------------------------------------


def test_an_exact_duplicate_is_skipped_without_a_model_call(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    jev = ScriptedJev()
    first = propose(db, tenants, agent, jev)

    again = propose(db, tenants, agent, jev, claim="  acme corp was founded in 2019 in LEEDS ")

    assert again.outcome == "duplicate"
    assert again.related_fact_id == first.fact.id  # type: ignore[union-attr]
    assert len(jev.calls) == 1, "the duplicate was caught in code"
    assert len(facts(db, tenants)) == 1


def test_a_reworded_duplicate_is_skipped_on_the_neighbour_judgment(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    first = propose(db, tenants, agent, ScriptedJev())
    jev = ScriptedJev(scores={"sameness": 1.9})

    result = propose(
        db, tenants, agent, jev, claim="Acme Corp, founded in 2019, is based in Leeds."
    )

    assert result.outcome == "duplicate"
    assert result.related_fact_id == first.fact.id  # type: ignore[union-attr]
    assert len(jev.calls_for("sameness")) == 1
    assert jev.calls_for("sameness")[0]["state"]["existing_fact"] == FOUNDED
    assert len(facts(db, tenants)) == 1


# --- A contradiction becomes disputed or goes to review -----------------------


def test_a_clear_contradiction_is_stored_as_disputed(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    propose(db, tenants, agent, ScriptedJev())
    claim = "Acme Corp was founded in 2021 in Leeds."
    source = "Acme Corp was founded in 2021 in Leeds, according to its filing."

    result = propose(
        db,
        tenants,
        agent,
        ScriptedJev(nouls={"contradicts": 0.9}, scores={"sameness": 0.8}),
        claim=claim,
        source_text=source,
    )

    assert result.outcome == "disputed"
    assert result.fact is not None and result.fact.status == "disputed"
    statuses = [f["status"] for f in facts(db, tenants)]
    assert statuses == ["active", "disputed"], "the existing fact stands until a person decides"


def test_an_unclear_contradiction_is_held_for_review_once(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    propose(db, tenants, agent, ScriptedJev())
    claim = "Acme Corp was founded in 2021 in Leeds."
    source = "Acme Corp was founded in 2021 in Leeds."
    jev = ScriptedJev(nouls={"contradicts": 0.5})

    held = propose(
        db, tenants, agent, jev, claim=claim, source_text=source, idempotency_key="run-1:c1"
    )
    again = propose(
        db, tenants, agent, jev, claim=claim, source_text=source, idempotency_key="run-1:c1"
    )

    assert held.outcome == "review" and held.fact is None
    assert again.approval_id == held.approval_id, "a retried step queues nothing new"
    approvals = rows(db, "select * from public.approvals where org_id = %s", str(tenants.org_a))
    assert len(approvals) == 1
    assert approvals[0]["status"] == "pending"
    assert approvals[0]["action_type"] == "fact_write"
    assert approvals[0]["payload"]["claim"] == claim
    assert approvals[0]["agent_output_snapshot"]["source_text"] == source
    assert len(facts(db, tenants)) == 1


def test_a_newer_value_supersedes_the_old_fact(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    old = propose(db, tenants, agent, ScriptedJev(), claim="The company employs 40 people.").fact
    claim = "The company employs 55 people."

    result = propose(
        db,
        tenants,
        agent,
        ScriptedJev(nouls={"updates": 0.9, "contradicts": 0.9}),
        claim=claim,
        source_text="As of this year the company employs 55 people.",
    )

    assert result.outcome == "superseded"
    assert result.related_fact_id == old.id  # type: ignore[union-attr]
    stored = {f["claim"]: f for f in facts(db, tenants)}
    assert stored["The company employs 40 people."]["status"] == "superseded"
    assert stored["The company employs 40 people."]["superseded_by"] == result.fact.id  # type: ignore[union-attr]
    assert stored[claim]["status"] == "active"


# --- Code checks: rejected without a model call --------------------------------


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (
            {"quote": "Acme Corp was founded in 2017 in Leeds."},
            "Fabricated",
        ),
        (
            {"claim": "Acme Corp was founded in 2019 and employs 400 people."},
            "Numbers not in the evidence: 400",
        ),
        ({"source_text": ""}, "No evidence"),
        ({"source": "  "}, "No provenance"),
        ({"claim": "x" * 501}, "the limit is 500"),
    ],
)
def test_claims_that_fail_the_code_checks_never_reach_the_model(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,
    fields: dict[str, Any],
    reason: str,
) -> None:
    jev = ScriptedJev()
    claim = fields.pop("claim", FOUNDED)

    result = propose(db, tenants, agent, jev, claim=claim, **fields)

    assert result.outcome == "rejected"
    assert reason in result.reasons[0]
    assert jev.calls == []
    assert facts(db, tenants) == []


def test_a_true_quote_is_sent_as_the_evidence(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    jev = ScriptedJev()

    result = propose(db, tenants, agent, jev, quote="founded in 2019  in Leeds")

    assert result.outcome == "accepted"
    assert jev.calls[0]["state"]["evidence"] == "founded in 2019  in Leeds"
    assert result.fact.quote == "founded in 2019  in Leeds"  # type: ignore[union-attr]


# --- The claim judgment decides ------------------------------------------------


@pytest.mark.parametrize(
    ("jev", "outcome"),
    [
        (ScriptedJev(nouls={"opinion": 0.9}), "rejected"),
        (ScriptedJev(nouls={"opinion": 0.5}), "review"),
        (ScriptedJev(nouls={"ai_instruction": 0.8}), "rejected"),
        (ScriptedJev(nouls={"personal_or_secret": 0.3}), "review"),
        (ScriptedJev(nouls={"standalone": 0.2}), "rejected"),
        (ScriptedJev(choices={"support": "says_nothing"}), "rejected"),
        (ScriptedJev(choices={"support": "contradicts"}), "rejected"),
    ],
)
def test_the_claim_judgment_rejects_or_holds(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, jev: ScriptedJev, outcome: str
) -> None:
    result = propose(db, tenants, agent, jev)

    assert result.outcome == outcome
    assert result.fact is None
    assert facts(db, tenants) == []


# --- Fail closed ------------------------------------------------------------------


def test_with_typesafe_down_nothing_is_written(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    jev = ScriptedJev(failure=UpstreamError("down", reason="unreachable"))

    result = propose(db, tenants, agent, jev)

    assert result.outcome == "rejected"
    assert "unreachable" in result.reasons[0] and "fails closed" in result.reasons[0]
    assert facts(db, tenants) == []


def test_with_the_neighbour_judgment_down_the_claim_waits_for_a_person(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    propose(db, tenants, agent, ScriptedJev())

    def fail_neighbours(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if "sameness" in questions:
            raise UpstreamError("down", reason="timeout")
        return {}

    result = propose(
        db,
        tenants,
        agent,
        ScriptedJev(respond=fail_neighbours),
        claim="Acme Corp makes small wind turbines.",
    )

    assert result.outcome == "review"
    assert len(facts(db, tenants)) == 1


def test_sensitive_claims_go_to_a_person_not_to_typesafe(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    jev = ScriptedJev()

    result = propose(db, tenants, agent, jev, sensitive=True)

    assert result.outcome == "review"
    assert jev.calls == []


# --- No bypass --------------------------------------------------------------------


def test_the_database_refuses_a_fact_without_an_admission(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="judgment that admitted it"):
        with acting_as(db, user_id=str(tenants.user_a)) as conn, conn.cursor() as cursor:
            cursor.execute(
                "insert into public.facts (org_id, claim) values (%s, 'Unjudged')",
                (str(tenants.org_a),),
            )


def test_the_database_refuses_an_admission_it_cannot_find(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="names no brain_claim judgment"):
        with acting_as(db, user_id=str(tenants.user_a)) as conn:
            Brain(conn, HashingEmbedder()).insert_fact(
                org_id=tenants.org_a, claim="Forged", admission=Admission(request_id=uuid4())
            )


# --- Evidence is filtered in code first ----------------------------------------


def test_long_evidence_is_cut_to_the_sentences_nearest_the_claim() -> None:
    filler = " ".join(f"Unrelated sentence number {i} about the weather." for i in range(200))
    text = f"{filler} Acme Corp was founded in 2019 in Leeds. {filler}"
    candidate = FactCandidate(claim=FOUNDED, source="x", source_text=text)

    evidence = _evidence(candidate, FOUNDED, limit=300)

    assert len(evidence) <= 300
    assert "Acme Corp was founded in 2019 in Leeds." in evidence
