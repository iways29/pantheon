# ADR 008: Scheduled triggers run from the database

- **Status:** Accepted (code and migrations done; live wiring waits on the owner, see below)
- **Date:** 2026-09-25
- **Step:** 4 (Triggers and safety)

## Context

Until now an agent only ran when the owner typed a command. Step 4 wakes agents
on their own. The owner's requirement, beyond the plan: agents should do a
**fixed morning routine** and otherwise wait to be told, not run on their own
all day, and the owner can step in at any time.

The plan's open decision was Vercel Cron, Supabase `pg_cron` plus database
webhooks, or a queue. The rule added in Step 5b (now Step 6) is that configurable things
live in the database, not in code. A Vercel cron schedule lives in
`vercel.json`, so changing it means a redeploy.

## Decision

**Supabase `pg_cron` and `pg_net`, with the schedule stored in a table.**

- **`triggers` table.** One row per fixed task: agent, task, time of day,
  weekdays, IANA time zone, grace period, per-run caps. Created **disabled**.
  A morning routine is several triggers at similar times. Every create, change
  and delete is an `events` row.
- **Once per local day.** A trigger has fired for a date when `last_slot` is
  that date. The run's idempotency key is `trigger:<id>:<local date>`, so even
  if the marker were lost the unique key on `runs` stops a second run.
- **Grace window.** A missed tick is made up, but only within `grace_minutes`
  (default 120) of the scheduled time. A long outage skips the slot rather
  than releasing stale work.
- **Nothing unattended runs unless every gate is open:** trigger enabled,
  agent enabled, department enabled, org kill switch off. The department's
  daily budget and the per-run caps still apply. Scheduled runs default to
  50,000 tokens (a manual run's default is 100,000).
- **The database creates the run; the API advances it.** Once a minute
  `pantheon_tick()` (1) creates a run for each due trigger, in SQL, and (2)
  calls `POST /internal/runs/{id}/advance` through `pg_net` for scheduled runs
  that need it. The run row is the durable record, so a lost request is simply
  repeated on the next tick, and `advance_run`'s lease makes a repeat
  harmless.
- **Only runs that need a poke get one:** not yet started, paused because an
  invocation ran out of time, or running with a lapsed lease (an invocation
  died). Runs stopped by a budget, the kill switch or a missing prompt are not
  poked every minute; they resume when someone resumes them. At most five
  pokes per run, spaced 45 seconds apart.
- **Authentication is a shared secret**, `TRIGGER_SECRET` on the API, stored
  in Supabase Vault as `pantheon_trigger_secret` together with the API's
  address as `pantheon_api_url`. Without the secret the endpoint refuses every
  request (503), never accepts everything. The endpoint's time budget is 45
  seconds, inside the function's 60-second limit.
- **Retry.** `retry_run` puts a run that failed on an `error` back to
  resumable. It resumes from its checkpoint, so only the failed step runs
  again, and every side effect in a step is idempotent (facts are keyed by run
  and claim). A run that hit a step or token cap stays final: that is a
  decision, not a fault.
- **Plain tables and functions are testable anywhere.** The extensions and the
  cron job are wired by a second migration that does nothing where `pg_cron`
  is unavailable (local, CI). The tick's HTTP call cannot be exercised there;
  it is checked against the real project once the owner has set the secrets.

## What the owner still controls

Triggers only start scheduled runs. They lock nothing. At any time the owner
can start or ask an agent by hand, flip the kill switch (stops every run and
every trigger), disable a single trigger, or change a prompt for the next run.
Until the chat window exists (Step 10) the command line is the way in.
Irreversible actions still go through the approval queue (Step 7.5); nothing
here removes that requirement.

## Not done here

- **Event triggers** ("when X happens"). The design allows them: a database
  trigger can create a run the same way. No event source exists yet.
- **Agents cannot schedule themselves.** Triggers are written by the owner
  through RLS; no agent is given a way to create one, so a morning routine
  cannot grow into an all-day loop on its own.
- **Runs paused for budget or the kill switch do not resume by themselves.**
  Deciding when they should is an owner policy question for later.
- **Control Center screen.** Step 11 adds the screen for editing the morning
  routine; until then the `scripts.agent trigger` commands do it.

## Owner steps to go live (nothing here was done on the live project)

1. Apply both migrations to Supabase. The second enables `pg_cron` and `pg_net`
   and starts the tick. Harmless until a trigger is enabled.
2. Generate a long random secret. Put it in the Vercel `pantheon-api` project
   as `TRIGGER_SECRET`, and set `DATABASE_URL` there to the pooler URL if it is
   not already.
3. In the Supabase SQL editor: `select vault.create_secret('<secret>',
   'pantheon_trigger_secret');` and `select vault.create_secret('<api base
   url>', 'pantheon_api_url');`.
4. Deploy the API (merge to `main`).
5. Create a trigger, enable it, and watch `cron.job_run_details`,
   `net._http_response` and the `events` table for the first firing.
