# ADR 032: Jev re-ranks recalled facts, and the side-by-side comparison

- **Status:** Proposed (Step 9 parts 2 and 3, 2026-09-27)
- **Date:** 2026-09-27
- **Step:** 9 (right-hand idea 7)

## Context

Step 9 asks for three things beyond the result check (ADR 031): re-rank
recalled facts before an agent reads them; score runs for quality; and prove,
on a labelled set, that checking cheap work costs less than always using the
standard tier with no drop in quality.

## Decision

**Re-rank in `brain_search`.** When the `recall_rank` gate exists, the tool
fetches twice the asked-for number of candidates (at most 20) and Jev judges
each against the query: `relevance` (a Score: unrelated, related, answers it)
and `contradicts_premise` (a Noul). Facts scoring under 0.5 are dropped; the
rest are ordered by relevance, less 0.5 for a fact past its `review_after`
date (decided in code: Jev does no date comparison). The agent gets the
number it asked for, each marked `relevance`, and `stale` or
`contradicts_premise` where they apply. A contradicting fact is kept and
flagged, never hidden: it is the most useful thing to know. Fail open: no
gate, no judge or a failed judgment gives the plain vector order. Settings
(`candidates_factor`, `max_candidates`, `stale_penalty`, `premise_flag_at`)
are gate data. About 8 searches a day at up to 20 facts each: well under a
cent.

**Quality scores** are the `result_checked` events (ADR 031), one per worker
result, with raw answers in `judgments`.

**The comparison** (`scripts.cascade compare`). Each labelled case
(`evals/cascade/scouting.json`: a task, the pages, `gold` strings a good
answer holds, `traps` a wrong one holds) is answered once on cheap and once
on standard from the same pages, with no tools. The cascade is the cheap
answer, checked by the live `result_check` gate with the pages as evidence,
replaced by the standard answer on a `redo`. Quality is scored in code
(share of gold found, less half a point per trap), never by Jev, so the
judge does not mark its own work. Calls run under a `benchmark` agent (cheap
tier) in its own `benchmarks` department ($0.50/day), so the test is costed,
capped and never spends a live department's budget. The report prints
quality and cost per task for the three strategies and the verdict against
Step 9's "done when". About 3 cents for the 8 starting cases.

## Consequences

- Both gates are data: merged code does nothing until `scripts.judge seed
  result_check` or `scripts.judge seed recall_rank` adds a gate, and
  `scripts.judge gate off <gate>` turns one off again (`gate on` back), keeping
  its settings.
- 8 cases is a small set. Real worker tasks with the owner's labels should
  replace the author's cases as they accumulate.
- The benchmark agent's prompt is a starter prompt in the database, editable
  like any other.
