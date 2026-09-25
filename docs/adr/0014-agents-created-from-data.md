# ADR 014: Agents are created from data, start switched off, and are audited

- **Status:** Accepted
- **Date:** 2026-09-26
- **Step:** 6 (configuration backend)

## Context

Until now the only agent, `researcher`, was created by the seed script. The
rule in `CLAUDE.md` is that agent definitions are data the owner changes
without editing code. The plan asks for agent creation from a structured
payload, with new agents starting disabled and an event emitted.

## Decision

- **`POST /agents`** (owner token only) takes a description: department (by
  name), name, role, tier, optional daily sub-cap, starting prompts by slot,
  allowed tools, and optionally the head it reports to. Unknown fields are
  refused. The same logic is `python -m scripts.agent agents create --file`.
- **New agents start switched off.** The column default is now `false`; the
  owner enables an agent deliberately (`POST /agents/{name}/enable`, or
  `scripts.agent agents enable`). Agents that existed keep their state, and
  the seed script turns the researcher on explicitly, since seeding is the
  owner's own act.
- **Idempotent by name.** Sending the same description again returns the same
  agent (200); a different description under an existing name is refused
  (409) rather than silently changing a live agent. Changes to an existing
  agent go through prompts (ADR 007), tiers (ADR 003) and, from Step 11,
  the Control Center.
- **Starting prompts** are published as version 1 of each slot through
  `publish_agent_prompt`, so they are versioned and audited like any edit.
- **`allowed_tools`** records what the owner allows; the tool registry
  (Step 7.2) decides what exists.
- **Audited by trigger** (`agents_audit`): `agent_created`, `agent_enabled`,
  `agent_disabled`, `agent_updated`, `agent_deleted`, including hand edits in
  the SQL editor. A cascade from deleting the org writes nothing.

## Consequences

- Creating an agent costs nothing and runs nothing: until it is enabled, the
  gateway refuses its calls (`agent_disabled`).
- A role is a contract with code: an agent whose role has no runner does
  nothing useful until Step 7.4 adds runners. That is intended.
