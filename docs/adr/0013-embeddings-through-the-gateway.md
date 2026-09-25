# ADR 013: Real embeddings, through the gateway, chosen in the database

- **Status:** Accepted (owner, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 6 (configuration backend)

## Context

Since Step 1 every fact has been embedded by `HashingEmbedder`, a free
word-hashing stand-in: it matches shared words but not meaning ("car" is not
near "automobile"). Step 6 adds documents, and the brain write gate (ADR 010)
relies on search to find a claim's neighbours. The plan said "embeddings via
the gateway" without naming a model. Options put to the owner: a real model
through OpenRouter (recommended), or keep the stand-in. The owner chose the
real model.

OpenRouter's embeddings API (`POST /embeddings`, read 2026-09-26) takes
`model`, `input` (a string or list) and optional `dimensions`, and returns
`data[].embedding` with `index`, and `usage.prompt_tokens` and `usage.cost`.
Its catalogue lists embedding models separately (`/embeddings/models`).

## Decision

- **Model:** `openai/text-embedding-3-small` via OpenRouter: 1536 dimensions
  (the width of our vector columns, requested explicitly), $0.02 per million
  tokens. A thousand one-paragraph facts cost well under a cent.
- **It is data.** A new `embedding` value in `model_tier_assignments`
  (org-wide, or per department), seeded for existing orgs and by
  `scripts.agent seed`, changed with `scripts.set_tier_model embedding <slug>`
  (checked against the embeddings catalogue). There is no environment
  fallback: with none assigned, embedding is refused.
- **`Gateway.embed`** applies the kill switch and budgets, logs one
  `model_calls` row with OpenRouter's reported cost and a `model_call` event,
  honours `sensitive` with zero-retention providers, and refuses vectors of the
  wrong width (after recording the cost, since it was paid).
- **`GatewayEmbedder`** implements the brain's `Embedder` for one agent and
  run, in batches of 64. Runs use it unless a test passes an embedder.
- **Never mix models.** Every embedded row records `embedding_model` (the
  assigned model name, which is stable; existing rows are `hashing-v1`).
  Search only compares rows embedded by the model in use. After a model
  change, `python -m scripts.brain reembed` moves old rows across; until then
  they are invisible to search, not wrongly ranked.

## Consequences

- Going live: after the migration, the live org's existing facts are
  invisible to search until `scripts.brain reembed` runs (a few cents at
  most). The research agent needs the OpenRouter key it already has.
- Every recall and every neighbour check in the write gate is now a small
  paid call, costed to the agent's department.
- OpenRouter now also serves TypeSafe's Jev (System One endpoints). ADR 009's
  choice of calling TypeSafe directly still stands; worth revisiting if one
  bill matters more than one fewer hop.
