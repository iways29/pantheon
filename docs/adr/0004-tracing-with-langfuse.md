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

## How it is wired (Step 3)

- The Python SDK is configured with `LANGFUSE_PUBLIC_KEY`,
  `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL`. These are server-side only,
  listed in `.env.example`, and must be set in Vercel as well.
- LangGraph nodes are traced through the LangChain callback handler.
- The gateway calls OpenRouter over plain `httpx`, not through a LangChain
  chat model, so the callback handler does not see model calls. The gateway
  opens its own generation observation instead, recording model, tokens and
  OpenRouter's reported cost.
- **Sensitive calls are masked.** Tracing sends prompt text to a third party.
  A `mask` function on the Langfuse client replaces the input and output of
  any call made with `sensitive=True`, leaving only metadata. CLAUDE.md's rule
  that sensitive data goes only to no-retention providers covers observability
  vendors too.
- **Infrastructure spans are filtered.** The v3+ SDK is OpenTelemetry-native
  and will also collect spans from other instrumented libraries (HTTP clients,
  database drivers). Each of those spends units. Only Langfuse's own and
  LangChain's instrumentation scopes are exported.
- Serverless functions must flush before returning, or buffered spans are lost
  when the instance freezes. The run lifecycle flushes at the end of each step.

## Consequences

- The owner creates a Langfuse Cloud account and project by hand, and picks
  the region, EU or US. Pick whichever is closer to the Supabase region.
- Postgres stays the source of truth for cost and audit. Traces expire after
  30 days on Hobby; `model_calls`, `events` and `runs` do not.
- If volume outgrows Hobby, Core is $29/month flat for 100k units with 90-day
  retention. That is a deliberate decision to take then, not an automatic one.
