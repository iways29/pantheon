"""Manage and try TypeSafe judge gates from the command line.

The owner's way to change gates until the Control Center (Step 11) exists.
Every change publishes a new version, takes effect on the next judgment with
no deploy, and is written to `events`.

    cd api
    uv run python -m scripts.judge gate list
    uv run python -m scripts.judge gate show <gate>
    uv run python -m scripts.judge question set <gate> <key> --file question.json [--note "why"]
    uv run python -m scripts.judge question activate <gate> <key> <version|retire>
    uv run python -m scripts.judge gate set <gate> --policy policy.json [--fail-mode closed]
        [--model jev-1.13.0] [--allow-sensitive] [--disabled] [--max-state-chars N] [--note "why"]
    uv run python -m scripts.judge gate activate <gate> <version>
    uv run python -m scripts.judge try <gate> --state "text" | --state-file state.json
    uv run python -m scripts.judge recent [--gate <gate>] [--limit 10]

A question file is the API's own shape:

    {"type": "noul", "instructions": "Does `text` ...?",
     "criteria": {"true": "...", "false": "..."}}

A policy file lists outcomes least severe first, and threshold rules:

    {"outcomes": ["clean", "review", "quarantine"],
     "rules": [{"question": "is_injection", "noul_at_least": 0.3, "outcome": "review"},
               {"question": "is_injection", "noul_at_least": 0.8, "outcome": "quarantine"}]}

`try` makes one real TypeSafe call on behalf of the `researcher` agent, costed
to its department (a few hundred tokens: well under a thousandth of a cent).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from app.config import Settings
from app.db import acting_as, as_service_role, connect
from app.judge import JudgeError, judge_from, store
from app.judge.policy import Policy
from scripts.agent import ROOT_ENV, _Env, _setup

DEFAULT_MODEL = "jev-1.13.0"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    gate = commands.add_parser("gate", help="list, show, set or roll back gates")
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    gate_commands.add_parser("list")
    show = gate_commands.add_parser("show")
    show.add_argument("gate")
    gate_set = gate_commands.add_parser("set")
    gate_set.add_argument("gate")
    gate_set.add_argument("--policy", type=Path, required=True, help="policy JSON file")
    gate_set.add_argument("--model", default=DEFAULT_MODEL)
    gate_set.add_argument("--fail-mode", choices=["closed", "open"], default="closed")
    gate_set.add_argument("--allow-sensitive", action="store_true")
    gate_set.add_argument("--disabled", action="store_true")
    gate_set.add_argument("--max-state-chars", type=int, default=20000)
    gate_set.add_argument("--note")
    gate_activate = gate_commands.add_parser("activate")
    gate_activate.add_argument("gate")
    gate_activate.add_argument("version", type=int)

    question = commands.add_parser("question", help="publish or roll back a question")
    question_commands = question.add_subparsers(dest="question_command", required=True)
    question_set = question_commands.add_parser("set")
    question_set.add_argument("gate")
    question_set.add_argument("key")
    question_set.add_argument("--file", type=Path, required=True, help="question JSON file")
    question_set.add_argument("--note")
    question_activate = question_commands.add_parser("activate")
    question_activate.add_argument("gate")
    question_activate.add_argument("key")
    question_activate.add_argument("version", help="a version number, or `retire`")

    try_gate = commands.add_parser("try", help="judge a state for real, once")
    try_gate.add_argument("gate")
    source = try_gate.add_mutually_exclusive_group(required=True)
    source.add_argument("--state")
    source.add_argument("--state-file", type=Path)

    recent = commands.add_parser("recent", help="recent decisions")
    recent.add_argument("--gate")
    recent.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(argv)
    dsn = _Env().database_url  # type: ignore[call-arg]

    with connect(dsn) as connection:
        try:
            if args.command == "gate":
                return _gate(connection, args)
            if args.command == "question":
                return _question(connection, args)
            if args.command == "try":
                return _try(connection, args)
            return _recent(connection, args)
        except JudgeError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1


def _gate(connection: psycopg.Connection, args: argparse.Namespace) -> int:
    org_id, user_id, _ = _setup(connection)
    command = args.gate_command
    if command == "list":
        for g in store.list_gates(connection, user_id=user_id, org_id=org_id):
            state = "on " if g.enabled else "OFF"
            print(f"{state} {g.gate} v{g.version}  {g.model}  fails {g.fail_mode}  {g.note or ''}")
        return 0
    if command == "show":
        versions, questions = store.gate_history(
            connection, user_id=user_id, org_id=org_id, gate=args.gate
        )
        for v in versions:
            mark = "*" if v.active else " "
            print(
                f"{mark} v{v.version} {'enabled' if v.enabled else 'DISABLED'} {v.model} "
                f"fails {v.fail_mode} sensitive={'allowed' if v.allow_sensitive else 'refused'} "
                f"max {v.max_state_chars} chars  {v.note or ''}"
            )
            if v.active:
                print("    " + json.dumps(v.policy, indent=2).replace("\n", "\n    "))
        for q in questions:
            mark = "*" if q.active else " "
            print(f"{mark} {q.key} v{q.version} ({q.type})  {q.note or ''}")
            if q.active:
                print(f"    instructions: {json.dumps(q.instructions)}")
                if q.criteria is not None:
                    print(f"    criteria: {json.dumps(q.criteria)}")
        return 0
    if command == "set":
        policy = json.loads(args.policy.read_text())
        Policy.model_validate(policy)
        v = store.publish_gate(
            connection,
            user_id=user_id,
            org_id=org_id,
            gate=args.gate,
            model=args.model,
            policy=policy,
            fail_mode=args.fail_mode,
            allow_sensitive=args.allow_sensitive,
            enabled=not args.disabled,
            max_state_chars=args.max_state_chars,
            note=args.note,
        )
    else:
        v = store.activate_gate(
            connection, user_id=user_id, org_id=org_id, gate=args.gate, version=args.version
        )
    print(f"{v.gate}: version {v.version} is live")
    return 0


def _question(connection: psycopg.Connection, args: argparse.Namespace) -> int:
    org_id, user_id, _ = _setup(connection)
    if args.question_command == "set":
        spec: dict[str, Any] = json.loads(args.file.read_text())
        q = store.publish_question(
            connection,
            user_id=user_id,
            org_id=org_id,
            gate=args.gate,
            key=args.key,
            type=spec["type"],
            instructions=spec["instructions"],
            criteria=spec.get("criteria"),
            note=args.note,
        )
        print(f"{q.gate}.{q.key}: version {q.version} is live")
        return 0
    version = None if args.version == "retire" else int(args.version)
    q = store.activate_question(
        connection,
        user_id=user_id,
        org_id=org_id,
        gate=args.gate,
        key=args.key,
        version=version,
    )
    print(f"{args.gate}.{args.key}: " + (f"version {q.version} is live" if q else "retired"))
    return 0


def _try(connection: psycopg.Connection, args: argparse.Namespace) -> int:
    _, user_id, agent_id = _setup(connection)
    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    if args.state_file:
        text = args.state_file.read_text()
        try:
            state: Any = json.loads(text)
        except json.JSONDecodeError:
            state = text
    else:
        state = args.state

    with acting_as(connection, user_id=user_id) as conn:
        decision = judge_from(conn, settings).run(args.gate, state, agent_id=agent_id)

    failed = "  (FAILED: fail mode applied)" if decision.failed else ""
    print(f"outcome   {decision.outcome}{failed}")
    print(f"gate      {decision.gate} v{decision.gate_version}, model {decision.model}")
    print(f"cost      ${decision.cost_usd:.8f}  latency {decision.latency_ms} ms")
    for reason in decision.reasons:
        print(f"reason    [{reason.outcome}] {reason.text}")
    for key, answer in decision.answers.items():
        print(f"answer    {key}: {json.dumps(answer.model_dump())}")
    return 0


def _recent(connection: psycopg.Connection, args: argparse.Namespace) -> int:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select created_at, payload from public.events
            where type = 'judgment_made' and (%s::text is null or payload->>'gate' = %s)
            order by created_at desc limit %s
            """,
            (args.gate, args.gate, args.limit),
        )
        for row in cursor.fetchall():
            p = row["payload"]
            failed = f"  FAILED ({p['failure']})" if p.get("failed") else ""
            print(
                f"{row['created_at']:%Y-%m-%d %H:%M:%S}  {p['gate']} v{p['gate_version']}  "
                f"{p['outcome']}{failed}  ${p.get('cost_usd') or 0:.8f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
