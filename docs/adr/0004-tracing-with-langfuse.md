# ADR 004: Tracing with Langfuse

- **Status:** Accepted
- **Date:** 2026-09-20
- **Step:** 3 (First agent end to end); resolves Open decision 2

## Context

Step 3 needs tracing: a step-by-step view of each run for debugging, beside
the cost and audit records Postgres already keeps. The build plan left the
choice between LangSmith and Langfuse open.

Pricing as published on 2026-09-20:

| | Langfuse Cloud Hobby | LangSmith Developer |
| --- | --- | --- |
| Price | Free | Free |
| Included | 50k units/month | 5k traces/month |
| Retention | 30 days | 14 days |
| Seats | 2 | 1 |
| Past the limit | No overage price; not billable | Pay-as-you-go |
| Next tier | Core, $29/month flat | Plus, $39/seat/month |

A Langfuse unit is any trace, observation or score, so one agent run costs
several units. A run of about ten steps is roughly 10-25 units, so the free
tier covers about 2,000-5,000 runs a month. That is comparable to LangSmith's
5k runs, and more than phase 1 is expected to need.

## Decision

**Langfuse Cloud on the free Hobby tier.**

1. **Cost control.** Hobby has no overage price and needs no card, so
   tracing cannot turn into an unplanned line item. What happens to ingestion
   at the 50k limit is not documented on the pricing page; check it once the
   account exists, and watch usage in the Langfuse dashboard.
2. **No lock-in.** Langfuse is open source and can be self-hosted. That is
   out of scope for phase 1, but later tenants with data-residency needs have
   somewhere to go without re-instrumenting.
3. **It covers our stack.** Langfuse documents integrations for LangGraph,
   LangChain deepagents and TypeSafe Jev, which is our agents runtime and
   our judge.

What LangSmith would have given us, and what we give up: tracing that works
from environment variables alone with no code, and LangGraph Studio. For us
that saves about half a day of instrumentation, and neither is worth a
per-seat bill.

## How it is wired

Built with the Langfuse agent skill (vendored in `.claude/skills/langfuse`) and
Python SDK `langfuse` 4.15.4, against the SDK docs and best-practices guide as
published on 2026-09-20. All of it lives in `api/app/tracing.py`; nothing else
imports the SDK.

- **Off without keys.** `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
  `LANGFUSE_BASE_URL` are server-side only and must also be set in Vercel.
  With no keys, a no-op tracer stands in, so tests and local work need no
  account.
- **One `generation` per model call**, named `call-model`. The gateway calls
  OpenRouter over plain `httpx`, so no framework integration can see these
  calls. It records the served model, `input`/`output` token usage, and
  OpenRouter's `usage.cost`, ingested as `cost_details.total`. Langfuse cannot
  price OpenRouter slugs itself, and this way the trace matches `model_calls`.
- **A refused call is a `guardrail`**, named `enforce-call-gates`, with the
  refusal code. An upstream failure marks the generation `ERROR`.
- **One trace per run.** Calls carrying a `run_id` get a trace id seeded from
  it, so a run resumed in a later serverless invocation joins the same trace.
  When an observation is already open, as Step 3's run observation will be,
  calls nest under it instead.
- **Tags** are `department:<name>` and `tier:<tier>`; **metadata** holds ids,
  the agent name and the requested model. `environment` comes from settings
  and `release` from `VERCEL_GIT_COMMIT_SHA`. Names are stable and never
  include a model, so a model swap (ADR 003) breaks no filter or evaluator.
- **Sensitive calls never send their text.** This differs from the plan
  above. Instead of masking at export, the gateway never hands the prompt or
  completion to the SDK; the observation says it was withheld and still
  carries usage and cost. Data that is never sent cannot leak if a mask
  function has a bug.
- **Infrastructure spans:** SDK v4 exports only Langfuse and GenAI spans by
  default, so HTTP and database spans stay out without any filter of ours.
- **Flushing.** Scripts call `flush()`. No request path traces anything yet.
  Step 3's run lifecycle must flush at the end of each step, before the
  function returns, or buffered spans can be lost when the instance freezes.
- **Verification.** `tests/test_tracing.py` asserts on the spans the real SDK
  exports, using an in-memory exporter. `api/scripts/trace_smoke.py` sends a
  real trace for the audit the skill requires.

Still to do in Step 3: trace LangGraph nodes with the LangChain
`CallbackHandler` under a root `agent` observation per run, set `user_id` to
the owner, and apply the same sensitive-call rule to anything the callback
captures.

## Consequences

- The owner creates a Langfuse Cloud account and project by hand, and picks
  the region, EU or US. Pick whichever is closer to the Supabase region.
- Postgres stays the source of truth for cost and audit. Traces expire after
  30 days on Hobby; `model_calls`, `events` and `runs` do not.
- If volume outgrows Hobby, Core is $29/month flat for 100k units with 90-day
  retention. That is a deliberate decision to take then, not an automatic one.
