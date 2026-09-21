# ADR 003: Model selection is runtime configuration, not deploy-time

- **Status:** Accepted; backend implemented, control-centre UI pending (Step 7)
- **Date:** 2026-09-21
- **Step:** 2 (Model gateway); backend pulled forward ahead of Step 3, UI in Step 7

## Context

Step 2 put the tier-to-model mapping in `MODEL_TIERS`, an environment
variable. Changing which model a tier resolves to therefore means editing
configuration in the Vercel dashboard and redeploying.

That defeats the reason OpenRouter was chosen. One endpoint, one request shape
and one credential exist precisely so that models can be swapped freely --
when a cheaper model appears, when one regresses, when a task turns out to
need more capability. Gating that on a deploy makes the swap something that
happens rarely and nervously, rather than routinely.

It is also at odds with how the rest of the system already works. An agent's
tier lives in `agents.model_tier`, a database column that can be changed at
any time. Only the other half of the mapping is frozen into the build.

## Decision

**The tier-to-model mapping belongs in the database, editable from the control
centre, taking effect on the next call with no redeploy.**

- An org-scoped table holds the mapping, one row per tier.
- Departments may override a tier, so one team can trial a model without
  moving everyone onto it.
- `MODEL_TIERS` survives as bootstrap only: it seeds an org that has no rows
  yet, and is the fallback if the table is unreachable. It stops being the
  place anyone edits.
- Every change writes an `events` row -- who changed which tier, from what to
  what, and when. A model change alters cost and behaviour, so it is an
  operational event, not a silent setting.
- The gateway reads the mapping per call, cached briefly. A model change
  should be visible in seconds, not on the next cold start.

**Explicit versioned slugs, never `~latest` aliases.** OpenRouter offers
aliases that always resolve to the newest model in a family. They are
rejected here for three reasons, each of which is a rule in `CLAUDE.md`:

1. Cost control is priority one. An alias can repoint to a more expensive
   model with no change on our side; budget enforcement would then catch the
   overspend after it happened.
2. Decisions must be auditable. Step 5 calibrates the judge against logged
   judgments; if the model changed underneath, those labelled examples
   describe two different models as one.
3. Prompts tuned against one model start running on its successor with no
   signal.

Upgrading becomes a deliberate edit in the control centre -- which, once this
is built, costs nothing.

## Why not just keep the environment variable

It works, and it is what Step 2 shipped. But every model change would need a
deploy, which means the cost review in Step 8 produces recommendations nobody
acts on quickly, and a model that starts failing in production cannot be
switched away from without a release.

## Consequences

- The control centre gains a model-management surface. Recorded in
  `docs/BUILD_PLAN.md` under Step 7.
- Values must be validated before they are saved. A typo in a slug would
  otherwise fail at the first model call rather than at the point of editing.
  OpenRouter's models endpoint can confirm a slug exists, and the control
  centre should offer the current catalogue rather than a free-text box.
- A cache means a window where two function instances disagree about which
  model a tier means. Acceptable: both are valid models, both are logged with
  the model actually used, and the window is seconds.
- `model_calls.model` already records what OpenRouter reports it served,
  rather than what was requested, so the ledger stays accurate across a change
  mid-flight.

## Status

Backend built before Step 3, at the owner's request, because agent tuning in
Step 3 is when model swaps become frequent. The control-centre screen is still
Step 7.

What shipped, and where it differs from the decision above:

- `model_tier_assignments` (migration `20260921030000`): org-scoped with RLS,
  optional `department_id` override, one row per scope and tier. A check
  constraint refuses `~` aliases, empty slugs and stray whitespace even for a
  write that bypasses the application.
- **No cache.** The gateway resolves the model inside the query that already
  loads the agent and its department, so a change costs no extra round trip
  and applies to the very next call. The cache and its disagreement window
  described under Consequences are therefore not needed.
- **Auditing is a database trigger**, not application code, so a change made
  by hand in the SQL editor is on the trail too. Payload: tier, department,
  from, to, `auth.uid()` and the database role.
- **No seeding.** An org with no row for a tier uses `MODEL_TIERS`, which is
  still required at startup so a missing tier fails loudly on boot.
- Slugs are checked against OpenRouter's public `/models` list by
  `app.gateway.model_admin.assign_model`. Until Step 7, the owner changes a
  model with `api/scripts/set_tier_model.py`, which uses it.
- `model_call` events now also carry `tier` and `requested_model`, beside the
  `model` OpenRouter reports serving.
