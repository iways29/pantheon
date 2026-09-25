"""The owner's decision desk from the command line (Step 7.5, ADR 021).

    cd api
    uv run python -m scripts.approvals list
    uv run python -m scripts.approvals approve <id> [--edit '{"text": "..."}'] [--note "..."]
    uv run python -m scripts.approvals reject <id> [--redirect] --note "why"
    uv run python -m scripts.approvals agreement
    uv run python -m scripts.approvals rule "Newsletters are always in English." [--as writer]

A decision with a note, and every rule, is written to the brain as the
owner's (through the write gate), so later proposals are checked against it.
The scheduler resumes an approved or redirected run on its next tick.
"""

import argparse
import json
import sys

from app.approvals import decide, decision_statement, list_pending, remember
from app.config import Settings
from app.db import acting_as, as_service_role, connect
from app.knowledge.wiring import writer_from
from scripts.agent import ROOT_ENV, _Env, _setup


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    approve = commands.add_parser("approve")
    approve.add_argument("approval_id")
    approve.add_argument("--edit", help="replacement arguments, as JSON")
    approve.add_argument("--note")
    reject = commands.add_parser("reject")
    reject.add_argument("approval_id")
    reject.add_argument("--redirect", action="store_true", help="the agent carries on without it")
    reject.add_argument("--note")
    commands.add_parser("agreement")
    rule = commands.add_parser("rule")
    rule.add_argument("statement")
    rule.add_argument("--as", dest="as_agent", default="researcher")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    with connect(_Env().database_url) as connection:  # type: ignore[call-arg]
        org_id, user_id, _ = _setup(connection)
        if args.command == "list":
            for row in list_pending(connection, user_id=user_id):
                print(f"{row['id']}  {row['action_key']}  by {row['agent']}")
                print(f"    arguments       {json.dumps(row['payload'].get('arguments'))}")
                print(f"    recommendation  {row['recommendation']} {row['recommendation_probs']}")
                if row["explanation"]:
                    print(f"    explanation     {row['explanation']}")
                for conflict in row["conflicts"]:
                    print(f"    CONFLICT        {conflict['claim']}")
            return 0
        if args.command == "agreement":
            with acting_as(connection, user_id=user_id) as conn:
                for row in conn.execute(
                    "select * from public.approval_agreement order by action_key"
                ).fetchall():
                    print(
                        f"{row['action_key']:<30} decided {row['decided']:>4}  "
                        f"agreed {row['agreed']:>4}/{row['recommended']:<4} "
                        f"({row['agreement'] or 0:.0%})"
                    )
            return 0
        if args.command == "rule":
            with as_service_role(connection) as conn:
                agent_id = conn.execute(
                    "select id from public.agents where org_id = %s and name = %s",
                    (org_id, args.as_agent),
                ).fetchone()["id"]
            with acting_as(connection, user_id=user_id) as conn:
                result = remember(
                    writer_from(conn, settings, agent_id=agent_id),
                    org_id=org_id,
                    agent_id=agent_id,
                    statement=args.statement,
                )
            print(f"{result.outcome}: {'; '.join(result.reasons) or 'written'}")
            return 0

        decision = (
            "approve" if args.command == "approve" else ("redirect" if args.redirect else "cancel")
        )
        row = decide(
            connection,
            user_id=user_id,
            approval_id=args.approval_id,
            decision=decision,
            note=args.note,
            edited_arguments=json.loads(args.edit) if getattr(args, "edit", None) else None,
        )
        print(f"{row['id']}  {row['status']}")
        if args.note and row.get("agent_id"):
            with acting_as(connection, user_id=user_id) as conn:
                result = remember(
                    writer_from(conn, settings, agent_id=row["agent_id"]),
                    org_id=org_id,
                    agent_id=row["agent_id"],
                    statement=decision_statement(row, decision, args.note),
                    ref=f"approval:{row['id']}",
                )
            print(f"remembered: {result.outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
