"""Run the research agent from the command line.

The owner's way to drive Step 3 until triggers (Step 4) and the UI (Step 10)
exist. Every run goes through the same lifecycle a serverless invocation
would: start_run, then advance_run in bounded invocations until it stops.

    cd api
    uv run python -m scripts.agent seed --user-id <uuid> [--budget 0.25]
    uv run python -m scripts.agent ask "What is ...?" [--max-steps N] [--max-tokens N]
    uv run python -m scripts.agent resume <run-id>
    uv run python -m scripts.agent show <run-id>
    uv run python -m scripts.agent trigger list
    uv run python -m scripts.agent trigger add <name> --time 08:30 --tz <zone> --question "..."
    uv run python -m scripts.agent trigger enable|disable|remove <name>
    uv run python -m scripts.agent retry <run-id>
    uv run python -m scripts.agent prompt list [--slot answer]
    uv run python -m scripts.agent prompt set <slot> --file prompt.txt [--note "why"]
    uv run python -m scripts.agent prompt activate <slot> <version>

`seed` creates, idempotently, the org, the user's membership, a `research`
department with a daily budget, a `researcher` agent on the cheap tier, that
agent's starting prompts, the price of TypeSafe's Jev model and the starter
judge gates. `trigger` manages the morning routine: fixed tasks at fixed
times, once a day, created switched off. `prompt` reads and changes the
prompts in the database: a `set` takes effect on the next run, with no code
change.
The user must already exist in auth.users; on a local database with no
Supabase Auth, `--create-local-user` adds a stand-in row.

Reads DATABASE_URL and the gateway and Langfuse settings from the
repository's .env. Real model calls cost real money; each run is a couple of
cheap-tier calls.
"""

import argparse
import sys
import uuid
from pathlib import Path

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agents import prompts, triggers
from app.agents.runs import RunReport, Runtime, advance_run, report, retry_run, start_run
from app.agents.starter_prompts import STARTER_PROMPTS
from app.config import Settings
from app.db import as_service_role, connect
from app.gateway.factory import systemone_transport_from, tier_map_from, transport_from
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.tracing import tracer_from

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
ORG_NAME = "Pantheon"
DEPARTMENT = "research"
AGENT = "researcher"
#: Per invocation, as a Vercel function would get. Well inside the 300s default.
INVOCATION_SECONDS = 240.0
MAX_INVOCATIONS = 20


class _Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV, extra="ignore")
    database_url: str


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed", help="create the org, department and agent")
    seed.add_argument("--user-id", help="the owner's auth.users id (default OWNER_USER_ID)")
    seed.add_argument("--budget", type=float, default=0.25, help="department USD per day")
    seed.add_argument("--create-local-user", action="store_true")

    ask = commands.add_parser("ask", help="start a run and advance it to a stop")
    ask.add_argument("question")
    ask.add_argument("--max-steps", type=int)
    ask.add_argument("--max-tokens", type=int)
    ask.add_argument("--idempotency-key", default=None)

    resume = commands.add_parser("resume", help="advance an existing run")
    resume.add_argument("run_id")

    prompt = commands.add_parser("prompt", help="list, publish or roll back the agent's prompts")
    prompt_commands = prompt.add_subparsers(dest="prompt_command", required=True)
    prompt_list = prompt_commands.add_parser("list", help="every version, newest first")
    prompt_list.add_argument("--slot")
    prompt_set = prompt_commands.add_parser("set", help="publish a new version and make it live")
    prompt_set.add_argument("slot")
    prompt_set.add_argument("--file", required=True, type=Path)
    prompt_set.add_argument("--note")
    prompt_activate = prompt_commands.add_parser("activate", help="make an old version live")
    prompt_activate.add_argument("slot")
    prompt_activate.add_argument("version", type=int)

    retry = commands.add_parser("retry", help="resume a run that failed on an error")
    retry.add_argument("run_id")

    trigger = commands.add_parser("trigger", help="manage the scheduled morning tasks")
    trigger_commands = trigger.add_subparsers(dest="trigger_command", required=True)
    trigger_commands.add_parser("list", help="every trigger and whether it is on")
    trigger_add = trigger_commands.add_parser("add", help="create a trigger (switched off)")
    trigger_add.add_argument("name")
    trigger_add.add_argument("--time", required=True, help="local time, 24-hour, e.g. 08:30")
    trigger_add.add_argument("--tz", required=True, help="IANA zone, e.g. Asia/Kolkata")
    trigger_add.add_argument("--question", required=True, help="the task the agent is given")
    trigger_add.add_argument("--days", default="mon-fri", help="mon-fri, all, or mon,wed,fri")
    trigger_add.add_argument("--grace", type=int, help="minutes after its time it may still run")
    trigger_add.add_argument("--max-tokens", type=int)
    for verb in ("enable", "disable", "remove"):
        trigger_commands.add_parser(verb).add_argument("name")

    show = commands.add_parser("show", help="print a run and its events")
    show.add_argument("run_id")

    args = parser.parse_args(argv)
    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    dsn = _Env().database_url

    if args.command == "seed":
        user_id = args.user_id or settings.owner_user_id
        if not user_id:
            parser.error("give --user-id or set OWNER_USER_ID")
        with connect(dsn) as connection:
            _seed(connection, user_id, args.budget, create_user=args.create_local_user)
        return 0

    if args.command == "prompt":
        with connect(dsn) as connection:
            return _prompt(connection, args)

    if args.command == "trigger":
        with connect(dsn) as connection:
            return _trigger(connection, args)

    if args.command == "retry":
        with connect(dsn) as connection:
            retry_run(connection, args.run_id)
        args.command = "resume"

    if args.command == "show":
        with connect(dsn) as connection:
            _print(report(connection, args.run_id))
            _print_events(connection, args.run_id)
        return 0

    runtime = Runtime(
        dsn=dsn,
        transport=transport_from(settings),
        tiers=tier_map_from(settings),
        embedder=None,
        tracer=tracer_from(settings),
        systemone=systemone_transport_from(settings) if settings.typesafe_api_key else None,
    )
    if args.command == "ask":
        with connect(dsn) as connection:
            org_id, user_id, agent_id = _setup(connection)
            run_id = start_run(
                connection,
                org_id=org_id,
                agent_id=agent_id,
                requested_by=user_id,
                question=args.question,
                idempotency_key=args.idempotency_key or f"cli-{uuid.uuid4()}",
                max_steps=args.max_steps,
                max_tokens=args.max_tokens,
            )
    else:
        run_id = args.run_id
    result = drive(runtime, run_id)
    _print(result)
    return 0 if result.status == "succeeded" else 1


