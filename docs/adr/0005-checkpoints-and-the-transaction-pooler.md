# ADR 005: Checkpoints in a private schema, reached through the transaction pooler

- **Status:** Accepted; verified against Supabase's transaction pooler on 2026-09-21
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
  with `autocommit=True` and `prepare_threshold=None`. It connects as the
  backend's own login, like the rest of the app. On Supabase that is
  `postgres`, which owns the checkpoint schema. `search_path=langgraph,public`
  is a connect-time option, so the library's unqualified table names resolve
  to the private schema. It has to be connect-time, because a `SET` would not
  survive to the next transaction under the pooler.
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

`scripts/pooler_probe.py` was run against the project's transaction pooler
(`aws-0-us-west-2.pooler.supabase.com:6543`) on 2026-09-21:

| Check | Result |
| --- | --- |
| `prepare_threshold=0`, LangGraph's default | **Fails**: `DuplicatePreparedStatement` |
| `prepare_threshold=None` | ok |
| Connect-time `search_path`, on every statement | ok |
| Connect-time `role` | **Ignored**: statements ran as `postgres` |
| `SET LOCAL ROLE` + JWT claims within a transaction | ok |
| Pipeline mode, which the saver uses for writes | ok |

The first row confirms the problem this ADR exists for. The ignored `role`
changed the design. An earlier draft had the checkpointer connect as
`service_role`; it now connects as the backend login instead. Migration
`20260921050000` makes that work everywhere:

- The org_id trigger becomes `SECURITY DEFINER`, so it can read the run
  through RLS whoever writes the checkpoint.
- Backend-only row policies cover logins that are merely members of
  `service_role`, such as the local test role.

`anon` and `authenticated` still have no privilege on the schema. The
dedicated-role fallback is not needed.

## Consequences

- One extra primary-key lookup per checkpoint write, for the `org_id`
  trigger.
- Checkpoint rows are deleted with their org. They are not deleted with their
  run, because `thread_id` is text and cannot carry a foreign key to
  `runs.id`. Pruning old checkpoints is a job for later.
