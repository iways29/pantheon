# ADR 017: Tool calling goes through the gateway, one turn at a time

- **Status:** Accepted
- **Date:** 2026-09-26
- **Step:** 7.1 (gateway tool calling)

## Context

Agents that use tools need a model that can ask for them. Until now the
gateway was text in, text out. The rule in `CLAUDE.md` is that every model
call goes through the gateway, including every turn of an agent's loop.
LangGraph and deepagents expect a LangChain chat model.

OpenRouter's tool-calling guide (read 2026-09-26): `tools` in OpenAI function
format, resent on every request; `tool_choice` `auto`, `none` or a named
function; `parallel_tool_calls`; a reply with `finish_reason: tool_calls` and
`message.tool_calls` (id, name, JSON arguments); results go back as `tool`
role messages. `provider.require_parameters: true` routes only to providers
that support every parameter sent.

## Decision

- **`Gateway.complete(tools=..., tool_choice=...)`** returns the tool calls on
  the response (`ModelResponse.tool_calls`, `finish_reason`). The gateway never
  runs a tool: the tool runtime (7.2) does, with its own checks.
- **One call per turn.** Each turn of a loop is its own gateway call, so the
  kill switch, budgets, the ledger and tracing apply between turns. A loop is
  stopped mid-way the moment either says no.
- **Sequential tool calls** (`parallel_tool_calls: false`), so every call is
  gated, logged and, where needed, approved one at a time.
- **`require_parameters: true`** whenever tools are sent, so a provider that
  would silently ignore them is never picked.
- **`GatewayChatModel`** (`app/gateway/chat_model.py`) is the LangChain chat
  model LangGraph and deepagents are given. It converts messages to and from
  the OpenAI format, supports `bind_tools`, and reports cost and tokens in the
  message metadata. Malformed tool arguments become invalid tool calls rather
  than errors.
- **`scripts.set_tier_model --check-tools`** checks every tier's model against
  OpenRouter's live catalogue (`supported_parameters` includes `tools`). The
  three seeded tier models all support tools (checked 2026-09-26).

## Consequences

- Tool loops cost one model call per step; per-run step and token caps
  (ADR 006) bound them.
- Parallel tool calls are given up for now: slightly slower loops in exchange
  for gating each call.
