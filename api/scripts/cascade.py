"""How Jev's checks of the workers' results are doing (Step 9, ADR 031 and 032).

    cd api
    uv run python -m scripts.cascade report [--days 7]
    uv run python -m scripts.cascade compare [--cases evals/cascade/scouting.json]
        [--save ../docs/reports/step9-comparison.md]

`report`: each worker's finished task is checked by Jev (`result_check`); a
failed check redoes the task once on a stronger tier. How often results
passed, were redone or stayed flagged, what the redos and checks cost, and an
estimate of always using the stronger tier.

`compare`: the Step 9 test. Every labelled case is answered on the cheap and
the standard tier from the same pages; the cascade is the cheap answer
checked by Jev, redone on standard when the check fails. Quality is scored
in code against each case's labels. Spends real model credit (about 3 cents
for the 8 starting cases), under its own `benchmarks` department and budget.
"""

import argparse
import json
import sys
from pathlib import Path

from app.agents.benchmark import compare, ensure_agent, load_cases
from app.agents.review import summary
from app.config import Settings
from app.db import connect
from app.gateway import gateway_from
from scripts.agent import ROOT_ENV, _Env, _setup

CASES = Path(__file__).resolve().parents[1] / "evals" / "cascade" / "scouting.json"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("--days", type=int, default=7)
    side_by_side = commands.add_parser("compare")
    side_by_side.add_argument("--cases", type=Path, default=CASES)
    side_by_side.add_argument("--save", type=Path, help="also write the report here")
    args = parser.parse_args(argv)

    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.command == "report":
            print(
                json.dumps(
                    summary(connection, user_id=user_id, org_id=org_id, days=args.days), indent=2
                )
            )
            return 0
        settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
        today, task, cases = load_cases(args.cases)
        agent_id = ensure_agent(connection, user_id=user_id, org_id=org_id)
        result = compare(
            connection,
            lambda conn: gateway_from(conn, settings),
            user_id=user_id,
            org_id=org_id,
            agent_id=agent_id,
            today=today,
            task=task,
            cases=cases,
        )
        text = result.markdown()
        print(text)
        if args.save:
            args.save.parent.mkdir(parents=True, exist_ok=True)
            args.save.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
