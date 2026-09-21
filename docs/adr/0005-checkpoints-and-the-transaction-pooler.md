# ADR 005: Checkpoints in a private schema, reached through the transaction pooler

- **Status:** Accepted; the live pooler probe is still to run (see Verification)
- **Date:** 2026-09-21
- **Step:** 3 (First agent end to end); resolves Open decision 4

## Context

LangGraph's Postgres checkpointer saves agent state after every step. That is
what makes a run resumable, and it has to reach Supabase from Vercel Functions.

Two facts from the current Supabase docs decide the connection:

1. **Vercel is IPv4-only, and Supabase's direct connection is IPv6-only**
   unless the project buys the IPv4 add-on. Supabase lists Vercel by name as
   an IPv4-only platform.
2. **The shared pooler (Supavisor) is IPv4 on every plan.** Its transaction
   mode (port 6543) is the documented choice for serverless functions. It does
   not support prepared statements, and a session-level `SET` does not survive
   to the next transaction, because each transaction may run on a different
   backend.

Two facts from LangGraph (`langgraph-checkpoint-postgres` 3.1.2) collide with
that:

3. Its `from_conn_string()` connects with `prepare_threshold=0`, which prepares
   every statement immediately. Under transaction pooling that fails outright.
4. Its `setup()` creates four tables in `public`, with no `org_id` and no RLS.
   Supabase's Data API exposes `public`, and CLAUDE.md requires `org_id` and
   RLS on every table.

## Options for the connection (Open decision 4)

| Option | Cost | Trade-off |
| --- | --- | --- |
| **Transaction pooler, 6543** | Included | Built for serverless; no prepared statements, no session state |
| Session pooler, 5432 | Included | Supports both, but each function instance holds a pooler client for its whole life, so bursts of instances can exhaust the plan's client limit |
| Direct connection + IPv4 add-on | Paid add-on | Full Postgres semantics; connection count grows with function instances, which is what poolers exist to prevent |

## Decision

**Production `DATABASE_URL` is the transaction pooler.** Everything is written
to work under it:

- **App connections** use `prepare_threshold=None` (`app/db.py`), Supabase's
  documented fix for psycopg. `acting_as` already scopes the role and the JWT
  claims to one transaction with `SET LOCAL`, which pooling preserves.
- **The checkpointer opens its own connection** (`app/agents/checkpointer.py`)
  with `autocommit=True` and `prepare_threshold=None`, plus two connect-time
  options:
  - `role=service_role`, so only the backend can reach the checkpoint schema,
    and it bypasses RLS there;
  - `search_path=langgraph,public`, so the library's unqualified table names
    resolve to the private schema. This has to be a connect-time option: a
    `SET` would not survive to the next transaction under the pooler.
- **The checkpoint tables come from our own migration**
  (`20260921040000_langgraph_checkpoints.sql`), in a `langgraph` schema that
  the Data API does not expose. `anon` and `authenticated` are revoked by
  name. Each table has `org_id`, filled by a trigger from the run the thread
  belongs to (a thread's id is its run's id), and a checkpoint for a thread
  that is not a run is refused. RLS is on, with a member read policy ready for
  a future run inspector. `setup()` is never called.
- **`langgraph-checkpoint-postgres` is pinned to exactly 3.1.2**, because the
  migration copies its schema. Upgrading means diffing its `MIGRATIONS` list
  against that file and adding a migration for anything that changed.

## Verification

- Locally, against plain Postgres as a non-superuser role:
  `scripts/pooler_probe.py` passes all six checks, and the lifecycle tests run
  the real checkpointer end to end.
- **Against Supabase's pooler: not yet run.** It needs the owner's pooler
  connection string. The run decides one open question: whether Supavisor's
  transaction mode passes the connect-time `options` through to Postgres.
  Supabase's docs do not say.
- **If it does not,** the fallback is a dedicated login role for the
  checkpointer, with `search_path` and `role` set on the role itself
  (`ALTER ROLE ... SET`). Role-level settings apply on every backend the
  pooler opens. The owner would create that role's password by hand.

## Consequences

- One extra primary-key lookup per checkpoint write, for the `org_id`
  trigger.
- Checkpoint rows are deleted with their org. They are not deleted with their
  run, because `thread_id` is text and cannot carry a foreign key to
  `runs.id`. Pruning old checkpoints is a job for later.
