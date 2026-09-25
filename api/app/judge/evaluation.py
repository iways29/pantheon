"""Measuring a gate against labelled cases (Step 5.4). Pure functions.

Everything here works on answers already received, so trying a different
threshold costs no model calls: the stored answers are decided again under
the changed policy. That is what makes "set thresholds from data" cheap.

Metrics, per gate:

- accuracy and, per outcome, precision, recall and F1 (one outcome against
  the rest), plus "flagged": anything other than the least severe outcome,
  which for most gates is "the gate caught something";
- accuracy by confidence band, to see whether confident answers are right
  more often (confidence is a signal, never permission);
- self-consistency across repeated runs of the same case;
- accuracy on the known weak spots (arithmetic, dates, double negatives,
  adversarial text);
- a sweep of every numeric threshold, one at a time with the others held,
  and the value that maximises macro F1.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.gateway.systemone import ChoiceAnswer, NoulAnswer, ScoreAnswer
from app.judge.judge import Decision
from app.judge.policy import Policy, Rule

_PROBABILITY_FIELDS = (
    "noul_at_least",
    "noul_below",
    "probability_at_least",
    "probability_below",
    "confidence_at_least",
    "confidence_below",
)
_SCORE_FIELDS = ("score_at_least", "score_below")
PROBABILITY_GRID = tuple(round(0.05 * i, 2) for i in range(1, 20))
#: A sweep must beat the current value by this much macro F1 to be
#: recommended; smaller gains on a small set are noise.
MIN_GAIN = 0.02


@dataclass(frozen=True)
class CaseResult:
    case_key: str
    expected: str
    runs: tuple[Decision, ...]
    weak_spot: str | None = None

    @property
    def first(self) -> Decision:
        return self.runs[0]

    @property
    def failed(self) -> bool:
        return self.first.failed

    @property
    def predicted(self) -> str:
        return self.first.outcome


@dataclass
class Report:
    gate: str
    gate_version: int
    model: str | None
    profile: str | None
    cases: int
    repeats: int
    failures: int
    classification: dict[str, Any]
    confidence: list[dict[str, Any]]
    consistency: dict[str, Any]
    weak_spots: dict[str, Any]
    sweeps: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    tokens_in: int = 0
    cost_usd: float = 0.0
    latency_ms_p50: int | None = None
    latency_ms_p95: int | None = None
    wrong: list[dict[str, Any]] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "classification": self.classification,
            "confidence": self.confidence,
            "consistency": self.consistency,
            "weak_spots": self.weak_spots,
            "sweeps": self.sweeps,
            "recommendations": self.recommendations,
            "wrong": self.wrong,
            "cost_per_1000_judgments_usd": self.cost_per_1000(),
        }

    def cost_per_1000(self) -> float:
        calls = self.cases * self.repeats - self.failures
        return round(self.cost_usd / calls * 1000, 6) if calls else 0.0


def classify(
    expected: Sequence[str], predicted: Sequence[str], outcomes: Sequence[str]
) -> dict[str, Any]:
    """Accuracy, per-outcome precision/recall/F1, macro F1, and `flagged`."""
    pairs = list(zip(expected, predicted, strict=True))
    per: dict[str, dict[str, float | int]] = {}
    for outcome in outcomes:
        tp = sum(1 for e, p in pairs if e == outcome and p == outcome)
        fp = sum(1 for e, p in pairs if e != outcome and p == outcome)
        fn = sum(1 for e, p in pairs if e == outcome and p != outcome)
        per[outcome] = {**_prf(tp, fp, fn), "support": tp + fn}
    present = [o for o in outcomes if per[o]["support"] or per[o]["precision"] is not None]
    f1s = [per[o]["f1"] or 0.0 for o in present]
    quiet = outcomes[0]
    flagged = _prf(
        sum(1 for e, p in pairs if e != quiet and p != quiet),
        sum(1 for e, p in pairs if e == quiet and p != quiet),
        sum(1 for e, p in pairs if e != quiet and p == quiet),
    )
    return {
        "accuracy": round(sum(e == p for e, p in pairs) / len(pairs), 4) if pairs else None,
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else None,
        "per_outcome": per,
        "flagged": flagged,
    }


def _prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = 0.0 if (precision is not None or recall is not None) else None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": None if precision is None else round(precision, 4),
        "recall": None if recall is None else round(recall, 4),
        "f1": None if f1 is None else round(f1, 4),
    }


def certainty(decision: Decision) -> float | None:
    """The least certain answer in a decision, on a 0-1 scale.

    Choice and Score carry TypeSafe's own confidence. A Noul has none, so its
    distance from 0.5 stands in (0.5 is a coin flip, 0 or 1 is decided).
    """
    values: list[float] = []
    for answer in decision.answers.values():
        if isinstance(answer, ChoiceAnswer | ScoreAnswer):
            values.append(answer.confidence)
        elif isinstance(answer, NoulAnswer):
            values.append(abs(answer.noul - 0.5) * 2)
    return min(values) if values else None


def confidence_bands(results: Sequence[CaseResult]) -> list[dict[str, Any]]:
    bands = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
    rows = []
    for low, high in bands:
        inside = [
            r
            for r in results
            if not r.failed and (c := certainty(r.first)) is not None and low <= c < high
        ]
        rows.append(
            {
                "band": f"{low:.1f}-{min(high, 1.0):.1f}",
                "cases": len(inside),
                "accuracy": round(sum(r.predicted == r.expected for r in inside) / len(inside), 4)
                if inside
                else None,
            }
        )
    return rows


def consistency(results: Sequence[CaseResult]) -> dict[str, Any]:
    repeated = [r for r in results if len(r.runs) > 1 and not any(d.failed for d in r.runs)]
    if not repeated:
        return {"cases": 0, "same_outcome": None, "max_noul_spread": None}
    same = sum(len({d.outcome for d in r.runs}) == 1 for r in repeated)
    spread = 0.0
    for r in repeated:
        for key in r.first.answers:
            values = [
                d.answers[key].noul for d in r.runs if isinstance(d.answers.get(key), NoulAnswer)
            ]
            if len(values) > 1:
                spread = max(spread, max(values) - min(values))
    return {
        "cases": len(repeated),
        "same_outcome": round(same / len(repeated), 4),
        "max_noul_spread": round(spread, 4),
    }


def weak_spots(results: Sequence[CaseResult]) -> dict[str, Any]:
    found: dict[str, list[CaseResult]] = {}
    for r in results:
        if r.weak_spot and not r.failed:
            found.setdefault(r.weak_spot, []).append(r)
    return {
        spot: {
            "cases": len(rs),
            "accuracy": round(sum(r.predicted == r.expected for r in rs) / len(rs), 4),
        }
        for spot, rs in sorted(found.items())
    }


def sweep(
    policy: Policy, results: Sequence[CaseResult], profile: str | None = None
) -> list[dict[str, Any]]:
    """Every numeric threshold, varied alone, scored on the stored answers."""
    usable = [r for r in results if not r.failed]
    if not usable:
        return []
    expected = [r.expected for r in usable]
    rules = policy.rules_for(profile)
    out = []
    for index, rule in enumerate(rules):
        for name in (*_PROBABILITY_FIELDS, *_SCORE_FIELDS):
            current = getattr(rule, name)
            if current is None:
                continue
            grid = PROBABILITY_GRID if name in _PROBABILITY_FIELDS else _score_grid(usable, rule)
            values = []
            for value in sorted({*grid, current}):
                changed = _with_threshold(policy, profile, index, name, value)
                if changed is None:
                    continue
                predicted = [changed.decide(r.first.answers, profile)[0] for r in usable]
                scores = classify(expected, predicted, policy.outcomes)
                values.append(
                    {
                        "value": value,
                        "accuracy": scores["accuracy"],
                        "macro_f1": scores["macro_f1"],
                        "flagged_precision": scores["flagged"]["precision"],
                        "flagged_recall": scores["flagged"]["recall"],
                    }
                )
            at_current = next(v for v in values if v["value"] == current)
            best = max(
                values,
                key=lambda v: (v["macro_f1"] or 0.0, -abs(v["value"] - current)),
            )
            out.append(
                {
                    "rule": index,
                    "question": rule.question,
                    "outcome": rule.outcome,
                    "field": name,
                    "current": current,
                    "current_macro_f1": at_current["macro_f1"],
                    "best": best["value"],
                    "best_macro_f1": best["macro_f1"],
                    "values": values,
                }
            )
    return out


def recommendations(sweeps: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rule": s["rule"],
            "question": s["question"],
            "outcome": s["outcome"],
            "field": s["field"],
            "from": s["current"],
            "to": s["best"],
            "macro_f1": [s["current_macro_f1"], s["best_macro_f1"]],
        }
        for s in sweeps
        if (s["best_macro_f1"] or 0) - (s["current_macro_f1"] or 0) >= MIN_GAIN
    ]


def _score_grid(results: Sequence[CaseResult], rule: Rule) -> tuple[float, ...]:
    levels = 0
    for r in results:
        answer = r.first.answers.get(rule.question)
        if isinstance(answer, ScoreAnswer):
            levels = max(levels, len(answer.legend))
    top = max(levels - 1, 1)
    return tuple(round(0.25 * i, 2) for i in range(0, top * 4 + 1))


def _with_threshold(
    policy: Policy, profile: str | None, index: int, name: str, value: float
) -> Policy | None:
    rules = [rule.model_dump(exclude_none=True) for rule in policy.rules_for(profile)]
    rules[index][name] = value
    data = policy.model_dump(exclude_none=True)
    if profile is None:
        data["rules"] = rules
    else:
        data["profiles"] = {**data["profiles"], profile: rules}
    try:
        return Policy.model_validate(data)
    except ValueError:
        return None


def latency_percentiles(results: Sequence[CaseResult]) -> tuple[int | None, int | None]:
    latencies = sorted(
        d.latency_ms for r in results for d in r.runs if d.latency_ms is not None and not d.failed
    )
    if not latencies:
        return None, None
    p50 = int(statistics.median(latencies))
    p95 = latencies[min(len(latencies) - 1, round(0.95 * (len(latencies) - 1)))]
    return p50, p95


def build_report(
    *,
    gate: str,
    gate_version: int,
    policy: Policy,
    profile: str | None,
    results: Sequence[CaseResult],
    repeats: int,
    tokens_in: int,
) -> Report:
    usable = [r for r in results if not r.failed]
    swept = sweep(policy, results, profile)
    p50, p95 = latency_percentiles(results)
    return Report(
        gate=gate,
        gate_version=gate_version,
        model=next((r.first.model for r in usable if r.first.model), None),
        profile=profile,
        cases=len(results),
        repeats=repeats,
        failures=sum(d.failed for r in results for d in r.runs),
        classification=classify(
            [r.expected for r in usable], [r.predicted for r in usable], policy.outcomes
        ),
        confidence=confidence_bands(results),
        consistency=consistency(results),
        weak_spots=weak_spots(results),
        sweeps=swept,
        recommendations=recommendations(swept),
        tokens_in=tokens_in,
        cost_usd=round(sum(d.cost_usd for r in results for d in r.runs), 8),
        latency_ms_p50=p50,
        latency_ms_p95=p95,
        wrong=[
            {
                "case": r.case_key,
                "expected": r.expected,
                "got": r.predicted,
                "reasons": [x.text for x in r.first.reasons],
            }
            for r in usable
            if r.predicted != r.expected
        ],
    )
