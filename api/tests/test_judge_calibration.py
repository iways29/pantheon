"""Step 5.4: the labelled sets and the eval harness.

Acceptance: the harness runs on labelled cases for the brain gate and the
screening gate and prints precision and recall. Here TypeSafe is scripted as
an oracle that knows each case's label (or gets chosen cases wrong), so the
assertions are about the measuring, not about Jev; the live numbers come from
`scripts.judge_eval run all` with a real key.
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.db import acting_as, as_service_role
from app.gateway import Gateway, NoulAnswer
from app.judge import Decision, Judge, Policy
from app.judge.calibration import import_cases, load_cases, run_eval
from app.judge.evaluation import (
    CaseResult,
    classify,
    consistency,
    recommendations,
    sweep,
)
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from scripts.judge_eval import print_report
from tests.conftest_db import Tenants
from tests.scripted_jev import ScriptedJev, choice, noul, score
from tests.test_gateway import TIERS, RecordingTransport, make_agent
from tests.test_judge import set_price

CASES_DIR = Path(__file__).parents[1] / "evals" / "cases"


# --- Pure metrics ---------------------------------------------------------------


def test_precision_and_recall_per_outcome_and_for_anything_flagged() -> None:
    expected = ["pass", "pass", "review", "block", "block"]
    predicted = ["pass", "review", "review", "block", "pass"]

    m = classify(expected, predicted, ["pass", "review", "block"])

    assert m["accuracy"] == 0.6
    assert m["per_outcome"]["review"] == {
        "precision": 0.5,
        "recall": 1.0,
        "f1": 0.6667,
        "support": 1,
    }
    assert m["per_outcome"]["block"]["precision"] == 1.0
    assert m["per_outcome"]["block"]["recall"] == 0.5
    # Flagged = not `pass`: 3 truly flagged, 2 caught, 1 false alarm.
    assert m["flagged"] == {"precision": 0.6667, "recall": 0.6667, "f1": 0.6667}


def decision(outcome: str, p: float, *, failed: bool = False) -> Decision:
    return Decision(
        gate="g",
        gate_version=1,
        outcome=outcome,
        reasons=(),
        failed=failed,
        answers={} if failed else {"hazard": NoulAnswer(type="noul", noul=p)},
    )


POLICY = Policy.model_validate(
    {
        "outcomes": ["pass", "block"],
        "rules": [{"question": "hazard", "noul_at_least": 0.8, "outcome": "block"}],
    }
)


def test_the_sweep_finds_a_better_threshold_from_stored_answers() -> None:
    # Hazards score 0.55-0.7; the current 0.8 threshold misses all of them.
    results = [
        CaseResult("a", "block", (decision("pass", 0.7),)),
        CaseResult("b", "block", (decision("pass", 0.6),)),
        CaseResult("c", "block", (decision("pass", 0.55),)),
        CaseResult("d", "pass", (decision("pass", 0.1),)),
        CaseResult("e", "pass", (decision("pass", 0.3),)),
    ]

    swept = sweep(POLICY, results)

    assert len(swept) == 1
    line = swept[0]
    assert line["current"] == 0.8 and line["current_macro_f1"] < 0.5
    assert 0.3 < line["best"] <= 0.55
    assert line["best_macro_f1"] == 1.0
    rec = recommendations(swept)
    assert rec[0]["from"] == 0.8 and rec[0]["to"] == line["best"]


def test_no_recommendation_when_the_current_value_is_already_best() -> None:
    results = [
        CaseResult("a", "block", (decision("block", 0.9),)),
        CaseResult("b", "pass", (decision("pass", 0.1),)),
    ]
    assert recommendations(sweep(POLICY, results)) == []


def test_failed_calls_are_left_out_of_the_scores() -> None:
    results = [
        CaseResult("a", "block", (decision("block", 0.0, failed=True),)),
        CaseResult("b", "pass", (decision("pass", 0.1),)),
    ]
    line = sweep(POLICY, results)[0]
    at_current = next(v for v in line["values"] if v["value"] == line["current"])
    assert at_current["accuracy"] == 1.0, "only case b is scored"


def test_consistency_counts_cases_that_kept_their_outcome() -> None:
    results = [
        CaseResult("a", "block", (decision("block", 0.9), decision("block", 0.85))),
        CaseResult("b", "block", (decision("block", 0.82), decision("pass", 0.75))),
    ]

    m = consistency(results)

    assert m == {"cases": 2, "same_outcome": 0.5, "max_noul_spread": 0.07}


# --- Case files ---------------------------------------------------------------


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants) -> UUID:
    agent_id = make_agent(db, tenants.org_a)
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=STARTER_GATES)
    return agent_id


def test_every_case_file_imports_against_its_gate(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    counts = {}
    for path in sorted(CASES_DIR.glob("*.json")):
        gate, count = import_cases(db, user_id=tenants.user_a, org_id=tenants.org_a, path=path)
        counts[gate] = count

    assert counts == {"brain_claim": 35, "brain_neighbour": 13, "content_screen": 12}
    cases = load_cases(db, org_id=tenants.org_a, gate="brain_claim")
    assert {c.weak_spot for c in cases} >= {"arithmetic", "dates", "double_negative", "adversarial"}
    assert all(c.label_source == "author" for c in cases)

    # Re-importing updates in place rather than duplicating.
    import_cases(
        db, user_id=tenants.user_a, org_id=tenants.org_a, path=CASES_DIR / "brain_claim.json"
    )
    assert len(load_cases(db, org_id=tenants.org_a, gate="brain_claim")) == 35


def test_a_label_that_is_not_an_outcome_of_the_gate_is_refused(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {"gate": "brain_claim", "cases": [{"id": "x", "expected": "acept", "state": {}}]}
        )
    )

    with pytest.raises(ValueError, match="not an outcome"):
        import_cases(db, user_id=tenants.user_a, org_id=tenants.org_a, path=bad)


# --- The harness, end to end --------------------------------------------------------


def oracle(labels: dict[str, str], wrong: set[str] = frozenset()) -> ScriptedJev:  # type: ignore[assignment]
    """Answers each case so the gate lands on its label, except `wrong` ones."""
    by_state = {}
    for path in CASES_DIR.glob("*.json"):
        for case in json.loads(path.read_text())["cases"]:
            by_state[json.dumps(case["state"], sort_keys=True)] = case

    def respond(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        case = by_state[json.dumps(state, sort_keys=True)]
        target = labels.get(case["id"], case["expected"])
        if case["id"] in wrong:
            target = {"accept": "reject", "reject": "accept"}.get(target, target)
        return _answers_for(target, questions)

    return ScriptedJev(respond=respond, tokens_in=400)


def _answers_for(target: str, questions: dict[str, Any]) -> dict[str, Any]:
    if "support" in questions:  # brain_claim
        support = {"accept": "supports", "reject": "contradicts", "review": "supports"}[target]
        answers = {"support": choice(support, list(questions["support"].criteria))}
        if target == "review":
            answers["opinion"] = noul(0.5)
        return answers
    if "sameness" in questions:  # brain_neighbour
        level = {"different": 0.1, "related": 1.0, "duplicate": 2.0}.get(target, 1.0)
        answers: dict[str, Any] = {"sameness": score(level)}
        if target == "conflict":
            answers["contradicts"] = noul(0.9)
        if target == "update":
            answers["updates"] = noul(0.9)
        return answers
    injection = {"quarantined": 0.95, "review": 0.5, "clean": 0.02}[target]
    return {"prompt_injection": noul(injection)}


@pytest.mark.parametrize("gate", ["brain_claim", "content_screen"])
def test_the_harness_measures_a_gate_and_records_the_run(
    db: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,
    gate: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_cases(db, user_id=tenants.user_a, org_id=tenants.org_a, path=CASES_DIR / f"{gate}.json")
    jev = oracle({}, wrong={"unsupported"})

    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        gateway = Gateway(conn, RecordingTransport(), TIERS, systemone=jev)
        report = run_eval(
            conn, Judge(conn, gateway), org_id=tenants.org_a, agent_id=agent, gate=gate, repeats=2
        )

    cases = len(load_cases(db, org_id=tenants.org_a, gate=gate))
    assert len(jev.calls) == cases * 2
    if gate == "brain_claim":
        assert [w["case"] for w in report.wrong] == ["unsupported"]
        assert report.classification["per_outcome"]["reject"]["recall"] < 1.0
    else:
        assert report.wrong == []
        assert report.classification["accuracy"] == 1.0
    assert report.consistency["same_outcome"] == 1.0
    assert report.tokens_in == cases * 2 * 400
    assert report.cost_usd == pytest.approx(cases * 2 * 400 * 0.042 / 1_000_000, abs=1e-5)

    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("select * from public.judge_eval_runs where gate = %s", (gate,))
        saved = cursor.fetchall()
    assert len(saved) == 1
    assert saved[0]["cases"] == cases and saved[0]["repeats"] == 2
    assert saved[0]["metrics"]["classification"]["accuracy"] == report.classification["accuracy"]

    print_report(report)
    printed = capsys.readouterr().out
    assert "precision" in printed and "recall" in printed
    assert f"=== {gate} v1" in printed
