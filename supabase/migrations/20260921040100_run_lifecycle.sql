-- What a run needs to be started, advanced in short steps, and resumed.
--
-- Step 3. Runs are advanced in bounded invocations (a serverless function has
-- minutes, not hours), so a run is picked up and put down many times. These
-- columns hold what survives between invocations beside LangGraph's own
-- checkpoint: the input it started from, who it acts for, how far it got,
-- why it stopped, and a lease so two invocations never advance it at once.
--
-- Additive only; runs is still empty everywhere, but nothing here needs that.

alter table public.runs
  add column input jsonb not null default '{}'::jsonb,
  add column output jsonb,
  -- The user whose identity the run acts under, so RLS stays in force for an
  -- agent exactly as it would for that person. Phase 1: always the owner.
  add column requested_by uuid references auth.users (id),
  add column steps_taken integer not null default 0 check (steps_taken >= 0),
  -- Why the run last stopped: finished, a cap, the kill switch, a budget, an
  -- error, or an invocation's time budget running out.
  add column stop_reason text,
  add column error text,
  add column lease_expires_at timestamptz;

comment on column public.runs.lease_expires_at is
  'Set while an invocation is advancing the run. Another invocation may take '
  'over only once it has passed, so a retried trigger never runs a step twice '
  'concurrently, and a crashed invocation does not hold the run forever.';
comment on column public.runs.stop_reason is
  'completed, max_steps, max_tokens, kill_switch, budget_exceeded, '
  'agent_disabled, department_disabled, deadline, or error.';

-- A resumed step can re-run after its database writes committed but before
-- LangGraph saved the checkpoint. This makes re-inserting the same claim from
-- the same run a no-op instead of a duplicate fact.
create unique index facts_run_claim_key
  on public.facts (created_by_run_id, md5(claim))
  where created_by_run_id is not null;
