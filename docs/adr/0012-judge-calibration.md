# ADR 012: Thresholds come from labelled cases; models move only after a re-run

- **Status:** Accepted (harness built; first live run pending the TypeSafe key in a session)
- **Date:** 2026-09-25
- **Step:** 5.4 (calibration and the labelled set)

## Context

The starter thresholds (ADRs 010, 011) are considered guesses. TypeSafe's
own advice and our research note agree that cookbook numbers are examples,
not defaults, and that the model behind an alias can change. We need a
cheap, repeatable way to measure each gate and to justify every change.

## Decision

- **`judge_cases`**: per gate, a stable case key, the state, the expected
  outcome (checked against the gate's live outcomes on import), where the
  label came from (`owner`, `approval`, `ensemble`, `author`) and an optional
  weak-spot tag (`arithmetic`, `dates`, `double_negative`, `adversarial`).
  Starting sets are files in `api/evals/cases/` (42 cases over
  `brain_claim`, `brain_neighbour`, `content_screen`), labelled `author`.
- **`scripts.judge_eval run <gate|all> [--repeats N]`** asks the live gate
  about every case through the gateway (costed, logged as `eval:` judgments)
  and reports: accuracy; precision, recall and F1 per outcome and for
  "flagged"; accuracy by certainty band; self-consistency across repeats;
  accuracy on each weak spot; the cases it got wrong; cost per 1,000
  judgments; latency p50 and p95. Each run is a `judge_eval_runs` row.
- **Threshold sweeps cost nothing.** Every numeric threshold is varied alone
  over a grid and the stored answers are decided again. A value is
  recommended only if it beats the current one by at least 0.02 macro F1.
  The script only recommends. The owner changes a threshold with
  `scripts.judge gate set`, which versions and audits it.
- **Model-upgrade rule.** Gates pin `jev-1.13.0` (aliases are refused by the
  database). To move: publish a gate version with the new model, run
  `judge_eval run all --repeats 3`, compare with the last run in
  `judge_eval_runs`, record the result in an ADR, then keep it or roll back
  (`scripts.judge gate activate`).
- **Where labels come from is still open (decision 14).** The frontier-model
  labeller is not built: it needs a labelling prompt, which must live in the
  database, and it spends money, so it waits for the owner's choice.

## Consequences

- From the request sizes of the starting cases, a judgment costs about
  $0.009 (`brain_neighbour`) to $0.022 (`brain_claim`) per 1,000. A full eval
  run of all three gates, three repeats, is about $0.002.
- The sweep holds the other thresholds fixed, so it can miss changes that
  only help in combination. With 42 hand-written cases, results are an
  early signal, not a verdict; the set should grow from real approvals.
- The weak-spot cases for dates and numbers measure Jev alone. In production
  the brain gate's code checks catch most of them before Jev is asked.
