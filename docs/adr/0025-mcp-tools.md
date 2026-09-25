# ADR 025: Tools from MCP servers, added and approved by the owner

- **Status:** Accepted (owner, 2026-09-26)
- **Date:** 2026-09-26
- **Step:** 7.7 (MCP tools)

## Context

The owner wants tools to be configurable: add an MCP server from the UI and
give its tools to any agent, starting with paid services they already
subscribe to (Higgsfield). Tools so far were only built into the code.

## Decision

**MCP tools are ordinary `tools` rows** (`source = 'mcp'`), so everything
built for tools applies unchanged: risk classes, the autonomy ladder, the
tool-risk gate, approvals and the decision desk, idempotency, loop
detection, the pause and the kill, per-agent assignment
(`agents.allowed_tools`, charters), and `tool_calls` plus events.

**Only remote servers, over Streamable HTTP**, through the official MCP
Python SDK (`mcp` 2.x, MIT, about 4 MB). Vercel cannot run a local (stdio)
server. Each call opens its own short connection.

**The owner stays in charge of what agents are told:**

1. A tool found on a server starts **switched off**, with a risk class
   suggested from its own hints (a tool that says nothing is R4). The owner
   reads its description and inputs, sets the risk class and switches it on.
2. Approval is of an **exact definition** (sha256 of name, description and
   input schema). If the server changes it, the database switches the tool
   off (`tools_mcp_on_only_as_approved`, `mcp_tool_guard`) and records
   `mcp_tool_changed` until the owner approves it again. A server cannot
   rewrite a tool's instructions behind the owner's back.
3. **Anything R4 always asks** (the approval rule cannot be set to auto);
   a **daily cap** per tool (`max_calls_per_day`) bounds spending.
4. **What a tool returns is screened** (`content_screen`, Step 5.3) before
   an agent reads its text; images are reported, not passed to a model.
5. **Credentials live in Supabase Vault.** `mcp_credentials` holds pointers;
   only the backend (service_role) may read or write, through SQL functions
   that refuse everyone else. They never appear in events, API responses,
   or a model's context.

**Signing in (OAuth) takes two requests.** The SDK's OAuth provider waits
in one process for the browser to come back, which a serverless function
cannot do. So `start` discovers the authorization server (RFC 9728, RFC
8414), registers Pantheon (RFC 7591), makes the PKCE pair and a state, and
keeps them in Vault for ten minutes; the callback's state (single use)
finds them, the issuer is checked, the code is exchanged, and tokens are
stored. `access_token` refreshes them; when it cannot, the server is marked
`needs_auth` for the owner. A server without dynamic registration uses a
bearer token instead.

**Also here:** event, tool-call, task, run and fact timestamps now use
`clock_timestamp()`, so rows written in one transaction keep their true
order in the audit trail (an intermittent test failure traced to this).

## Consequences

- Adding a service is: add the server, sign in, read and approve its tools,
  assign them. No code, no deploy. The Control Center (Step 11) is a screen
  over `/mcp/*`; until then `python -m scripts.mcp`.
- Long jobs (a video generation) must finish within the tool's timeout
  (45 seconds by default, inside the API function's 60) or be polled with a
  status tool; pausing a run on an
  outside job, like an approval, is a later addition.
- Each call to an MCP tool that returns text costs one screening judgment.
- A2A (agents in other systems) is recorded in the plan for after phase 1.
