# ADR 027: The Chief of Staff routes orders; the brief writer ranks the morning

- **Status:** Accepted (owner's go on 8.2, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 8.2 (Executive Office and Chief of Staff; right-hand ideas 4, 5 and 6)

## Context

The owner needs one place to give orders at any time, and one brief each
morning that leads with what needs them. The `router` and `digest` runners
were reserved in ADR 020 for exactly this.

## Decision

**The Executive Office is a charter** (ADR 024), no longer a draft: the
Chief of Staff (`router`, role `chief_of_staff`) and the brief writer
(`digest`), cheap tier, $0.10 a day, brief at 07:15 New York time Monday to
Saturday, after Research's 06:30.

**Orders.** `POST /orders` and `python -m scripts.order "..."` give the Chief
of Staff an order in plain words (a task). Its `router` runner makes no model
loop; it decides with Jev:
- the options are the departments that can take work now (a live, final
  charter whose head is switched on), described by their charters' purpose,
  plus `owner`; a Score of complexity suggests a tier (settings);
- the order is checked against the owner's decisions and standing rules
  (idea 4, `owner_conflict`);
- a clear routing becomes a sub-task for that department's head, and the
  order's result is the department's when it finishes;
- an unclear one, one only the owner can settle, or one that goes against
  an owner decision comes back as a question (idea 5): an approval card
  with the likeliest departments. Approve takes the suggestion, an edited
  `department` picks another, cancel stops it, a redirect's note is routed
  again (at most three questions).

**The morning brief.** The `digest` runner gathers, from the database alone,
what happened since the last brief: finished orders and routines, approvals
waiting (the approvals digest), runs that failed or stopped, facts by
outcome, spend per department. Jev scores each item (`brief_rank`: urgency,
impact, needs the owner); weighted by settings, the top five lead (idea 6).
One short model call writes the brief from the ranked items with the brief
writer's `brief` prompt (database).

## Consequences

- An order costs one Jev request plus at most three conflict checks; a
  brief costs one Jev request per item (at most 15, a setting) and one cheap
  model call.
- The suggested tier is recorded on the department's task; using it to pick
  the model for that task is a later change.
- Email delivery of the brief waits for the owner to connect Resend (MCP,
  Step 7.7) and choose the rules.
