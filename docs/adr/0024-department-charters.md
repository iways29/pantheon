# ADR 024: Departments are charters, applied from data

- **Status:** Accepted
- **Date:** 2026-09-26
- **Step:** 8.0 (charters) and 8.1 (Research and Intelligence)

## Context

Step 8 builds departments one at a time. The plan asks for each charter to be
a database row the owner can edit (purpose, head, workers, tools, routine,
approval rules, gates, budget, autonomy, sensitivity, metrics), and for each
department to prove itself on five unattended mornings.

## Decision

**A charter is one versioned JSON document** in `department_charters`
(append-only apart from the live flag, audited, only a person may write it).
Its shape is `app/departments/charter.py`: the head and workers (name, role,
tier, runner, tools, starting prompts, autonomy, caps), the routine (task,
agent, local time, days, time zone, caps, input such as sources), plus
rules, gates, budget, sensitivity and metrics. A charter may be a **draft**:
stored for review, refused by apply.

**Apply** (`apply_charter`) makes the database match the live charter, in
one owner transaction: the department and budget; each agent created
switched off or updated in place; a prompt gets a new version only when the
charter's text differs from the live one; each routine item is a trigger
keyed by `routine_key`, created switched off or updated (its on/off state is
the owner's); an item removed from the charter switches its trigger off.
Tools must be built and gates set up, or apply refuses. **Enable** switches
the charter's agents and routine on or off together. Nothing ever starts on
its own.

**Research and Intelligence** (8.1) is the first charter: Research Lead
(head, `deep`), web researcher and fact curator (workers, `deep`), all
cheap tier, $0.25 a day, L1. Its one routine item, weekdays at 06:30 New
York time, carries the owner's topics and sources as input. Two tools were
added: `web_push_preview` (R1: a clean preview's facts through the write
gate) and `brain_hygiene_scan` (R0: disputed facts and facts past their
review date). At L1 every web read goes through the tool-risk gate.

**The department report** reads only the database: each routine task and
its tree cost, spend per day against budget, facts by write-gate outcome,
outside actions without an approval (must be 0), and the owner's orders.
`done` is the plan's test: five mornings in a row inside budget, nothing
external unapproved, and one order obeyed.

Executive Office and Marketing and Content are seeded as **drafts** for the
owner's review; their runners and tools (`router`, `digest`, `save_draft`)
come in 8.2 and 8.3.

## Consequences

- Changing a department (a new worker, a tool, a prompt, the routine time,
  the sources) is publishing a charter version and applying it: no code, no
  deploy. The Control Center (Step 11) will be a form over the same calls.
- A charter never moves an agent between departments; apply refuses.
- Sources are plain web pages fetched with the safe fetcher (decision 9);
  RSS and paid search are out of scope.
- CLI: `python -m scripts.department`; API: `/departments/{name}/charter`,
  `/apply`, `/enable`, `/report`.
