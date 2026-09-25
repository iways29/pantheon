# ADR 020: An agent is a row plus a runner; deepagents runs heads and open-ended work

- **Status:** Accepted (completes owner decision 5, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 7.4 (agent definitions and runners)

## Context

ADR 019 made the chain of command durable tasks. Something has to *do* each
task: plan it (a head), or carry it out (a worker). The plan names four
runners; deepagents is in the locked stack for open-ended loops.

## Decision

- **`agents` gains** `role_type` (ADR 019), `runner` (`pipeline`, `deep`,
  `router`, `digest`; default `pipeline`), `autonomy_level` (`L0` to `L3`,
  default `L1`, owner-set; used in 7.5 and 7.6) and `max_children` (a tighter
  cap than the org's, honoured by the task guard). All set through the
  Step 6 agent API and audited.
- **`pipeline`** is the existing research graph. A task given to it carries
  its question as the task's instructions.
- **`deep`** (`app/agents/deep.py`) is `create_deep_agent` with:
  `SessionChatModel` (every turn through the gateway, each in its own
  transaction); the agent's allowed tools, each call through `ToolRuntime`;
  deepagents' `StateBackend`, so its file tools are a scratchpad inside the
  run's state and there is no disk and no shell; our Postgres checkpointer;
  one `system` prompt from the database. Its `task` tool gives a worker a
  temporary helper inside the same run: not an agent row, costed to the
  worker.
- **A head** is `role_type = head` on the `deep` runner with `create_task`
  and `report_result`. Its first message lists the task, its team (its
  reports, or its department's workers) and, on a later run, its
  sub-tasks' results, marked as data. It delegates and stops; ADR 019 wakes
  it. **A worker** does one task and records a short result.
- **`router` and `digest`** are named but not built: a run on them fails
  with `runner_not_built`. They arrive with the Chief of Staff and the
  morning brief (Step 8.2).
- **deepagents 0.7.19** is now a dependency (+22 MB installed; about 126 MB
  in all, under Vercel's 250 MB). It brings the Anthropic and Google SDKs,
  which stay unused: no agent code holds a provider client.
- Starter `system` prompts for heads and workers are seed data in
  `starter_prompts.py`.

## Consequences

- A head costs about as much as its planning and reporting turns; each
  worker its own loop. The whole tree's cost is `task_costs.tree_cost_usd`.
- Proven end to end in `tests/test_runners.py`: a head and two workers,
  across four separate invocations, with a scripted model.
- Human approval mid-loop (deepagents `interrupt_on`) is Step 7.5.
