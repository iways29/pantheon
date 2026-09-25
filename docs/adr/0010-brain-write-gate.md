# ADR 010: No fact enters the brain unjudged

- **Status:** Accepted
- **Date:** 2026-09-25
- **Step:** 5.2 (brain write gate)

## Context

Until now the research agent wrote every claim it extracted straight into
`facts`. The brain is the shared truth every agent reads, so one bad write
spreads. Step 5.2 puts TypeSafe's citation-check and entity-alignment
patterns in front of every write, with thresholds held as data (ADR 009).

## Decision

- **One path in:** `BrainWriter.propose(FactCandidate)` in
  `app/brain/write_gate.py`. `Brain.insert_fact` now requires an
  `Admission`, and a database trigger refuses any new fact whose
  `admitted_by` does not name a real `brain_claim` judgment in the same org.
  Skipping the gate leaves no judgment to name, so there is no bypass.
- **Code first, free:** length limit; a source is named; there is evidence
  (source text or a quote); a quote must appear in the source text or the
  claim is rejected as *fabricated*; every number in the claim must appear in
  the evidence (Jev is weak at numbers); an exact duplicate (same words,
  ignoring case, spacing and trailing punctuation) is skipped. None of these
  costs a model call.
- **One Jev request per claim** (gate `brain_claim`): six questions over
  `{claim, source, evidence}`: standalone, opinion or hedge, secret or
  personal data, instruction aimed at an AI, likely to change, and a
  Choice for how the evidence relates (`supports`, `contradicts`,
  `says_nothing`). A long source is cut in code to the sentences nearest
  the claim.
- **Neighbours** (gate `brain_neighbour`): the nearest active or disputed
  facts (3, within a distance setting), one request each: a three-level
  sameness Score, and Nouls for "contradicts" and "gives a newer value".
- **Decision in code**, in this order: any uncertainty → **review**; a same
  fact → **duplicate** (skipped); a newer value for exactly one fact →
  **superseded** (old fact marked `superseded_by`); a contradiction →
  **disputed** (stored as `disputed`, so it is not recalled as believed; the
  existing fact stands); otherwise **accepted**. The claim gate's own
  `reject` and `review` come first.
- **Review is the approval queue.** A held claim is a pending `approvals`
  row (`action_type = 'fact_write'`) with the proposal and a snapshot of the
  source text, keyed by run and claim so a retried step queues nothing new.
  Step 7.5 adds approve and reject.
- **Fail closed.** No answer from TypeSafe on the claim gate = rejected; on a
  neighbour = review. Sensitive claims never go to TypeSafe (ADR 009); they
  go to review.
- **New fact fields:** `admitted_by`, `review_after` (set from the
  `volatile` answer and the gate's `review_after_days`), `visibility`
  (`internal` by default; `public` must be granted deliberately), `quote`.
- **The research agent** proposes each claim with its own answer as the
  evidence. Without a TypeSafe key a run still answers but stores nothing
  (`fact_writes` shows `not_judged`). A missing gate does the same rather
  than failing the run. Every decision is a `fact_write_decided` event.
- **Starter gates are seed data** (`app/judge/starter_gates.py`), published
  once per org by `scripts.agent seed` and never overwritten. The thresholds
  are a first guess until Step 5.4 measures them.

## Consequences

- A fact costs one Jev call plus up to three more for neighbours: about
  4 × 400 tokens × $0.042/M ≈ $0.00007 per claim.
- The live Pantheon org needs the migrations applied, then
  `scripts.agent seed` run once to publish the two brain gates.
- Facts written before this step have no `admitted_by`; they stay as they are.
- Found while testing: deleting an org fails when it has a model tier
  assignment, because that audit trigger writes an event for the org being
  deleted. The new price audit trigger avoids it; the existing one is a
  separate fix.
