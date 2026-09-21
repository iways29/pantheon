"""Probe how a Postgres endpoint behaves for the things Step 3 depends on.

Build plan Step 3: "Test the pooler early: transaction-mode pooling can break
prepared statements." Run against Supabase's transaction pooler (port 6543),
and optionally the session pooler (5432), before trusting the checkpointer to
either. Read-only: creates nothing and changes nothing.

    cd api && uv run python -m scripts.pooler_probe            # SUPABASE_POOLER_URL
    cd api && uv run python -m scripts.pooler_probe <dsn-env-var-name>

Checks, each reported ok / FAIL with the error:

1. psycopg's default auto-preparing (prepare_threshold=5), which the pooler
   documentation says transaction mode cannot support.
2. prepare_threshold=0, what LangGraph's own from_conn_string() uses.
3. prepare_threshold=None, Supabase's documented fix for psycopg.
4. The checkpointer's connect-time settings (`options=-c role=service_role
   -c search_path=langgraph,public`): the role so it may use the private
   `langgraph` schema, the path so LangGraph's unqualified table names resolve
   there. Checked on every statement across autocommit transactions, since
   under transaction pooling each one may land on a different backend.
5. SET LOCAL ROLE plus request.jwt.claims inside one transaction, which is how
   app.db.acting_as keeps RLS in force.
6. Pipeline mode, which LangGraph's PostgresSaver uses for its writes.

The DSN comes from the repository's .env and is never printed.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
REPEATS = 12  # past psycopg's default prepare threshold of 5


def _dsn(name: str) -> str:
    """Read one value from the environment or the repository's .env."""
    value = os.getenv(name)
    if not value and ROOT_ENV.exists():
        for line in ROOT_ENV.read_text().splitlines():
            key, sep, raw = line.partition("=")
            if sep and key.strip() == name:
                value = raw.strip().strip('"').strip("'")
    if not value:
        raise SystemExit(f"{name} is not set in the environment or .env")
    return value


def _check(label: str, probe: Callable[[], str]) -> bool:
    try:
        detail = probe()
    except Exception as error:  # the error is the finding
        print(f"FAIL  {label}\n      {type(error).__name__}: {str(error).strip()[:300]}")
        return False
    print(f"ok    {label}{f' -- {detail}' if detail else ''}")
    return True


def _repeat_query(dsn: str, **connect_kwargs: object) -> Callable[[], str]:
    def probe() -> str:
        with psycopg.connect(dsn, autocommit=True, **connect_kwargs) as conn:  # type: ignore[arg-type]
            for i in range(REPEATS):
                conn.execute("select %s::int + 1", (i,)).fetchone()
        return f"{REPEATS} identical parameterised queries"

    return probe


def _checkpointer_options(dsn: str) -> str:
    options = "-c role=service_role -c search_path=langgraph,public"
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None, options=options) as conn:
        seen = {
            (role, path.replace(" ", ""))
            for role, path in (
                conn.execute("select current_user, current_setting('search_path')").fetchone()
                for _ in range(REPEATS)
            )
        }
    if seen != {("service_role", "langgraph,public")}:
        raise AssertionError(f"connect-time settings not held on every statement: {sorted(seen)}")
    return "role and search_path held on every statement"


def _set_local_role(dsn: str) -> str:
    with psycopg.connect(dsn, prepare_threshold=None, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("set local role authenticated")
            conn.execute(
                "select set_config('request.jwt.claims', %s, true)",
                ('{"sub": "00000000-0000-0000-0000-000000000000"}',),
            )
            row = conn.execute("select current_user as role, auth.uid() as uid").fetchone()
    if row["role"] != "authenticated" or row["uid"] is None:
        raise AssertionError(f"transaction-local identity lost: {row}")
    return "role and claims held for the whole transaction"


def _pipeline(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
        with conn.pipeline(), conn.cursor() as cur:
            for i in range(REPEATS):
                cur.execute("select %s::int", (i,))
    return "pipelined statements completed"


def main(argv: list[str]) -> int:
    name = argv[0] if argv else "SUPABASE_POOLER_URL"
    dsn = _dsn(name)
    print(f"probing {name}\n")
    results = [
        _check("default auto-prepare (threshold 5)", _repeat_query(dsn)),
        _check("prepare_threshold=0 (LangGraph)", _repeat_query(dsn, prepare_threshold=0)),
        _check("prepare_threshold=None", _repeat_query(dsn, prepare_threshold=None)),
        _check("role + search_path via connect options", lambda: _checkpointer_options(dsn)),
        _check("SET LOCAL ROLE + claims within a transaction", lambda: _set_local_role(dsn)),
        _check("pipeline mode", lambda: _pipeline(dsn)),
    ]
    return 0 if all(results[2:]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
