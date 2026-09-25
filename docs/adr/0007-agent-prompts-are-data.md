# ADR 007: Agent prompts are versioned data, not code

- **Status:** Accepted
- **Date:** 2026-09-24
- **Step:** 5b (prompts, pulled ahead of Step 4)

## Context

The research agent's two prompts were string constants in
`api/app/agents/research.py`. Changing one meant a code change and a redeploy.
Prompts are the setting the owner will tune most often, and the plan's
principle (Step 5b, now Step 6) is that nothing configurable lives in code. Model tiers
already work this way (ADR 003). Doing prompts before Step 4 means every agent
built from here on loads its prompt from the database from day one.

## Decision

- **`agent_prompts` table.** One row per agent, named slot (`answer`,
  `extract`) and version. Editing a prompt publishes a new version; a version
  is never changed or deleted. Exactly one version per slot is active (a
  partial unique index). Rolling back is activating an older version.
- **History is enforced, not promised.** The app roles may only update the
  `active` column (column-level grants); a trigger rejects any other change,
  so a hand edit in the SQL editor cannot rewrite what a past run used. There
  is no delete grant.
- **`publish_agent_prompt` and `activate_agent_prompt`** do the version bump
  and the swap in one transaction under an advisory lock, as `SECURITY
  INVOKER`, so RLS decides who may touch an agent.
- **Audited by trigger.** Every activation and deactivation writes an
  `agent_prompt_activated` or `agent_prompt_deactivated` event. A switch is a
  pair. Same-transaction events share a timestamp, so their order is not
  meaningful; the pair is.
- **Pinned per run.** A run resolves its prompts on its first invocation and
  stores the versions in `runs.prompt_versions`. A resume after a prompt edit
  finishes on the versions it started with. The versions appear on the
  `run_invoked` event and in the Langfuse trace metadata.
- **No prompt, no run.** If a slot has no active prompt the run pauses with
  `stop_reason = prompt_missing` and resumes once one exists. It never falls
  back to text in code.
- **Starting text is seed data.** `starter_prompts.py` holds the text a new
  agent begins with (`scripts.agent seed` uses it; the Control Center intake
  form will offer it). Nothing reads it at run time. The migration writes the
  same text as version 1 for existing research agents, as a snapshot.
- **Until the Control Center exists** (Step 11), prompts are managed with
  `python -m scripts.agent prompt list | set | activate`.

## Consequences

- A prompt change costs no deploy and takes effect on the next run; every run
  can be traced to the exact prompt text it used.
- The token cap in the extract prompt ("at most 5 claims") is now text the
  owner can edit; `parse_claims` still enforces `MAX_CLAIMS` in code, so an
  edit cannot make a run store an unbounded number of facts.
- A slot name is a contract with the agent's code: a new slot needs code that
  reads it. Editing a slot's text does not.
- Prompt text is org-visible to every member of the org (RLS by `org_id`).
  Per-agent access inside an org is not modelled; phase 1 has one operator.
