"""Measure the judge gates against labelled cases (Step 5.4).

    cd api
    uv run python -m scripts.judge_eval import                 # every file in evals/cases/
    uv run python -m scripts.judge_eval import evals/cases/brain_claim.json
    uv run python -m scripts.judge_eval run brain_claim [--repeats 3] [--profile strict]
    uv run python -m scripts.judge_eval run all [--repeats 3]
    uv run python -m scripts.judge_eval history brain_claim

`run` makes real TypeSafe calls through the gateway as the `researcher`
agent: one per case per repeat. The starting sets hold 42 cases, so
`run all --repeats 3` is 126 calls, about 50,000 input tokens: roughly
$0.002. It prints accuracy, precision and recall per outcome, accuracy by
confidence, self-consistency, the weak spots, the cases it got wrong, cost
per 1,000 judgments, latency, and threshold changes the data supports. Each
run is saved in `judge_eval_runs`. Thresholds are changed by the owner with
`scripts.judge gate set`, never by this script.
"""

import argparse
import sys
from pathlib import Path

from app.config import Settings
from app.db import acting_as, as_service_role, connect
from app.judge import judge_from
from app.judge.calibration import import_cases, run_eval
from app.judge.evaluation import Report
from scripts.agent import ROOT_ENV, _Env, _setup

CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "cases"
EVAL_GATES = ("brain_claim", "brain_neighbour", "content_screen")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("import", help="load case files into judge_cases")
    load.add_argument("files", nargs="*", type=Path)
    run = commands.add_parser("run", help="run a gate (or `all`) over its cases")
    run.add_argument("gate")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--profile")
    run.add_argument("--limit", type=int)
    history = commands.add_parser("history", help="earlier eval runs of a gate")
    history.add_argument("gate")
    args = parser.parse_args(argv)

    dsn = _Env().database_url  # type: ignore[call-arg]
    with connect(dsn) as connection:
        org_id, user_id, agent_id = _setup(connection)
        if args.command == "import":
            files = args.files or sorted(CASES_DIR.glob("*.json"))
            for path in files:
                gate, count = import_cases(connection, user_id=user_id, org_id=org_id, path=path)
                print(f"{gate}: {count} cases from {path.name}")
            return 0
        if args.command == "history":
            _history(connection, args.gate)
            return 0

        settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
        gates = EVAL_GATES if args.gate == "all" else (args.gate,)
        for gate in gates:
            with acting_as(connection, user_id=user_id) as conn:
                report = run_eval(
                    conn,
                    judge_from(conn, settings),
                    org_id=org_id,
                    agent_id=agent_id,
                    gate=gate,
                    repeats=args.repeats,
                    profile=args.profile,
                    limit=args.limit,
                )
            print_report(report)
    return 0


def print_report(r: Report) -> None:
    c = r.classification
    print(f"\n=== {r.gate} v{r.gate_version}  model {r.model}  profile {r.profile or 'default'}")
    print(f"cases {r.cases} x {r.repeats}   failed calls {r.failures}")
    print(f"accuracy {_pct(c['accuracy'])}   macro F1 {_num(c['macro_f1'])}")
    flagged = c["flagged"]
    print(
        f"flagged (anything but the least severe outcome): precision "
        f"{_pct(flagged['precision'])}  recall {_pct(flagged['recall'])}"
    )
    print("  outcome          precision  recall   f1     support")
    for outcome, m in c["per_outcome"].items():
        print(
            f"  {outcome:<16} {_pct(m['precision']):>9}  {_pct(m['recall']):>6}  "
            f"{_num(m['f1']):>5}  {m['support']:>7}"
        )
    print("accuracy by certainty of the least certain answer:")
    for band in r.confidence:
        print(f"  {band['band']}  {band['cases']:>3} cases  {_pct(band['accuracy'])}")
    s = r.consistency
    if s["cases"]:
        print(
            f"self-consistency over {r.repeats} runs: same outcome {_pct(s['same_outcome'])}, "
            f"largest Noul spread {s['max_noul_spread']}"
        )
    for spot, m in r.weak_spots.items():
        print(f"weak spot {spot:<16} {m['cases']} cases  accuracy {_pct(m['accuracy'])}")
    for w in r.wrong:
        print(f"WRONG {w['case']}: expected {w['expected']}, got {w['got']}  {w['reasons'][:2]}")
    print(
        f"cost ${r.cost_usd:.6f} for {r.tokens_in} input tokens; "
        f"${r.cost_per_1000():.4f} per 1,000 judgments; "
        f"latency p50 {r.latency_ms_p50} ms, p95 {r.latency_ms_p95} ms"
    )
    if r.recommendations:
        print("thresholds the data supports (one at a time, others held):")
        for rec in r.recommendations:
            print(
                f"  rule {rec['rule']} {rec['question']} -> {rec['outcome']}: {rec['field']} "
                f"{rec['from']} -> {rec['to']}  (macro F1 {rec['macro_f1'][0]} -> "
                f"{rec['macro_f1'][1]})"
            )
    else:
        print("no threshold change beats the current values by enough to recommend")


def _history(connection: object, gate: str) -> None:
    with as_service_role(connection) as conn, conn.cursor() as cursor:  # type: ignore[arg-type]
        cursor.execute(
            """
            select created_at, gate_version, model, profile, cases, repeats, cost_usd,
                   metrics->'classification'->>'accuracy' as accuracy,
                   metrics->'classification'->>'macro_f1' as macro_f1
            from public.judge_eval_runs where gate = %s order by created_at desc limit 20
            """,
            (gate,),
        )
        for row in cursor.fetchall():
            print(
                f"{row['created_at']:%Y-%m-%d %H:%M}  v{row['gate_version']} {row['model']} "
                f"{row['profile'] or 'default'}  {row['cases']}x{row['repeats']}  "
                f"accuracy {row['accuracy']}  macro F1 {row['macro_f1']}  ${row['cost_usd']}"
            )


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
