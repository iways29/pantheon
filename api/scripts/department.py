"""Departments from the command line: charters, apply, switch on, report (Step 8).

    cd api
    uv run python -m scripts.department seed                  # starting charters, if missing
    uv run python -m scripts.department show research         # the live charter, as JSON
    uv run python -m scripts.department publish research charter.json [--note "why"]
    uv run python -m scripts.department publish executive --starter   # the version in code
    uv run python -m scripts.department sources research --topic "AI agents" \
        --source https://example.com/news [--source ...] [--time 06:30] [--days mon-sat]
    uv run python -m scripts.department apply research        # make agents and routine match
    uv run python -m scripts.department enable research       # switch agents and routine on
    uv run python -m scripts.department disable research
    uv run python -m scripts.department report research [--days 5]

A charter is data (ADR 024): editing it never needs a code change. `apply`
creates what is missing switched off; `enable` is the owner's go.
"""

import argparse
import json
import sys
from pathlib import Path

from app.agents.triggers import parse_days
from app.db import acting_as, connect
from app.departments.apply import apply_charter, enable
from app.departments.charter import Charter, load, publish, seed_charters, to_json
from app.departments.report import department_report
from app.departments.starter_charters import STARTER_CHARTERS
from scripts.agent import _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed")
    for name in ("show", "apply", "enable", "disable"):
        commands.add_parser(name).add_argument("department")
    pub = commands.add_parser("publish")
    pub.add_argument("department")
    pub.add_argument("path", type=Path, nargs="?")
    pub.add_argument("--starter", action="store_true", help="publish the starting charter")
    pub.add_argument("--note")
    src = commands.add_parser("sources", help="set the morning routine's topics and sources")
    src.add_argument("department")
    src.add_argument("--topic", action="append", default=[])
    src.add_argument("--source", action="append", default=[])
    src.add_argument("--time", help="local time, e.g. 06:30")
    src.add_argument("--days", help='e.g. "mon-sat", "weekdays", "mon,wed,fri"')
    rep = commands.add_parser("report")
    rep.add_argument("department")
    rep.add_argument("--days", type=int, default=5)
    args = parser.parse_args(argv)

    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.command == "seed":
            print(
                seed_charters(connection, user_id=user_id, org_id=org_id, charters=STARTER_CHARTERS)
                or "nothing new"
            )
        elif args.command == "show":
            with acting_as(connection, user_id=user_id) as owner:
                version, charter = load(owner, org_id=org_id, department=args.department)
            print(f"# version {version}")
            print(to_json(charter))
        elif args.command == "publish":
            if args.starter:
                charter = STARTER_CHARTERS[args.department]
            elif args.path is not None:
                charter = Charter.model_validate_json(args.path.read_text())
            else:
                parser.error("publish needs a charter file or --starter")
            print(
                publish(
                    connection,
                    user_id=user_id,
                    org_id=org_id,
                    department=args.department,
                    charter=charter,
                    note=args.note,
                )
            )
        elif args.command == "sources":
            with acting_as(connection, user_id=user_id) as owner:
                _, charter = load(owner, org_id=org_id, department=args.department)
            if not charter.routine:
                raise SystemExit("This charter has no morning routine")
            first = charter.routine[0]
            item = first.model_copy(
                update={
                    # Only what was passed changes; the rest of the input stays.
                    "input": {
                        **first.input,
                        **({"topics": args.topic} if args.topic else {}),
                        **({"sources": args.source} if args.source else {}),
                    },
                    **({"time": args.time} if args.time else {}),
                    **({"days": parse_days(args.days)} if args.days else {}),
                }
            )
            changed = charter.model_copy(update={"routine": [item, *charter.routine[1:]]})
            version = publish(
                connection,
                user_id=user_id,
                org_id=org_id,
                department=args.department,
                charter=Charter.model_validate(changed.model_dump()),
                note="Topics and sources",
            )
            print(f"version {version}; run `apply {args.department}` to use it")
        elif args.command == "apply":
            report = apply_charter(
                connection, user_id=user_id, org_id=org_id, department=args.department
            )
            print(json.dumps(report.summary(), indent=2))
        elif args.command in ("enable", "disable"):
            print(
                enable(
                    connection,
                    user_id=user_id,
                    org_id=org_id,
                    department=args.department,
                    on=args.command == "enable",
                )
                or "nothing to change"
            )
        else:
            print(
                json.dumps(
                    department_report(
                        connection,
                        user_id=user_id,
                        org_id=org_id,
                        department=args.department,
                        days=args.days,
                    ),
                    indent=2,
                    default=str,
                )
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
