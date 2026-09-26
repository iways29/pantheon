"""Turning raw answers into an outcome, with thresholds held as data.

A gate's policy lists its outcomes from least to most severe (for example
`pass`, `review`, `block`) and a set of rules. Each rule names one question,
one or more conditions on its answer (all must hold) and the outcome it calls
for. Every rule that matches is a reason; the decision is the most severe
outcome any matching rule calls for, or the least severe when none match.

Taking the most severe match makes the result independent of rule order, and
gives the "uncertain, ask a person" band a natural shape:

    {"question": "is_injection", "noul_at_least": 0.3, "outcome": "review"}
    {"question": "is_injection", "noul_at_least": 0.8, "outcome": "block"}

Pure functions only: no database, no network. The same answers under a new
policy give a new decision without another model call, which is what lets the
eval harness (Step 5.4) try thresholds cheaply.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.gateway.systemone import ChoiceAnswer, NoulAnswer, Question, ScoreAnswer

AnswerValue = NoulAnswer | ChoiceAnswer | ScoreAnswer
FailMode = Literal["open", "closed"]

_OUTCOME_PATTERN = r"^[a-z][a-z0-9_]*$"


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    outcome: str = Field(pattern=_OUTCOME_PATTERN)
    #: Plain words for the owner; generated from the conditions when absent.
    reason: str | None = None

    # Noul: the probability the answer is yes.
    noul_at_least: float | None = Field(default=None, ge=0, le=1)
    noul_below: float | None = Field(default=None, ge=0, le=1)
    # Choice: the chosen option, or the probability of one named option.
    choice_in: list[str] | None = Field(default=None, min_length=1)
    option: str | None = None
    probability_at_least: float | None = Field(default=None, ge=0, le=1)
    probability_below: float | None = Field(default=None, ge=0, le=1)
    # Score: the probability-weighted level (0 is the first level).
    score_at_least: float | None = None
    score_below: float | None = None
    # Choice or Score: how concentrated the distribution is. A signal about
    # the answer, never permission to act.
    confidence_at_least: float | None = Field(default=None, ge=0, le=1)
    confidence_below: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _coherent(self) -> "Rule":
        has_probability = (
            self.probability_at_least is not None or self.probability_below is not None
        )
        if (self.option is None) != (not has_probability):
            raise ValueError("`option` and `probability_at_least`/`probability_below` go together")
        kinds = self._kinds()
        if not kinds and not self._has_confidence():
            raise ValueError("a rule needs at least one condition")
        if len(kinds) > 1:
            raise ValueError(f"a rule mixes conditions for {sorted(kinds)}")
        if "noul" in kinds and self._has_confidence():
            raise ValueError("Noul answers carry no confidence")
        return self

    def _has_confidence(self) -> bool:
        return self.confidence_at_least is not None or self.confidence_below is not None

    def _kinds(self) -> set[str]:
        kinds: set[str] = set()
        if self.noul_at_least is not None or self.noul_below is not None:
            kinds.add("noul")
        if self.choice_in is not None or self.option is not None:
            kinds.add("choice")
        if self.score_at_least is not None or self.score_below is not None:
            kinds.add("score")
        return kinds

    def accepts(self, question_type: str) -> bool:
        kinds = self._kinds()
        if kinds:
            return question_type in kinds
        return question_type in ("choice", "score")

    def matches(self, answer: AnswerValue) -> bool:
        checks: list[bool] = []
        if isinstance(answer, NoulAnswer):
            checks += _bounds(answer.noul, self.noul_at_least, self.noul_below)
        if isinstance(answer, ChoiceAnswer):
            if self.choice_in is not None:
                checks.append(answer.choice in self.choice_in)
            if self.option is not None:
                value = answer.probabilities.get(self.option, 0.0)
                checks += _bounds(value, self.probability_at_least, self.probability_below)
        if isinstance(answer, ScoreAnswer):
            checks += _bounds(answer.score, self.score_at_least, self.score_below)
        if isinstance(answer, ChoiceAnswer | ScoreAnswer):
            checks += _bounds(answer.confidence, self.confidence_at_least, self.confidence_below)
        return bool(checks) and all(checks)

    def describe(self, answer: AnswerValue) -> str:
        if self.reason:
            return self.reason
        parts: list[str] = []
        if isinstance(answer, NoulAnswer):
            parts.append(f"yes-probability {answer.noul:.3f}")
        if isinstance(answer, ChoiceAnswer):
            parts.append(f"chose {answer.choice!r}")
            if self.option is not None:
                value = answer.probabilities.get(self.option, 0.0)
                parts.append(f"P({self.option})={value:.3f}")
        if isinstance(answer, ScoreAnswer):
            parts.append(f"score {answer.score:.2f}")
        if isinstance(answer, ChoiceAnswer | ScoreAnswer) and self._has_confidence():
            parts.append(f"confidence {answer.confidence:.3f}")
        return f"{self.question}: {', '.join(parts)}"


def _bounds(value: float, at_least: float | None, below: float | None) -> list[bool]:
    checks = []
    if at_least is not None:
        checks.append(value >= at_least)
    if below is not None:
        checks.append(value < below)
    return checks


@dataclass(frozen=True)
class Reason:
    #: The question behind it; None for a reason about the call itself.
    question: str | None
    outcome: str
    text: str


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Least severe first. The first is the default when no rule matches and
    #: the fail-open outcome; the last is the fail-closed outcome.
    outcomes: list[str] = Field(min_length=2)
    rules: list[Rule] = Field(default_factory=list)
    #: Named alternative rule sets over the same questions and outcomes, such
    #: as `strict` and `normal` guardrails. The same answers can be decided
    #: under any of them with no new model call. `rules` is the default.
    profiles: dict[str, list[Rule]] = Field(default_factory=dict)
    #: Gate-specific numbers the calling code reads (length limits, how many
    #: neighbours to compare, how long until a volatile fact is rechecked).
    #: Here so they are data like the thresholds, not constants in code.
    settings: dict[str, float | int | str | bool] = Field(default_factory=dict)

    def setting(self, name: str, default: float) -> float:
        value = self.settings.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"setting {name!r} must be a number")
        return float(value)

    def text_setting(self, name: str, default: str | None = None) -> str | None:
        value = self.settings.get(name, default)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"setting {name!r} must be text")
        return value

    @model_validator(mode="after")
    def _known_outcomes(self) -> "Policy":
        if len(set(self.outcomes)) != len(self.outcomes):
            raise ValueError("outcomes must be unique")
        for outcome in self.outcomes:
            if not re.match(_OUTCOME_PATTERN, outcome):
                raise ValueError(f"outcome {outcome!r} must be lower_snake_case")
        named = {rule.outcome for rule in self._all_rules()}
        unknown = sorted(named - set(self.outcomes))
        if unknown:
            raise ValueError(f"rules name outcomes not in `outcomes`: {unknown}")
        for name in self.profiles:
            if not re.match(_OUTCOME_PATTERN, name):
                raise ValueError(f"profile {name!r} must be lower_snake_case")
        return self

    def _all_rules(self) -> list[Rule]:
        return [*self.rules, *(rule for rules in self.profiles.values() for rule in rules)]

    def rules_for(self, profile: str | None) -> list[Rule]:
        if profile is None:
            return self.rules
        if profile not in self.profiles:
            raise KeyError(f"no profile {profile!r}; have {sorted(self.profiles)}")
        return self.profiles[profile]

    def problems(self, questions: Mapping[str, Question]) -> list[str]:
        """Where the rules and the gate's live questions disagree."""
        found: list[str] = []
        for index, rule in enumerate(self._all_rules()):
            question = questions.get(rule.question)
            if question is None:
                found.append(
                    f"rule {index} refers to question {rule.question!r}, which is not live"
                )
                continue
            if not rule.accepts(question.type):
                found.append(
                    f"rule {index} has conditions that do not apply to a "
                    f"{question.type} question ({rule.question!r})"
                )
                continue
            if question.type == "choice" and isinstance(question.criteria, dict):
                options = set(question.criteria)
                named = set(rule.choice_in or []) | ({rule.option} if rule.option else set())
                missing = sorted(named - options)
                if missing:
                    found.append(f"rule {index} names options {missing} not in {rule.question!r}")
        return found

    def decide(
        self, answers: Mapping[str, AnswerValue], profile: str | None = None
    ) -> tuple[str, list[Reason]]:
        severity = {outcome: index for index, outcome in enumerate(self.outcomes)}
        reasons = [
            Reason(rule.question, rule.outcome, rule.describe(answers[rule.question]))
            for rule in self.rules_for(profile)
            if rule.question in answers and rule.matches(answers[rule.question])
        ]
        if not reasons:
            return self.outcomes[0], []
        reasons.sort(key=lambda reason: severity[reason.outcome], reverse=True)
        return reasons[0].outcome, reasons

    def failure_outcome(self, fail_mode: FailMode) -> str:
        return self.outcomes[0] if fail_mode == "open" else self.outcomes[-1]