def _trigger(connection: psycopg.Connection, args: argparse.Namespace) -> int:
    org_id, user_id, agent_id = _setup(connection)
    command = args.trigger_command
    try:
        if command == "list":
            for t in triggers.list_triggers(connection, user_id=user_id):
                state = "ON " if t.enabled else "off"
                days = ",".join(str(d) for d in t.days_of_week)
                print(f"{state} {t.name}  {t.time_of_day:%H:%M} {t.timezone}  days {days}")
                print(f"    {t.task.get('question')}   (last fired for {t.last_slot or 'never'})")
            return 0
        if command == "add":
            t = triggers.create(
                connection,
                user_id=user_id,
                org_id=org_id,
                agent_id=agent_id,
                name=args.name,
                task={"question": args.question},
                time_of_day=triggers.parse_time(args.time),
                timezone=args.tz,
                days_of_week=triggers.parse_days(args.days),
                grace_minutes=args.grace,
                max_tokens=args.max_tokens,
            )
            print(f"created '{t.name}' (off). Turn it on: trigger enable {t.name}")
        elif command == "remove":
            triggers.delete(connection, user_id=user_id, name=args.name)
            print(f"removed '{args.name}'")
        else:
            t = triggers.set_enabled(
                connection, user_id=user_id, name=args.name, enabled=command == "enable"
            )
            print(f"'{t.name}' is now {'ON' if t.enabled else 'off'}")
    except triggers.TriggerNotFound:
        print(f"No trigger named '{args.name}'", file=sys.stderr)
        return 1
    except (ValueError, psycopg.Error) as error:
        print(f"Could not do that: {error}", file=sys.stderr)
        return 1
    return 0


def _prompt(connection: psycopg.Connection, args: argparse.Namespace) -> int:
    _, user_id, agent_id = _setup(connection)
    if args.prompt_command == "list":
        for p in prompts.history(connection, user_id=user_id, agent_id=agent_id, slot=args.slot):
            mark = "*" if p.active else " "
            print(f"{mark} {p.slot} v{p.version}  {p.note or ''}\n    {p.body}")
        return 0
    if args.prompt_command == "set":
        body = args.file.read_text().strip()
        p = prompts.publish(
            connection,
            user_id=user_id,
            agent_id=agent_id,
            slot=args.slot,
            body=body,
            note=args.note,
        )
    else:
        p = prompts.activate(
            connection, user_id=user_id, agent_id=agent_id, slot=args.slot, version=args.version
        )
    print(f"{p.slot}: version {p.version} is live")
    return 0


def drive(runtime: Runtime, run_id: uuid.UUID | str) -> RunReport:
    """Advance a run invocation by invocation until it stops for a reason
    other than running out of time."""
    for _ in range(MAX_INVOCATIONS):
        result = advance_run(runtime, run_id, deadline_seconds=INVOCATION_SECONDS)
        if result.stop_reason != "deadline":
            return result
    return result


