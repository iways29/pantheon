# ADR 031: Jev checks the workers' results; a failed check redoes the task on a stronger tier

- **Status:** Proposed (owner chose "worker results" first, 2026-09-27)
- **Date:** 2026-09-27
- **Step:** 9, part 1 (right-hand idea 7)

## Context

Most work is done by cheap-tier workers (web-researchers, topic researcher,
editor). Live cost is tiny (about $0.11 over three days, 2026-09-27), so Step
9's value is mostly catching bad work, with the cost comparison as proof that
checking is cheaper than always paying for the standard tier.

## Decision

**Check every worker's finished task.** When a worker's deep run succeeds on
a task, the `result_check` gate asks Jev four narrow questions before the
task finishes: the result gives no answer; it leaves out what the task asked;
it names a company, person, amount or date that is nowhere in the evidence;
the evidence contradicts it. The evidence is what the run's own tools
returned (`tool_calls`), up to `evidence_chars` (24,000). Heads are not
checked: their workers' results are.

**A `redo` sends the task back once, one tier up.** Allowed when the run's
tier is in `from_tiers` (`cheap`), a tier up to `max_tier` (`standard`)
exists and fewer than `max_escalations` (1) redos happened. The task goes back
to the queue with `model_tier` set and the first attempt and its problems in
its input; the run ends `succeeded` with stop reason `escalated`, and the
task-follows-run trigger leaves the task alone. The redo's run carries the
tier (`runs.model_tier`), and the gateway uses it for every call of that
run, upward only, with budgets and the pause switch as usual. A result still
flagged with no redo left finishes with `check.problems` on it.

**All of it is data.** Questions, thresholds (0.6 each) and settings live in
the gate (`scripts.judge`). Only the backend may set a task's or run's tier
or its escalation count (a trigger refuses signed-in callers), so an agent
cannot move itself to a dearer model.

**Fail open.** A check that cannot run (Jev down, gate missing) never stops
the work. Every check is a judgment (raw answers) and a `result_checked`
event, which doubles as the run's quality score.

**Measurement.** `scripts.cascade report` counts checks, first-time passes,
redos, results still flagged, and splits cost into first attempts, redos and
checks, with the observed redo-to-first-attempt cost ratio and an estimate of
always using the standard tier. `evals/cases/result_check.json` (14 cases) is
the labelled set for the gate's accuracy (`scripts.judge_eval run
result_check`).

## Consequences

- Each check costs about $0.0002 in Jev fees; a redo costs one standard-tier
  run (about 1 to 3 cents for a web-researcher).
- A false alarm costs a redo, never lost work: the first attempt's facts are
  already in the brain, and the redo's go through the same write gate.
- Not yet (Step 9, next parts): re-ranking recalled facts before an agent
  reads them, and a side-by-side run of cheap, cascade and standard on the
  same labelled tasks for the Step 9 report.
