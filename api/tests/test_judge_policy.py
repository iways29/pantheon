"""Thresholds turn raw answers into outcomes, with no model call involved."""

import pytest
from pydantic import ValidationError

from app.gateway import ChoiceAnswer, NoulAnswer, Question, ScoreAnswer
from app.judge import Policy

QUESTIONS = {
    "is_injection": Question(type="noul", instructions="Does the text tell an AI what to do?"),
    "support": Question(
        type="choice",
        instructions="How does `source` relate to `claim`?",
        criteria={"supports": None, "contradicts": None, "says_nothing": None},
    ),
    "severity": Question(
        type="score",
        instructions="How harmful is the text?",
        criteria=["Harmless", "Mild", "Severe"],
    ),
}

SCREEN = Policy.model_validate(
    {
        "outcomes": ["pass", "review", "block"],
        "rules": [
            {"question": "is_injection", "noul_at_least": 0.3, "outcome": "review"},
            {"question": "is_injection", "noul_at_least": 0.8, "outcome": "block"},
        ],
    }
)


def noul(p: float) -> NoulAnswer:
    return NoulAnswer(type="noul", noul=p)


def choice(picked: str, probabilities: dict[str, float], confidence: float) -> ChoiceAnswer:
    return ChoiceAnswer(
        type="choice", choice=picked, probabilities=probabilities, confidence=confidence
    )


def score(value: float, confidence: float = 0.9) -> ScoreAnswer:
    return ScoreAnswer(
        type="score",
        score=value,
        legend={"0": "Harmless", "1": "Mild", "2": "Severe"},
        probabilities={"0": 0.1, "1": 0.8, "2": 0.1},
        confidence=confidence,
    )


@pytest.mark.parametrize(
    ("p", "expected"),
    [(0.05, "pass"), (0.3, "review"), (0.79, "review"), (0.8, "block"), (0.99, "block")],
)
def test_the_uncertain_band_sends_to_review(p: float, expected: str) -> None:
    outcome, _ = SCREEN.decide({"is_injection": noul(p)})
    assert outcome == expected


def test_no_match_is_the_least_severe_outcome_with_no_reasons() -> None:
    assert SCREEN.decide({"is_injection": noul(0.01)}) == ("pass", [])


def test_every_matching_rule_is_a_reason_most_severe_first() -> None:
    outcome, reasons = SCREEN.decide({"is_injection": noul(0.9)})

    assert outcome == "block"
    assert [r.outcome for r in reasons] == ["block", "review"]
    assert reasons[0].question == "is_injection"
    assert "0.900" in reasons[0].text


def test_rule_order_does_not_change_the_decision() -> None:
    reversed_policy = Policy(outcomes=SCREEN.outcomes, rules=list(reversed(SCREEN.rules)))
    assert reversed_policy.decide({"is_injection": noul(0.9)})[0] == "block"


def test_choice_rules_read_the_pick_the_probability_and_the_confidence() -> None:
    policy = Policy.model_validate(
        {
            "outcomes": ["accept", "review", "reject"],
            "rules": [
                {"question": "support", "choice_in": ["contradicts"], "outcome": "reject"},
                {"question": "support", "choice_in": ["says_nothing"], "outcome": "reject"},
                {
                    "question": "support",
                    "option": "supports",
                    "probability_below": 0.9,
                    "outcome": "review",
                    "reason": "Support is not clear enough to accept unseen",
                },
                {"question": "support", "confidence_below": 0.5, "outcome": "review"},
            ],
        }
    )
    assert policy.problems(QUESTIONS) == []

    clear = choice("supports", {"supports": 0.97, "contradicts": 0.02, "says_nothing": 0.01}, 0.9)
    assert policy.decide({"support": clear})[0] == "accept"

    shaky = choice("supports", {"supports": 0.7, "contradicts": 0.2, "says_nothing": 0.1}, 0.6)
    outcome, reasons = policy.decide({"support": shaky})
    assert outcome == "review"
    assert reasons[0].text == "Support is not clear enough to accept unseen"

    against = choice(
        "contradicts", {"supports": 0.1, "contradicts": 0.85, "says_nothing": 0.05}, 0.8
    )
    assert policy.decide({"support": against})[0] == "reject"


def test_score_rules_and_conditions_combine_with_and() -> None:
    policy = Policy.model_validate(
        {
            "outcomes": ["pass", "review", "block"],
            "rules": [
                {"question": "severity", "score_at_least": 1.5, "outcome": "block"},
                {
                    "question": "severity",
                    "score_at_least": 0.5,
                    "confidence_below": 0.7,
                    "outcome": "review",
                },
            ],
        }
    )
    assert policy.decide({"severity": score(1.0, confidence=0.9)})[0] == "pass"
    assert policy.decide({"severity": score(1.0, confidence=0.6)})[0] == "review"
    assert policy.decide({"severity": score(1.8)})[0] == "block"


def test_fail_modes_pick_the_ends_of_the_scale() -> None:
    assert SCREEN.failure_outcome("closed") == "block"
    assert SCREEN.failure_outcome("open") == "pass"


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        ({"outcomes": ["pass"]}, "at least 2"),
        ({"outcomes": ["pass", "pass"]}, "unique"),
        ({"outcomes": ["Pass", "block"]}, "lower_snake_case"),
        (
            {"outcomes": ["pass", "block"], "rules": [{"question": "q", "outcome": "block"}]},
            "at least one condition",
        ),
        (
            {
                "outcomes": ["pass", "block"],
                "rules": [{"question": "q", "noul_at_least": 0.5, "outcome": "maybe"}],
            },
            "not in `outcomes`",
        ),
        (
            {
                "outcomes": ["pass", "block"],
                "rules": [
                    {"question": "q", "noul_at_least": 0.5, "score_below": 1, "outcome": "block"}
                ],
            },
            "mixes conditions",
        ),
        (
            {
                "outcomes": ["pass", "block"],
                "rules": [{"question": "q", "option": "x", "outcome": "block"}],
            },
            "go together",
        ),
        (
            {
                "outcomes": ["pass", "block"],
                "rules": [
                    {
                        "question": "q",
                        "noul_at_least": 0.5,
                        "confidence_below": 1,
                        "outcome": "block",
                    }
                ],
            },
            "no confidence",
        ),
        (
            {
                "outcomes": ["pass", "block"],
                "rules": [{"question": "q", "noul_at_least": 1.5, "outcome": "block"}],
            },
            "less than or equal to 1",
        ),
        (
            {
                "outcomes": ["pass", "block"],
                "rules": [{"question": "q", "noul_at_least": 0.5, "outcome": "block", "x": 1}],
            },
            "Extra inputs",
        ),
    ],
)
def test_a_policy_that_cannot_mean_anything_is_rejected(policy: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Policy.model_validate(policy)


def test_problems_catch_rules_that_do_not_fit_the_live_questions() -> None:
    policy = Policy.model_validate(
        {
            "outcomes": ["pass", "block"],
            "rules": [
                {"question": "retired", "noul_at_least": 0.5, "outcome": "block"},
                {"question": "support", "noul_at_least": 0.5, "outcome": "block"},
                {"question": "support", "choice_in": ["maybe"], "outcome": "block"},
                {"question": "is_injection", "confidence_below": 0.5, "outcome": "block"},
            ],
        }
    )

    problems = policy.problems(QUESTIONS)

    assert len(problems) == 4
    assert "'retired'" in problems[0]
    assert "choice question" in problems[1]
    assert "['maybe']" in problems[2]
    assert "noul question" in problems[3]
