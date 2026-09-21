# ADR 002: Departments hold the budget, not agents

- **Status:** Accepted
- **Date:** 2026-09-21
- **Step:** 2 (Model gateway)

## Context

`docs/BUILD_PLAN.md` Step 2 specifies per-agent spend control, with the daily
limit read from `agents.daily_budget_usd`. Step 2 was built that way, and the
acceptance criteria passed against it.

Two things then pushed against that shape.

Open decision 3 in the build plan asks whether to enforce budgets in the
application or through OpenRouter's per-key limits. OpenRouter does support
them: a per-key `limit` with a `daily`, `weekly` or `monthly` reset, managed
through a provisioning key. But a key per agent is key sprawl -- every agent
needs a secret created, stored, rotated and revoked, and phase 1 expects
department heads with several workers each.

The owner also observed that budgets are really a property of a team rather
than of an individual worker: a department should get an allowance and decide
internally how to spend it.

## Decision

**A department is the budget holder.** `departments` carries
`daily_budget_usd`, agents belong to exactly one department through a NOT NULL
`department_id`, and the gateway enforces spend across the whole department.

**The per-agent budget survives as an optional sub-cap.** `daily_budget_usd`
on `agents` is now nullable:

| Value | Meaning |
| ----- | ------- |
| `NULL` | No sub-cap; the department budget alone governs |
| `0` | This agent specifically may not spend |
| *n* | Narrows the department budget for this agent |

A sub-cap can only narrow a department budget, never widen one. A generous
sub-cap does not buy an agent past an exhausted department.

Gates are checked broadest first: org kill switch, department switch, agent
switch, department budget, agent sub-cap. Budgets come last because they cost
a query, and every gate runs before anything is sent upstream so a blocked
call costs nothing.

## Why

1. **It makes provider-side caps practical.** One OpenRouter key per
   department is a handful of keys. That keeps the door open to a hard,
   provider-enforced ceiling that survives a bug in our own budget check --
   the strongest form of the cost control that `CLAUDE.md` makes priority one.
2. **A budget outlives the agent that spends it.** Agents get disabled,
   replaced and retiered. Attaching money to a team rather than to a worker
   means none of that disturbs the allowance.
3. **It costs no visibility.** Per-agent spend is still tracked through
   `model_calls.agent_id`; only enforcement moved. `spent_today_usd` and
   `department_spent_today_usd` both exist.

`agents.parent_agent_id` is deliberately left alone. It describes delegation
-- which head agent owns which worker -- and that is a different question from
who holds the budget. Step 6 builds on it.

## Consequences

- Every agent must belong to a department. There is no unassigned state to
  half-configure, and the database enforces it.
- The composite foreign key on `(department_id, org_id)` means an agent cannot
  join another organisation's department. The database refuses it rather than
  trusting application code to check.
- `BudgetExceeded` carries the scope that was hit, `department` or `agent`,
  because a caller that wants to raise the right limit has to know which.
- A department budget of zero means nothing approved yet, not unlimited. An
  unfunded department cannot spend, so the safe state is the default one.
- **This migration is only safe because every table was empty when it ran.**
  Adding a NOT NULL `department_id` to a populated `agents` table needs a
  backfill and a different migration.

## Still open

Provider-side per-key limits are not built. The application-level check is the
only enforcement today. Adding them later means a provisioning key and a
per-department OpenRouter key; those keys should live in Supabase Vault, which
is already installed on the project, rather than in a column.
