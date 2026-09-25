# ADR 018: Tools are code, configured as data, and every call goes through one runtime

- **Status:** Accepted
- **Date:** 2026-09-26
- **Step:** 7.2 (tool registry and runtime)

## Context

Agents need tools, and a tool is where an agent touches the world. The design
(`docs/design/agent-organization.md` section 5) sets risk classes R0 to R5
and says code implements tools while the database says who may use them and
how risky they are.

## Decision

- **Code declares, the database configures.** A `ToolSpec` in
  `app/tools/` has an argument schema, a risk class and a handler. The
  `tools` table (per org, audited) holds the description the model reads,
  the risk class, the approval policy, on or off, the timeout and the output
  limit; `scripts.agent seed` adds rows for new tools and never overwrites
  the owner's edits. R5 is refused by the database, and an R4 tool cannot be
  set to run without approval.
- **Grants are `agents.allowed_tools`** (Step 6, already audited). A second
  `agent_tools` table would duplicate it; it can come if per-grant settings
  are ever needed.
- **`ToolRuntime.call`** is the only way to run a tool: unknown, switched
  off or not granted is refused; arguments are validated; a side-effecting
  call needs an idempotency key (derived from run, tool and arguments if the
  caller gives none) and a repeat returns the stored result; an approval
  policy or R4 holds the call as a pending `tool_call` approval without
  running it; output is size-capped; an R2 tool's content is passed on only
  if it was screened clean. Every call is a `tool_calls` row and a
  `tool_called` event. Refusals go back to the model as readable errors;
  kill switch and budget refusals stop the run.
- **Starting tools:** `brain_search` (R0), `read_document` (R0, the agent's
  scopes only, ADR 015), `brain_propose_fact` (R1, through the write gate),
  `web_fetch_preview` (R2, fetch, screen, list claims, writes nothing to the
  brain). `create_task` and `report_result` arrive with tasks (7.3).
- **Narrowing long tool lists with Jev** (gate `tool_select`): above six
  allowed tools, a Choice over the tools' descriptions offers the top three.
  The options are added per call from the `tools` table (`extra_options`);
  the question's wording is data. The gate fails open to the full list, so
  narrowing can never lock an agent out.
- **LangChain tools** (`app/tools/langchain.py`) wrap the runtime, so a
  LangGraph or deepagents loop gets the same checks.

## Consequences

- Timeouts are enforced by each tool's own I/O limits (the fetcher's 10 s);
  the runtime records an overrun but cannot interrupt a running handler.
- New tools for departments (email drafts, calendar holds) are a `ToolSpec`
  plus a seeded row, and start held for approval if they touch the outside.
