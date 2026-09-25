"""Labelled cases and eval runs for the judge gates (Step 5.4, ADR 012).

Cases live in `judge_cases`, imported from files under `api/evals/cases/`
or, later, from the owner's approval decisions. `run_eval` asks a gate's
live questions about every active case (optionally several times, for
self-consistency), measures the result with `evaluation.build_report`, and
records it in `judge_eval_runs`.

Eval calls are real TypeSafe calls through the gateway, costed to the agent
they run as, and logged in `judgments` with an `eval:` input reference.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from app.db import acting_as
from app.judge.evaluation import CaseResult, Report, build_report
from app.judge.judge import Judge
from app.judge.store import load_gate

LABEL_SOURCES = ("owner", "approval", "ensemble", "author")


@dataclass(frozen=True)
class Case:
    case_key: str
    state: Any
    expected: str
    label_source: str
    weak_spot: str | None = None
    note: str | None = None


def import_cases(
    connection: psycopg.Connection,
    *,
    user_id: UUID | str,
    org_id: UUID | str,
    path: Path,
) -> tuple[str, int]:
    """Load a case file into `judge_cases`, updating cases with the same key.

    The file names its gate; every expected outcome must be one of that
    gate's live outcomes, so a typo cannot quietly become a class of its own.
    """
    data = json.loads(path.read_text())
    gate = data["gate"]
    with acting_as(connection, user_id=str(user_id)) as conn, conn.cursor() as cursor:
        outcomes = load_gate(conn, org_id=org_id, gate=gate).policy.outcomes
        for raw in data["cases"]:
            if raw["expected"] not in outcomes:
                raise ValueError(
                    f"case {raw['id']}: {raw['expected']!r} is not an outcome of {gate} {outcomes}"
                )
            if raw.get("label_source", "author") not in LABEL_SOURCES:
                raise ValueError(f"case {raw['id']}: unknown label_source")
            cursor.execute(
                """
                insert into public.judge_cases
                    (org_id, gate, case_key, state, expected, label_source, weak_spot, note)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (org_id, gate, case_key) do update set
                    state = excluded.state,
                    expected = excluded.expected,
                    label_source = excluded.label_source,
                    weak_spot = excluded.weak_spot,
                    note = excluded.note,
                    active = true
                """,
                (
                    str(org_id),
                    gate,
                    raw["id"],
                    json.dumps(raw["state"]),
                    raw["expected"],
                    raw.get("label_source", "author"),
                    raw.get("weak_spot"),
                    raw.get("note"),
                ),
            )
    return gate, len(data["cases"])


def load_cases(connection: psycopg.Connection, *, org_id: UUID | str, gate: str) -> list[Case]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select case_key, state, expected, label_source, weak_spot, note
            from public.judge_cases
            where org_id = %s and gate = %s and active
            order by case_key
            """,
            (str(org_id), gate),
        )
        return [Case(**row) for row in cursor.fetchall()]


def run_eval(
    connection: psycopg.Connection,
    judge: Judge,
    *,
    org_id: UUID | str,
    agent_id: UUID | str,
    gate: str,
    repeats: int = 1,
    profile: str | None = None,
    limit: int | None = None,
) -> Report:
    """Judge every active case `repeats` times and measure the gate.

    Runs on the caller's connection and identity; commit is the caller's.
    """
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    config = load_gate(connection, org_id=org_id, gate=gate)
    cases = load_cases(connection, org_id=org_id, gate=gate)[:limit]
    results = []
    for case in cases:
        runs = tuple(
            judge.run(
                gate,
                case.state,
                agent_id=agent_id,
                input_ref=f"eval:{gate}:{case.case_key}:{attempt}",
                profile=profile,
            )
            for attempt in range(repeats)
        )
        results.append(CaseResult(case.case_key, case.expected, runs, case.weak_spot))

    report = build_report(
        gate=gate,
        gate_version=config.version,
        policy=config.policy,
        profile=profile,
        results=results,
        repeats=repeats,
        tokens_in=sum(d.tokens_in for r in results for d in r.runs),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into public.judge_eval_runs
                (org_id, gate, gate_version, model, profile, cases, repeats, metrics,
                 tokens_in, cost_usd, latency_ms_p50, latency_ms_p95)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(org_id),
                gate,
                config.version,
                report.model or config.config.model,
                profile,
                report.cases,
                repeats,
                json.dumps(report.metrics()),
                report.tokens_in,
                report.cost_usd,
                report.latency_ms_p50,
                report.latency_ms_p95,
            ),
        )
    return report
