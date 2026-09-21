"""Run the research agent from the command line.

The owner's way to drive Step 3 until triggers (Step 4) and the UI (Step 7)
exist. Every run goes through the same lifecycle a serverless invocation
would: start_run, then advance_run in bounded invocations until it stops.

    cd api
    uv run python -m scripts.agent seed --user-id <uuid> [--budget 0.25]
    uv run python -m scripts.agent ask "What is ...?" [--max-steps N] [--max-tokens N]
    uv run python -m scripts.agent resume <run-id>
    uv run python -m scripts.agent show <run-id>

`seed` creates, idempotently, the org, the user's membership, a `research`
department with a daily budget, and a `researcher` agent on the cheap tier.
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

from app.agents.runs import RunReport, Runtime, advance_run, report, start_run
from app.brain.embeddings import HashingEmbedder
from app.config import Settings
from app.db import as_service_role, connect
from app.gateway.factory import tier_map_from, transport_from
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

    if args.command == "show":
        with connect(dsn) as connection:
            _print(report(connection, args.run_id))
            _print_events(connection, args.run_id)
        return 0

    runtime = Runtime(
        dsn=dsn,
        transport=transport_from(settings),
        tiers=tier_map_from(settings),
        embedder=HashingEmbedder(),
        tracer=tracer_from(settings),
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
    print(f"org {org_id}: department '{DEPARTMENT}' at ${budget:.2f}/day, agent '{AGENT}'")


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
