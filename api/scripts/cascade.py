"""How Jev's checks of the workers' results are doing (Step 9, ADR 031).

    cd api
    uv run python -m scripts.cascade report [--days 7]

Each worker's finished task is checked by Jev (`result_check`); a failed check
redoes the task once on a stronger tier. This reports how often results
passed, were redone or stayed flagged, what the redos and checks cost, and an
estimate of what always using the stronger tier would have cost. The gate's
questions, thresholds and settings: `python -m scripts.judge`.
"""

import argparse
import json
import sys

from app.agents.review import summary
from app.db import connect
from scripts.agent import _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        print(
            json.dumps(
                summary(connection, user_id=user_id, org_id=org_id, days=args.days), indent=2
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
