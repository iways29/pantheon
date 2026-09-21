"""LangGraph's Postgres checkpointer, pointed at the private `langgraph` schema.

ADR 005. Two things differ from LangGraph's own `from_conn_string()`:

- `prepare_threshold=None`, not 0. LangGraph's default prepares every
  statement immediately; against Supabase's transaction pooler (the only
  endpoint Vercel can reach over IPv4) that fails with
  DuplicatePreparedStatement, as scripts/pooler_probe.py showed.
- `search_path=langgraph,public` as a connect-time option, so the library's
  unqualified table names resolve to the private schema. Connect-time rather
  than `SET`, because under transaction pooling a session `SET` does not
  survive to the next transaction. The pooler honours it on every statement.

It connects as the backend's own login, like the rest of the app. A
connect-time `role` was tried and dropped: the pooler ignores it. On Supabase
that login is `postgres`, which owns the schema; `anon` and `authenticated`
have no privilege on it at all.

`setup()` is never called: the tables come from a versioned migration
(20260921040000_langgraph_checkpoints.sql), with org_id and RLS the library
would not add.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

CHECKPOINTER_OPTIONS = "-c search_path=langgraph,public"


@contextmanager
def checkpointer(dsn: str) -> Iterator[PostgresSaver]:
    """A checkpointer on its own connection, closed when the block ends.

    One connection per invocation: a serverless function advances a run for a
    bounded time and exits, so there is nothing for a pool to reuse.
    """
    connection = psycopg.connect(
        dsn,
        # The library relies on autocommit: each checkpoint write stands alone,
        # which is what makes a crash between steps lose at most one step.
        autocommit=True,
        prepare_threshold=None,
        row_factory=dict_row,
        options=CHECKPOINTER_OPTIONS,
    )
    try:
        yield PostgresSaver(connection)
    finally:
        connection.close()
