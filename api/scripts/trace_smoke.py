"""Send one real trace to Langfuse through the gateway, then leave no trace here.

The Langfuse skill's rule: instrumentation is not done until a real trace has
been fetched and audited. This produces one to audit. Inside a single database
transaction that is rolled back at the end, it seeds a throwaway org, department,
agent and run, then makes three gateway calls in that run:

1. an ordinary call        -> `call-model` generation with text, usage, cost
2. a sensitive call        -> `call-model` generation with the text withheld
3. a call on a disabled agent -> `enforce-call-gates` guardrail

All three share one trace, seeded from the run id. Nothing persists in the
database; the trace persists in Langfuse.

    cd api && uv run python -m scripts.trace_smoke              # real OpenRouter, cheap tier
    cd api && uv run python -m scripts.trace_smoke --fake-model # no OpenRouter spend

Needs DATABASE_URL (a database with the migrations applied) and the three
LANGFUSE_* values in the repository's .env; the real mode also needs
OPENROUTER_API_KEY. Cost in real mode: two calls capped at 32 output tokens on
the cheap tier.
"""

import argparse
import sys
import uuid
from pathlib import Path

import psycopg
from langfuse import Langfuse
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings
from app.db import as_service_role, connect
from app.gateway import Gateway, GatewayError, ModelResponse, Transport
from app.gateway.factory import tier_map_from, transport_from
from app.tracing import LangfuseTracer, langfuse_from

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class _Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV, extra="ignore")
    database_url: str


class _FakeTransport:
    """Answers without calling any provider, for checking Langfuse alone."""

    def complete(self, *, model: str, **_: object) -> ModelResponse:
        return ModelResponse(
            model=model,
            provider="fake",
            text="Paris.",
            tokens_in=14,
            tokens_out=2,
            cost_usd=0.0,
            latency_ms=1,
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake-model", action="store_true", help="skip OpenRouter")
    args = parser.parse_args(argv)

    settings = Settings(_env_file=ROOT_ENV)  # type: ignore[call-arg]
    langfuse = langfuse_from(settings)
    if langfuse is None:
        print("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set in .env", file=sys.stderr)
        return 1
    if not langfuse.auth_check():
        print("Langfuse rejected the keys; check them and LANGFUSE_BASE_URL", file=sys.stderr)
        return 1

    transport = _FakeTransport() if args.fake_model else transport_from(settings)
    connection = connect(_Env().database_url)
    try:
        with connection.transaction():
            run_id = _run(connection, langfuse, transport, settings)
            raise psycopg.Rollback
    finally:
        connection.close()

    langfuse.flush()
    trace_id = langfuse.create_trace_id(seed=run_id)
    print(f"trace {trace_id}")
    print(langfuse.get_trace_url(trace_id=trace_id))
    return 0


def _run(
    connection: psycopg.Connection, langfuse: Langfuse, transport: Transport, settings: Settings
) -> str:
    with as_service_role(connection), connection.cursor() as cursor:
        cursor.execute("insert into public.orgs (name) values ('trace-smoke') returning id")
        org_id = cursor.fetchone()["id"]
        cursor.execute(
            "insert into public.departments (org_id, name, daily_budget_usd) "
            "values (%s, 'research', 1) returning id",
            (org_id,),
        )
        department_id = cursor.fetchone()["id"]
        agents = {}
        for name, enabled in (("analyst", True), ("retired", False)):
            cursor.execute(
                "insert into public.agents (org_id, department_id, name, role, model_tier, enabled)"
                " values (%s, %s, %s, 'worker', 'cheap', %s) returning id",
                (org_id, department_id, name, enabled),
            )
            agents[name] = cursor.fetchone()["id"]
        cursor.execute(
            "insert into public.runs (org_id, agent_id, trigger, status, idempotency_key)"
            " values (%s, %s, 'manual', 'running', %s) returning id",
            (org_id, agents["analyst"], f"trace-smoke-{uuid.uuid4()}"),
        )
        run_id = str(cursor.fetchone()["id"])

    gateway = Gateway(
        connection,
        transport,
        tier_map_from(settings),
        LangfuseTracer(langfuse, public_key=settings.langfuse_public_key or ""),
    )
    question = [
        {"role": "system", "content": "Answer in one short sentence."},
        {"role": "user", "content": "What is the capital of France?"},
    ]
    with as_service_role(connection):
        gateway.complete(
            agent_id=agents["analyst"], messages=question, run_id=run_id, max_tokens=32
        )
        gateway.complete(
            agent_id=agents["analyst"],
            messages=[{"role": "user", "content": "Summarise: confidential test text."}],
            run_id=run_id,
            max_tokens=32,
            sensitive=True,
        )
        try:
            gateway.complete(agent_id=agents["retired"], messages=question, run_id=run_id)
        except GatewayError as error:
            print(f"blocked as expected: {error.code}")
    return run_id


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
