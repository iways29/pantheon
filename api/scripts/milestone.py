"""Gather the Step 3 Milestone evidence against real OpenRouter.

Build plan: "Show cost per run for the first agent, a budget-block demo, a
kill-switch demo, and the projected monthly cost at a few run volumes."

    cd api && uv run python -m scripts.milestone [--runs 8]

Needs a seeded database (`python -m scripts.agent seed ...`) and the gateway
settings in .env. Everything it does is real: real model calls, real
checkpoints, a real process killed with SIGKILL mid-run. Total spend is well
under a cent at the cheap tier's current prices.

Prints a JSON summary at the end for the report.
"""

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from app.agents.runs import RunReport, Runtime, advance_run, start_run
from app.brain.embeddings import HashingEmbedder
from app.config import Settings
from app.db import as_service_role, connect
from app.gateway.factory import tier_map_from, transport_from
from app.tracing import tracer_from
from scripts.agent import ROOT_ENV, _Env, _setup

QUESTIONS = [
    "What causes the seasons on Earth?",
    "How does a vaccine train the immune system?",
    "Why did the Roman Empire split into east and west?",
    "What is the difference between TCP and UDP?",
    "How do central banks use interest rates to control inflation?",
    "What makes a prime number useful in cryptography?",
    "Why is the sky blue during the day but red at sunset?",
    "How does photosynthesis convert light into chemical energy?",
    "What is a Postgres index and when does it help?",
    "Why do airplanes fly along curved routes on flat maps?",
]
#: Fixed platform costs per month, list prices checked 2026-09-21.
PLATFORM_USD_PER_MONTH = {"vercel_pro": 20.0, "supabase_pro": 25.0, "langfuse_hobby": 0.0}
VOLUMES_PER_DAY = [10, 100, 1_000, 10_000]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=8)
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    dsn = _Env().database_url
    runtime = Runtime(
        dsn=dsn,
        transport=transport_from(settings),
        tiers=tier_map_from(settings),
        embedder=HashingEmbedder(),
        tracer=tracer_from(settings),
    )
    with connect(dsn) as connection:
        org_id, user_id, agent_id = _setup(connection)

    def run(question: str, **limits: int) -> tuple[uuid.UUID, RunReport, float]:
        with connect(dsn) as connection:
            run_id = start_run(
                connection,
                org_id=org_id,
                agent_id=agent_id,
                requested_by=user_id,
                question=question,
                idempotency_key=f"milestone-{uuid.uuid4()}",
                **limits,
            )
        started = time.monotonic()
        result = advance_run(runtime, run_id, deadline_seconds=240)
        return run_id, result, time.monotonic() - started

    summary: dict[str, Any] = {}

    print(f"== cost per run ({args.runs} real runs)")
    costs, tokens, seconds = [], [], []
    for question in QUESTIONS[: args.runs]:
        _, result, elapsed = run(question)
        if result.status != "succeeded":
            print(f"  {result.status} ({result.stop_reason}): {result.error}")
            continue
        costs.append(result.cost_usd)
        tokens.append(result.tokens_in + result.tokens_out)
        seconds.append(elapsed)
        print(f"  ${result.cost_usd:.6f}  {tokens[-1]:>5} tok  {elapsed:4.1f}s  {question}")
    mean = sum(costs) / len(costs)
    summary["cost_per_run"] = {
        "runs": len(costs),
        "mean_usd": float(mean),
        "median_usd": float(statistics.median(costs)),
        "max_usd": float(max(costs)),
        "mean_tokens": statistics.mean(tokens),
        "mean_seconds": round(statistics.mean(seconds), 2),
        "max_seconds": round(max(seconds), 2),
    }
    summary["projection_usd_per_month"] = {
        f"{per_day}/day": {
            "models": round(float(mean) * per_day * 30, 2),
            "total_with_platform": round(
                float(mean) * per_day * 30 + sum(PLATFORM_USD_PER_MONTH.values()), 2
            ),
        }
        for per_day in VOLUMES_PER_DAY
    }

    print("== budget block")
    with connect(dsn) as connection:
        department_id = _department_of(connection, agent_id)
        original = _set_budget(connection, department_id, Decimal("0.00001"))
    try:
        run_id, result, _ = run(QUESTIONS[0])
        calls = _count(
            dsn, "select count(*) as n from public.model_calls where run_id = %s", run_id
        )
        blocked = _count(
            dsn,
            "select count(*) as n from public.events where run_id = %s "
            "and type = 'run_paused' and payload->>'stop_reason' = 'budget_exceeded'",
            run_id,
        )
        print(f"  {result.status} ({result.stop_reason}) after {calls} call(s): {result.error}")
        summary["budget_block"] = {
            "status": result.status,
            "stop_reason": result.stop_reason,
            "calls_sent": calls,
            "spent_usd": float(result.cost_usd),
            "budget_usd": 0.00001,
            "paused_event": blocked == 1,
        }
    finally:
        with connect(dsn) as connection:
            _set_budget(connection, department_id, original)

    print("== kill switch")
    _kill_switch(dsn, org_id, on=True)
    try:
        run_id, result, _ = run(QUESTIONS[1])
        print(f"  on:  {result.status} ({result.stop_reason}), {result.steps_taken} steps")
        killed = (result.status, result.stop_reason, result.steps_taken, float(result.cost_usd))
    finally:
        _kill_switch(dsn, org_id, on=False)
    resumed = advance_run(runtime, run_id, deadline_seconds=240)
    print(f"  off: {resumed.status} after resume, {resumed.steps_taken} steps, ${resumed.cost_usd}")
    summary["kill_switch"] = {
        "while_on": dict(zip(("status", "stop_reason", "steps", "spent_usd"), killed, strict=True)),
        "after_off_and_resume": resumed.status,
    }

    print("== caps")
    _, capped_steps, _ = run(QUESTIONS[2], max_steps=2)
    _, capped_tokens, _ = run(QUESTIONS[3], max_tokens=300)
    for label, result in (("max_steps=2", capped_steps), ("max_tokens=300", capped_tokens)):
        print(
            f"  {label}: {result.status} ({result.stop_reason}), {result.steps_taken} steps, "
            f"{result.tokens_in + result.tokens_out} tokens"
        )
    summary["caps"] = {
        "max_steps_2": [capped_steps.status, capped_steps.stop_reason, capped_steps.steps_taken],
        "max_tokens_300": [
            capped_tokens.status,
            capped_tokens.stop_reason,
            capped_tokens.tokens_in + capped_tokens.tokens_out,
        ],
    }

    print("== killed mid-run with SIGKILL, then resumed")
    summary["kill_and_resume"] = _kill_and_resume(dsn, runtime, org_id, agent_id, user_id)
    print(f"  {summary['kill_and_resume']}")

    print(json.dumps(summary, indent=2, default=str))
    return 0


