# ADR 022: The autonomy ladder and safety limits

- **Status:** Accepted
- **Date:** 2026-09-26
- **Step:** 7.6 (autonomy and safety limits; right-hand idea 2)

## Context

Step 7.5 made held actions work. Step 7.6 decides what each agent may do
without asking, and adds the limits that stop a task tree from running away:
repeating itself, getting stuck, flooding an agent with work, or carrying on
after the owner hits the kill switch.

## Decision

**The ladder is data.** `agents.autonomy_level` (L0 to L3) is set only by the
owner (agent sessions cannot write agents). What a level means for each risk
class is `public.tool_mode(org, level, risk)`: an owner row in
`autonomy_rules` wins, otherwise the defaults:

| Level | R0 read internal | R1 write internal | R2 read outside | R3 reversible outside effect |
| --- | --- | --- | --- | --- |
| L0 draft only | run | hold | hold | hold |
| L1 default | run | run | gate | hold |
| L2 | run | run | gate | gate |
| L3 | run | run | run | gate |

`gate` means the tool-risk gate (ADR 021) decides: run, hold, or refuse. R4
is always held and cannot be configured otherwise (the table's check
constraint, the function, and the runtime each refuse it). Rule changes are
audited events; agent sessions cannot write rules or limits.

**Promotions are suggested, never made** (idea 2). `autonomy_suggestions`
gives, per agent and kind of action, how often Jev's recommendation matched
the owner, and marks it eligible when there are at least
`promotion_min_decisions` (30) with at least `promotion_min_agreement` (95%)
agreement, the action is a reversible tool (not R4), and the agent is below
L3. `GET /autonomy/suggestions`; `POST /agents/{name}/autonomy` to act on one.

**Limits live in `delegation_limits`** (one row per org, `org_limits()` gives
defaults): the ADR 019 limits plus `max_tasks_per_agent_per_hour` (20),
`loop_repeat_limit` (3), `stuck_task_minutes` (60) and the promotion bar.

- **Rate:** the task guard refuses a task when its agent has been given, or
  (as a head) has created, the hourly limit in the last hour.
- **Loops:** between steps the run counts the tool calls the model has asked
  for; the same tool with the same arguments `loop_repeat_limit` times fails
  the run as `loop_detected`, before the repeat runs.
- **Stuck tasks:** `reap_stuck_tasks()`, first in every tick, fails a
  `running` task that has not changed for `stuck_task_minutes` when none of
  its runs can progress (no lease, no wakes left, not waiting on the owner, a
  switch, a budget, or a prompt). Its parent then wakes and sees why.
- **Kill switch:** it already refused model calls, new runs and new tasks.
  Now nothing wakes while it is on either, and a run whose task was
  cancelled stops at its next step. After the switch is turned off, runs it
  paused stay paused (ADR 008's rule) until the owner resumes them with one
  call, `resume_paused_runs` (`POST /runs/resume`); each carries on from its
  checkpoint.

## Consequences

- The default L1 means every R3 action waits for the owner until the owner
  promotes the agent. That is deliberate while there is no history.
- Loop detection sees the agent's own conversation, not a deepagents
  helper's; a helper is bounded by its parent run's step and token caps.
- CLI: `python -m scripts.approvals level | suggest | resume`.