def _seed(
    connection: psycopg.Connection, user_id: str, budget: float, *, create_user: bool
) -> None:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        if create_user:
            cursor.execute(
                "insert into auth.users (id, email) values (%s, %s) on conflict (id) do nothing",
                (user_id, f"owner-{user_id[:8]}@local.invalid"),
            )
        cursor.execute("select id from public.orgs where name = %s", (ORG_NAME,))
        org = cursor.fetchone()
        if org is None:
            cursor.execute("insert into public.orgs (name) values (%s) returning id", (ORG_NAME,))
            org = cursor.fetchone()
        org_id = org["id"]
        cursor.execute(
            "insert into public.org_members (org_id, user_id) values (%s, %s) "
            "on conflict do nothing",
            (org_id, user_id),
        )
        cursor.execute(
            "insert into public.departments (org_id, name, daily_budget_usd) values (%s, %s, %s) "
            "on conflict (org_id, name) do update set daily_budget_usd = excluded.daily_budget_usd "
            "returning id",
            (org_id, DEPARTMENT, budget),
        )
        department_id = cursor.fetchone()["id"]
        cursor.execute(
            "select id from public.agents where org_id = %s and name = %s", (org_id, AGENT)
        )
        if cursor.fetchone() is None:
            cursor.execute(
                "insert into public.agents (org_id, department_id, name, role, model_tier) "
                "values (%s, %s, %s, 'research', 'cheap')",
                (org_id, department_id, AGENT),
            )
        cursor.execute(
            "select id from public.agents where org_id = %s and name = %s", (org_id, AGENT)
        )
        agent_id = cursor.fetchone()["id"]
        # Starting prompts only where the agent has none: re-seeding must never
        # overwrite what the owner has since edited.
        for slot, body in STARTER_PROMPTS["research"].items():
            cursor.execute(
                "insert into public.agent_prompts (org_id, agent_id, slot, version, body, "
                "note, active) select %s, %s, %s, 1, %s, 'Starting prompt', true "
                "where not exists (select 1 from public.agent_prompts "
                "where agent_id = %s and slot = %s)",
                (org_id, agent_id, slot, body, agent_id, slot),
            )
        # Jev's published price (ADR 009), for an org created after the
        # migration that seeded it. Never overwrites a price the owner set.
        # The embedding model (ADR 013), for an org created after the
        # migration that assigned it. Never overwrites the owner's choice.
        cursor.execute(
            "insert into public.model_tier_assignments (org_id, department_id, tier, model) "
            "values (%s, null, 'embedding', 'openai/text-embedding-3-small') "
            "on conflict on constraint model_tier_assignments_scope_key do nothing",
            (org_id,),
        )
        cursor.execute(
            "insert into public.model_prices (org_id, provider, model, input_usd_per_mtok, "
            "output_usd_per_mtok) values (%s, 'typesafe', 'jev-1.13.0', 0.042, 0) "
            "on conflict (org_id, provider, model) do nothing",
            (org_id,),
        )
    # The brain write gate's starting questions and thresholds (ADR 010). A
    # gate the org already has, edited or not, is left alone.
    seeded = seed_gates(connection, user_id=user_id, org_id=org_id, gates=STARTER_GATES)
    print(f"org {org_id}: department '{DEPARTMENT}' at ${budget:.2f}/day, agent '{AGENT}'")
    print(f"judge gates seeded: {', '.join(seeded) or 'none (already present)'}")


def _setup(connection: psycopg.Connection) -> tuple[str, str, str]:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select o.id as org_id, m.user_id, a.id as agent_id
            from public.orgs o
            join public.org_members m on m.org_id = o.id
            join public.agents a on a.org_id = o.id and a.name = %s
            where o.name = %s
            order by m.created_at
            limit 1
            """,
            (AGENT, ORG_NAME),
        )
        row = cursor.fetchone()
    if row is None:
        raise SystemExit("Not seeded yet: run `python -m scripts.agent seed` first")
    return str(row["org_id"]), str(row["user_id"]), str(row["agent_id"])


def _print(result: RunReport) -> None:
    print(f"run      {result.run_id}")
    print(f"status   {result.status} ({result.stop_reason})")
    print(f"steps    {result.steps_taken}")
    print(f"tokens   {result.tokens_in} in / {result.tokens_out} out")
    print(f"cost     ${result.cost_usd:.6f}")
    if result.error:
        print(f"error    {result.error}")
    if result.output:
        print(f"answer   {result.output.get('answer')}")
        print(f"stored   {len(result.output.get('stored_fact_ids', []))} new facts")


def _print_events(connection: psycopg.Connection, run_id: str) -> None:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select created_at, type, payload from public.events where run_id = %s "
            "order by created_at, id",
            (run_id,),
        )
        for row in cursor.fetchall():
            print(f"  {row['created_at']:%H:%M:%S}  {row['type']:<18} {row['payload']}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