def _kill_and_resume(
    dsn: str, runtime: Runtime, org_id: str, agent_id: str, user_id: str
) -> dict[str, Any]:
    """Start a run in a child process, SIGKILL it after step 2, resume here."""
    with connect(dsn) as connection:
        run_id = start_run(
            connection,
            org_id=org_id,
            agent_id=agent_id,
            requested_by=user_id,
            question=QUESTIONS[4],
            idempotency_key=f"milestone-kill-{uuid.uuid4()}",
        )
    child = subprocess.Popen(
        [sys.executable, "-m", "scripts.milestone_child", str(run_id)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": dsn},
    )
    steps_at_kill = 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and child.poll() is None:
        steps_at_kill = _count(
            dsn, "select steps_taken as n from public.runs where id = %s", run_id
        )
        if steps_at_kill >= 2:
            child.send_signal(signal.SIGKILL)
            break
        time.sleep(0.02)
    child.wait()
    with connect(dsn) as connection, as_service_role(connection) as conn:
        state = conn.execute(
            "select status, lease_expires_at > now() as leased from public.runs where id = %s",
            (str(run_id),),
        ).fetchone()
        # Stands in for waiting out the dead invocation's lease (300s).
        conn.execute(
            "update public.runs set lease_expires_at = now() - interval '1 second' where id = %s",
            (str(run_id),),
        )
    result = advance_run(runtime, run_id, deadline_seconds=240)
    answer_calls = _count(
        dsn,
        "select count(*) as n from public.events where run_id = %s and type = 'run_step' "
        "and payload->>'node' = 'answer'",
        run_id,
    )
    return {
        "killed_after_steps": steps_at_kill,
        "exit_signal": -child.returncode if child.returncode and child.returncode < 0 else None,
        "status_after_kill": state["status"],
        "lease_held_after_kill": state["leased"],
        "status_after_resume": result.status,
        "steps_taken": result.steps_taken,
        "answer_step_ran": answer_calls,
        "model_calls": _count(
            dsn, "select count(*) as n from public.model_calls where run_id = %s", run_id
        ),
        "cost_usd": float(result.cost_usd),
    }


def _department_of(connection: psycopg.Connection, agent_id: str) -> str:
    with as_service_role(connection) as conn:
        return str(
            conn.execute(
                "select department_id from public.agents where id = %s", (agent_id,)
            ).fetchone()["department_id"]
        )


def _set_budget(connection: psycopg.Connection, department_id: str, budget: Decimal) -> Decimal:
    with as_service_role(connection) as conn:
        previous = conn.execute(
            "select daily_budget_usd from public.departments where id = %s", (department_id,)
        ).fetchone()["daily_budget_usd"]
        conn.execute(
            "update public.departments set daily_budget_usd = %s where id = %s",
            (budget, department_id),
        )
    return Decimal(previous)


def _kill_switch(dsn: str, org_id: str, *, on: bool) -> None:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        conn.execute(
            "insert into public.system_flags (org_id, key, value) values (%s, 'kill_switch', %s) "
            "on conflict (org_id, key) do update set value = excluded.value",
            (org_id, json.dumps(on)),
        )


def _count(dsn: str, query: str, run_id: uuid.UUID) -> int:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        row = conn.execute(query, (str(run_id),)).fetchone()
    return int(row["n"] or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
