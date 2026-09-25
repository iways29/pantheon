# ADR 021: Held actions pause the run; the owner decides at a decision desk

- **Status:** Accepted
- **Date:** 2026-09-26
- **Step:** 7.5 (approvals and the tool-risk gate; right-hand ideas 1, 2 and 4)

## Context

ADR 018 could hold an R4 tool call as a pending approval, but nothing came
of the owner's decision: the agent just read "held" and moved on. Step 7.5
needs a held action to stop its run and task, and the owner's approve,
edit or reject to resume or cancel it. The owner also asked for Jev as a
right hand: every held action should arrive with a recommendation, the
facts behind it, how similar actions were decided, and any clash with the
owner's earlier decisions.

## Decision

**Pausing and resuming.** A held call raises `ApprovalPending` out of the
tool (after its own transaction commits the held `tool_calls` row, the
approval and the task's `awaiting_approval`). The run stops as `paused` /
`awaiting_approval`; the LangGraph checkpoint is the model's turn that asked
for the call. `public.decide_approval` (one transaction, people only) moves
the approval, the held call, the task and the run together and writes an
`approval_decided` event. The scheduler's `pending_wakeups` then wakes the
run; the tools node runs again and the call **replays by its idempotency
key**:

- approved: the tool runs now, once, with the approved (possibly edited)
  arguments, and its row becomes `ok`;
- redirect: the agent reads "the owner rejected this: <note>" and carries on;
- cancel: the task and run are cancelled and never wake.

The model is not asked again for the held call. A run whose task is
cancelled stops itself at its next step.

The plan named deepagents' `interrupt_on` with `Command(resume=...)`. We use
the idempotent replay instead because it (1) works for every runner, not
only deepagents; (2) needs no process to stay alive, only the checkpoint
already written; (3) reuses the one idempotency mechanism that already
guarantees "at most once", so a crash after approval cannot run the action
twice. Same outcome, one fewer mechanism.

**Only people decide.** `decide_approval` refuses an agent's session, and a
trigger stops an agent's session from changing an approval's decision or
moving, or editing, a held call, even by a direct UPDATE.

**The tool-risk gate** (`tool_risk`, Jev) runs before every R2 and R3 call.
Five Nouls, each phrased so "yes" means risk: irreversible, external
effect, spends money, off task, sensitive content. Outcomes `auto`, `ask`
(held for the owner), `block` (refused). Thresholds scale with risk through
profiles: an R2 read asks at 0.3, an R3 action at 0.15. An unanswered check
asks; without TypeSafe an R3 call asks and an R2 read runs (it is still
screened afterwards). R0 and R1 are not checked (internal, with their own
gates); R4 is always held.

**The decision desk** (`app/approvals/desk.py`), built once when a call is
held and stored on the approval:

- `facts_checked`: the three nearest brain facts;
- `similar_decisions`: the owner's last five decisions on the same
  `action_key` (e.g. `tool:publish_post`);
- `recommendation` (approve, reject, look_closer), with probabilities and
  the judgment's request id, from the `approval_recommend` gate. "Approve"
  below 0.7 becomes look_closer. Without an answer it is look_closer;
- `conflicts`: owner facts (source `owner`) that the `owner_conflict` gate
  says this action goes against (or is unsure about);
- `explanation`: a few sentences from the agent's own `explain` prompt
  (in the database like every prompt). No prompt, no model call.

**Agreement** (idea 2): the `approval_agreement` view gives, per
`action_key`, decisions, recommendations and how often they matched. Step 7.6
reads it to suggest (never make) autonomy promotions.

**Decisions become facts** (idea 4): a decision with a note, and every
standing rule (`POST /policies`), is proposed to the brain through the write
gate with source `owner`. Decisions without a note are not written: they
say nothing a later check could use, and each would cost a judgment.

## Consequences

- Every approval row is a labelled example: the frozen output snapshot, the
  recommendation, the decision and any edit (Step 5.4 calibration).
- A held call costs about 2 to 5 Jev questions (recommendation, up to three
  conflict checks) and at most one short cheap-model call. An R2 or R3 call
  costs one Jev request of five questions.
- A helper started with deepagents' `task` tool that hits a held call is
  re-run from its start on resume (its tool calls replay; its model turns
  are paid again). Acceptable for short helpers.
- Endpoints: `GET /approvals`, `POST /approvals/{id}/approve` (optional
  edited arguments), `POST /approvals/{id}/reject` (cancel or redirect with a
  note), `POST /policies`. CLI: `python -m scripts.approvals`.
