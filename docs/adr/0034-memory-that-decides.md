# ADR 034: Memory that decides for itself, and the owner's preferences

- **Status:** Proposed (owner, 2026-09-27)
- **Date:** 2026-09-27
- **Step:** after 9; piece 1 of the brain plan (piece 2 is the graph, ADR 035)

## Context

ADR 033 made the brain grow with use: the owner's orders, questions and
approved drafts became dated facts. The owner wants more: when they say
"call me boss", the agents should learn it and follow it, and the brain
should decide by itself what is worth keeping.

## Decision

**Jev sorts what the owner says** (`memory_triage`, one Choice question):
`preference`, `rule`, `interest`, `fact`, `style`, or `forget`. Sorted:
orders and research questions (approved drafts stay "something the owner
did"). `forget` is dropped and the owner is never asked. Below
`min_probability` (0.5) for its best kind, it is kept as an `interest`, so
the owner's words are never lost to an unsure sort.

**Facts carry a kind** (`facts.kind`). Each memory is stored through the
write gate with the owner's words as its own evidence, judged by the
`brain_claim` gate's new **`owner` profile**: only secrets are refused, no
opinion or instruction checks (a preference is both), and nothing is held
for the owner to review. The neighbour check still runs, so a newer
preference that replaces an older one supersedes it.

**Every agent reads the active preferences** (at most `max_preferences`,
20) after its own instructions: deep agents and the brief writer. "Call me
boss" is followed from the next run.

**When:** the brief writer each morning, and the Chief of Staff when it takes
an order, so a preference in an order applies within a minute. Each item is
kept once.

## Consequences

- About $0.0001 per thing the owner says.
- Preferences are visible and removable (a Control Center screen, Step 11;
  until then `scripts.brain` or SQL).
- Draft edits as "how the owner writes" examples, and the chat window
  (Step 10), plug into the same sort.
- 14 labelled cases for the gate (`scripts.judge_eval run memory_triage`).
