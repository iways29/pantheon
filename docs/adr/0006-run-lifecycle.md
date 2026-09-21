# ADR 006: Runs advance in bounded, leased invocations

- **Status:** Accepted
- **Date:** 2026-09-21
- **Step:** 3 (First agent end to end)

## Context

A Vercel Function runs for 300 seconds by default and 800 at most on Pro.
CLAUDE.md requires runs to be short and resumable, capped in steps and
tokens, and never repeated by a serverless retry. Step 3 must show a run
killed mid-way and resumed from its checkpoint.

## Decision

A run is advanced by `advance_run` (`app/agents/runs.py`) in **invocations**.
Each invocation:

1. **Claims the run under a lease.** A conditional update of
   `lease_expires_at` means two invocations never advance one run at once. A
   crashed invocation holds the run only until its lease lapses. The lease is
   renewed after every step.
2. **Checks the kill switch** before running anything.
3. **Resumes the graph** from its last checkpoint (thread id = run id), or
   starts it from `runs.input`.
4. **Runs steps until something stops it.** Between steps it rolls tokens and
   cost up from `model_calls` onto the run, emits a `run_step` event, and
   checks the caps, the kill switch and its own deadline.
5. **Records why it stopped** (`status`, `stop_reason`) and releases the lease.

| Stop reason | Status | Resumable |
| --- | --- | --- |
| completed | succeeded | no |
| max_steps, max_tokens | failed | no |
| deadline | paused | yes |
| kill_switch, budget_exceeded, agent/department_disabled | paused | yes, once lifted |
| upstream_error | paused | yes |
| error | failed | no |

**Checkpoints are written synchronously** (`durability="sync"`). LangGraph's
default, `"async"`, saves a step's checkpoint while the next step is already
running. A crash can then lose that checkpoint, and the between-step read of
graph state can still see the previous one. That race was real: in CI it ended
a run as `completed` after its first step. `test_steps_wait_for_their_checkpoint_before_the_next_begins`
slows checkpoint writes to make the race certain, and guards the setting.

**Caps are checked between steps, never mid-step,** so a run always stops on
a step boundary with a consistent checkpoint. The token cap can therefore be
overshot by one step's calls; per-call `max_tokens` ceilings bound that.

**Each step must be safe to run twice.** A process that dies after a step's
database writes commit, but before LangGraph saves its checkpoint, re-runs
that step on resume. Facts are keyed by (run, claim) with a unique index, so a
re-run store step is a no-op. Model calls re-run, which costs money but has no
side effect. Irreversible actions go through the Step 6 approval queue, keyed
the same way.

**Identity.** Lifecycle bookkeeping (lease, status, rollup, events) runs as
`service_role`: it is the backend administering its own runs. The agent's
work inside each step runs as `runs.requested_by` under RLS, exactly as that
user's own requests would.

## Evidence (Milestone, 2026-09-21, real OpenRouter)

- **Kill and resume:** a child process was SIGKILLed after step 2. The run
  stayed `running` with its lease held. Once the lease lapsed, the next
  invocation resumed from the checkpoint and finished. The answer step ran
  once, and there were 2 model calls in total.
- **Caps:** `max_steps=2` stopped after step 2. `max_tokens=300` stopped at
  408 tokens: the one-step overshoot described above.
- **Budget:** under a department budget of $0.0029, a run's second call was
  refused before it was sent, pausing the run with `budget_exceeded`.
  Overshoot past the budget was $0.000052, one call's cost. Checking before
  each call can overshoot by at most the call that crosses the line.

## Consequences

- One invocation can end mid-run with `deadline`. Something must invoke
  again; that is Step 4's trigger mechanism. Until then,
  `scripts/agent.py` loops invocations itself.
- A lease is set to 300s, so a crashed run waits up to five minutes before it
  can resume. Shorter leases would resume sooner, but risk a slow step losing
  its lease to a second invocation.
