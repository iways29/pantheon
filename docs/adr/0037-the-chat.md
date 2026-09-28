# ADR 037: The chat: talk with the Chief of Staff or any head, in seconds

- **Status:** Accepted (owner, 2026-09-27: "real chat first")
- **Date:** 2026-09-27
- **Step:** 10

## Context

The first chat (Step 10, phase 2) sent every message to `POST /orders`. The
owner saw the result on 2026-09-27: "hi" became an order, waited up to a
minute for the scheduler, and came back as "Which department should do
this?". There was no conversation, no answer from the agent, and no way to
talk with a department head.

## Decision

**A conversation, not an order box.** `POST /chat/{agent}` answers in one
model call, on the agent's own tier, through the gateway (budgets, the pause
and cost logging apply). The owner can talk with the Chief of Staff or any
department head (`GET /chat` lists them); workers are reached through their
head.

**What the agent reads, fresh each message:** its `chat` prompt (data;
seeded from `CHAT_STARTER_PROMPTS` by role on first use), who it is and the
time in New York, the departments and their heads, today's spend and work,
its recent orders or its department's tasks, the owner's preferences, the
facts the brain holds nearest the message, and the last messages of the
conversation.

**Asking for work starts work, at once.** The model has one tool: the
Chief of Staff `give_order` (an order it then routes, with its questions as
before), a head `give_task` (a task for its department). The task is created
with the message as its key and started by `start_now()`, a database function
that does what the minute tick does, now. The work's card follows the
message in the chat: route, question with answer buttons, result, cost.

**Memory after the reply.** What the owner said is sorted (ADR 034) after
the reply is sent (`remembered_at` marks it), so "call me boss" is learned
without making the owner wait. The `brain_policy` flag's `remember_chat`
switches it off.

**Data:** `chat_messages` (`org_id`, RLS; every message writes a
`chat_said` or `chat_replied` event), and the `chat` flag (reply length,
history, facts, recent orders), audited.

## Consequences

- About $0.0005 a message on the cheap tier, plus one embedding for recall
  and, after the reply, one Jev call to sort what was said.
- Replies stream (owner, 2026-09-27): `POST /chat/{agent}/stream` sends
  server-sent events as the model writes, through the gateway's `on_text`
  (OpenRouter `stream: true`, usage and cost from the last chunk), so every
  gate and the cost log still apply. The web relay passes the stream through.
- `POST /orders` stays for scripts; the web app uses the chat.
- Conversations (owner, 2026-09-27: "New chat"): each message belongs to a
  conversation with one agent (`chat_conversations`, migration
  `20260928170000_chat_conversations.sql`); the agent reads only its
  conversation's recent messages, while preferences and the brain carry
  across. A conversation is made by its first message, never by the button,
  so "New chat" on an empty chat makes nothing. After the `chat` flag's
  `idle_hours` (12) of quiet the next message starts a new one. Past chats
  are listed and can be carried on. An empty chat opens with a greeting and a
  line on today, from what the screen already knows (no model call).

