# ADR 039: The Control Center

- **Status:** Proposed (owner's go for Step 11, 2026-10-10)
- **Date:** 2026-10-10
- **Step:** 11

## Context

Everything the owner can change already lives in tables (Steps 6 to 9): charters,
prompts, routines, model tiers, tools, the autonomy ladder, limits, Jev's gates,
mailing lists. Until now the owner changed them with `api/scripts/` from a
laptop. Step 11 puts all of it in the browser, with no code change or redeploy,
and every change audited.

## Decision

**One router, `/control`, over what is already data** (`api/app/control.py`).
It reads and writes the same tables the scripts do, as the owner (RLS), and
reuses the existing endpoints where they exist (agents, departments, links,
documents, MCP, mailing lists). No new service or dependency.

**The charter is the source.** Anything a department charter defines (an
agent's prompt, tier, budget or tools; a routine; the department budget) is
changed by publishing a new charter version and applying it. Otherwise the next
apply would quietly undo the edit. Things no charter owns (an agent outside any
department, a routine with no `routine_key`) are written directly.

**Preview before apply.** "Preview" publishes and applies the charter inside a
transaction that is always rolled back, and shows what would change. Nothing is
saved until the owner presses Apply. Restoring an old charter version publishes
it again as the newest one.

**The change log lists changes only.** It is an inclusion list of event types
(charters, agents, prompts, routines, models, tools, autonomy, limits, judge,
rules, mailing lists, pause and kill), not "everything but the work". New kinds
of change must be added to it on purpose. Department edits gain an audit
trigger (`department_created`, `department_updated`), like the other tables.

**Standing rules are facts.** A rule is the owner's own words, written through
the brain's write gate like any memory, so every agent reads it with the brain.
Retiring one sets a new fact status, `retired`: it leaves search and the map
but stays in the record.

**Locks stay locks.** R4 always asks the owner; the ladder edits R0 to R3 only.
An R4 tool cannot be set to run without approval. MCP tools are switched on
only through the MCP review (ADR 027). The embedding model cannot be changed
here. Only a person can pause or kill (ADR 023).

**The phone gets the quick switches and the log**; charters, prompts, the
ladder and the judge are edited on a computer.

## Consequences

- One additive migration (`20261010100000_control_center.sql`).
- The scripts keep working; both paths write the same rows and events.
- Jev's question wording is still edited with `scripts.judge`; the screen shows
  versions and lets the owner switch gates, fail modes and policies.
- A new section is a new entry in the menu, not a redesign (Step 11 item 12).
