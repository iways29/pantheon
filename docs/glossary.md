# What the words mean

Plain-English meanings of the statuses and reasons Pantheon shows: in the
database, the command-line tools, the morning brief and, later, the Control
Center. Nothing here is a setting; it explains what you see.

## Tasks: a piece of work given to an agent

| Status | Means |
| --- | --- |
| `queued` | Waiting for the scheduler to start it (within a minute). |
| `running` | An agent is working on it. |
| `blocked` | **Waiting on its team.** The agent handed parts of the work to other agents and steps aside, spending nothing, until they finish. It wakes up by itself with their results. Normal, not an error. |
| `awaiting_approval` | Waiting for **you**: the agent wants to do something that needs a person's yes. See `scripts.approvals list`. |
| `done` | Finished. The result is on the task. |
| `failed` | Stopped with an error; the reason is on the task and in the brief. |
| `cancelled` | Stopped for good by a person (or the kill). |

A task that is still `queued`, `running` or `blocked` when the 07:15 brief is
written appears in the brief as **"Not finished yet"**.

## Runs: one agent's turn at a task

A task may take several runs: an agent works, stops to wait, and is woken.

| Status | Means |
| --- | --- |
| `pending` | Created; the scheduler starts it within a minute. |
| `running` | Working now. Only one copy can run at a time (a "lease" of 300 seconds). |
| `paused` | Stopped for a reason below and will usually carry on. |
| `succeeded` | Its part is done. |
| `failed` | Its part could not be done (reason below). |
| `cancelled` | Its task was cancelled. |

### Why a run paused

| Reason | Means | What happens next |
| --- | --- | --- |
| `deadline` | It used its time slot (180 seconds) and saved its place. | Carries on automatically within a minute. |
| `awaiting_approval` | An action is held for you. | Carries on once you approve or reject. |
| `approved` / `redirected` | You decided on a held action. | Carries on automatically. |
| `upstream_error` | The model provider was busy or failed (for example a 429 rate limit). | Retried automatically, up to 5 times, 45 seconds apart. |
| `kill_switch` | The pause switch is on. | Waits until you resume (`scripts.approvals resume`). |
| `budget_exceeded` | The department spent its daily budget. | Waits for tomorrow's budget or a raise. |
| `agent_disabled` / `department_disabled` | Switched off. | Waits until switched on. |
| `prompt_missing` | The agent has no instructions for this step. | Needs a fix in its charter. |

### Why a run failed

| Reason | Means |
| --- | --- |
| `max_steps` / `max_tokens` | Hit its per-run cap (a safety limit on every run). |
| `loop_detected` | Asked for the same action with the same details three times; stopped so it cannot spend in a loop. |
| `stuck` | Made no progress for too long; the cleanup job ("reaper") stopped it. |
| `error` | Something broke; the message says what. |

## Approvals: the decision desk

| Status | Means |
| --- | --- |
| `pending` | Waiting for you. Each card has what the agent wants to do, why it was held, and a recommendation (`approve`, `reject`, or `look_closer`). |
| `approved` / `rejected` | You decided. A decision with a note is remembered in the brain. |
| `expired` | Cancelled by the kill. |

Why something is held: every agent action has a **risk class**, R0 (reads
the company's own data) to R4 (spends money). Each agent has an **autonomy
level**, L0 (asks for almost everything) to L3 (reads the outside world on
its own). The level and the class together decide whether an action runs,
is checked first by Jev (the **risk check**), or always waits for you.
Spending (R4) and anything that sends or publishes always waits for you.

## Facts: what the brain knows

| Word | Means |
| --- | --- |
| `accepted` | Passed the fact check and stored. |
| `review` / held | The check was unsure; waits for you (`scripts.brain held`, then `admit` or `reject`). |
| `rejected` | Failed the check (not a clear fact, not supported by its source, or unsafe). |
| `duplicate` | Already known; skipped. |
| `disputed` | Stored, but contradicts another fact; the curator flags it. |
| `superseded` | Replaced by a newer fact; kept for the record. |
| `internal` / `public` | Only public facts may appear in content. Only a person makes a fact public. |

## Drafts: Marketing's content

| Status | Means |
| --- | --- |
| `draft` | Written, not yet checked. |
| `blocked` | A check failed (an unsupported claim, a banned word, the voice); back to the editor with the sentence to fix. |
| `ready` | Passed; waiting on your approval card. |
| `approved` / `rejected` | You decided. Your edits are kept as examples of your voice. |
| `published` | You posted it by hand and marked it (`scripts.draft posted`). |

## Page reads

| Word | Means |
| --- | --- |
| `clean` | Screened; safe for agents to read. |
| `review` | Screening was unsure (for example the page contains an email address). |
| `quarantined` | Looks like it tries to instruct an AI; no agent reads it. |
