"""A scripted TypeSafe for tests: answers every question by its key.

By default it answers like a clean, well-supported, brand-new claim would: a
standalone fact, not opinion, no secrets, no instructions, supported by its
evidence, unlike any existing fact. A test overrides only what it is about,
either per key (`nouls`, `choices`, `scores`) or per call (`respond`).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.gateway import (
    ChoiceAnswer,
    NoulAnswer,
    Question,
    ScoreAnswer,
    SystemOneResponse,
    UpstreamError,
)

AnswerValue = NoulAnswer | ChoiceAnswer | ScoreAnswer

DEFAULT_NOULS = {
    "standalone": 0.97,
    "opinion": 0.02,
    "personal_or_secret": 0.01,
    "ai_instruction": 0.01,
    "evidence_instruction": 0.01,
    "volatile": 0.1,
    "contradicts": 0.02,
    "updates": 0.02,
}
DEFAULT_CHOICES = {"support": "supports"}
DEFAULT_SCORES = {"sameness": 0.1}


@dataclass
class ScriptedJev:
    nouls: dict[str, float] = field(default_factory=dict)
    choices: dict[str, str] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    #: Called with (state, questions); may return {key: answer} overrides.
    respond: Callable[[Any, dict[str, Question]], dict[str, AnswerValue]] | None = None
    failure: UpstreamError | None = None
    tokens_in: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def evaluate(
        self, *, model: str, state: Any, questions: dict[str, Question]
    ) -> SystemOneResponse:
        self.calls.append({"model": model, "state": state, "questions": questions})
        if self.failure is not None:
            raise self.failure
        overrides = self.respond(state, questions) if self.respond else {}
        answers = {
            key: overrides.get(key) or self._answer(key, question)
            for key, question in questions.items()
        }
        return SystemOneResponse(
            model=model, answers=answers, tokens_in=self.tokens_in, tokens_out=0, latency_ms=5
        )

    def calls_for(self, *keys: str) -> list[dict[str, Any]]:
        """The calls that asked a question with one of these keys."""
        return [c for c in self.calls if set(keys) & set(c["questions"])]

    def _answer(self, key: str, question: Question) -> AnswerValue:
        if question.type == "noul":
            return noul(self.nouls.get(key, DEFAULT_NOULS.get(key, 0.02)))
        if question.type == "choice":
            options = list(question.criteria or {})
            picked = self.choices.get(key, DEFAULT_CHOICES.get(key, options[0]))
            return choice(picked, options)
        levels = len(question.criteria or [])
        return score(self.scores.get(key, DEFAULT_SCORES.get(key, 0.0)), levels)


def noul(p: float) -> NoulAnswer:
    return NoulAnswer(type="noul", noul=p)


def choice(picked: str, options: list[str], p: float = 0.95) -> ChoiceAnswer:
    rest = (1 - p) / max(len(options) - 1, 1)
    return ChoiceAnswer(
        type="choice",
        choice=picked,
        probabilities={o: (p if o == picked else rest) for o in options},
        confidence=p,
    )


def score(value: float, levels: int = 3, confidence: float = 0.9) -> ScoreAnswer:
    nearest = min(max(round(value), 0), levels - 1)
    return ScoreAnswer(
        type="score",
        score=value,
        legend={str(i): f"level {i}" for i in range(levels)},
        probabilities={
            str(i): (0.9 if i == nearest else 0.1 / (levels - 1)) for i in range(levels)
        },
        confidence=confidence,
    )
