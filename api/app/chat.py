"""The chat (Step 10, ADR 037): the owner talks with the Chief of Staff or any
department head and gets an answer in seconds.

One model call per message, on the agent's own tier, through the gateway (so
budgets, the pause and cost logging apply). The agent reads its `chat` prompt
(data; seeded from the starter on first use), who it is, the company's state
today, its recent work, the owner's preferences and the facts the brain holds
nearest the message, then the conversation so far.

When the owner asks for work, the model calls one tool: the Chief of Staff
`give_order` (an order it then routes), a head `give_task` (a task for its
department). The work starts at once (`start_now`), not at the next minute.

What the owner said is sorted into memory (ADR 034) after the reply is sent,
so the answer is never kept waiting for it; `remembered_at` marks it done.
"""

import json
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import OwnerPrincipal
from app.clock import owner_now
from app.config import Settings, get_settings
from app.db import acting_as, connect
from app.gateway import GatewayError
from app.owner_api import Connection, owner_org
from app.screen import order_card, pulse
from app.tracing import in_session

router = APIRouter(tags=["chat"])

FLAG = "chat"
DEFAULTS: dict[str, Any] = {
    "max_tokens": 900,
    "history": 20,
    "facts": 5,
    "orders": 5,
    # After this long without a word, the next message starts a new conversation.
    "idle_hours": 12,
}
PROMPT_SLOT = "chat"
#: Who the owner can talk with.
TALKERS = ("chief_of_staff", "head")
#: The owner's time zone, for "today" in what the agent is told.

_TOOL_TEXT = {
    "chief_of_staff": (
        "give_order",
        "Hand the owner's request to a department as an order. Only for company work "
        "that needs a department: web research, its tools, content to publish, several "
        "steps, or an approval. Never for small talk or anything you can write or answer "
        "yourself in your reply.",
        "order",
    ),
    "head": (
        "give_task",
        "Start work in your department on what the owner asked for. Only when the owner "
        "asks your department to do something.",
        "task",
    ),
}


def _tool(role: str) -> dict[str, Any]:
    name, description, field = _TOOL_TEXT[role]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    field: {"type": "string", "description": "The request, in the owner's words"},
                    "title": {"type": "string", "description": "A short title, at most 10 words"},
                },
                "required": [field],
            },
        },
    }


# --- Services (overridden in tests) ------------------------------------------------


@dataclass
class Talk:
    """What a reply needs: a gateway for the model call, and a brain to recall
    facts from (None: no recall)."""

    gateway: Any
    brain: Any | None


MakeTalk = Callable[[psycopg.Connection, UUID], Talk]
Remember = Callable[[str, str, str, str], None]


def get_talk_factory(settings: Annotated[Settings, Depends(get_settings)]) -> MakeTalk:
    def make(connection: psycopg.Connection, agent_id: UUID) -> Talk:
        from app.brain import Brain, GatewayEmbedder
        from app.gateway import gateway_from

        gateway = gateway_from(connection, settings)
        return Talk(gateway, Brain(connection, GatewayEmbedder(gateway, agent_id=agent_id)))

    return make


def get_remember(settings: Annotated[Settings, Depends(get_settings)]) -> Remember:
    """Sorts one owner message into memory, on its own connection, after the
    reply has gone. Without TypeSafe nothing is sorted (it stays unmarked)."""

    def remember(org_id: str, user_id: str, agent_id: str, message_id: str) -> None:
        if not settings.database_url or not settings.typesafe_api_key:
            return
        from app.brain import memory
        from app.brain import policy as brain_policy
        from app.judge import judge_from
        from app.knowledge.wiring import writer_from

        with connect(settings.database_url) as connection:
            with acting_as(connection, user_id=user_id) as conn:
                if not brain_policy.load(conn, org_id).remember_chat:
                    return
                message = conn.execute(
                    "select m.id, m.body, m.created_at, a.name from public.chat_messages m "
                    "join public.agents a on a.id = m.agent_id "
                    "where m.id = %s and m.remembered_at is null",
                    (message_id,),
                ).fetchone()
            if message is None:
                return
            with acting_as(connection, user_id=user_id) as conn:
                memory.remember(
                    writer_from(conn, settings, agent_id=agent_id),
                    [memory.chat_moment(message, message["name"])],
                    org_id=UUID(org_id),
                    agent_id=UUID(agent_id),
                    judge=judge_from(conn, settings),
                    connection=conn,
                )
                conn.execute(
                    "update public.chat_messages set remembered_at = now() where id = %s",
                    (message_id,),
                )

    return remember


OpenConnection = Callable[[], AbstractContextManager[psycopg.Connection]]


def get_connection_opener(settings: Annotated[Settings, Depends(get_settings)]) -> OpenConnection:
    """A connection of its own for a streamed reply, which outlives the request
    handler. Overridden in tests."""

    def open_() -> AbstractContextManager[psycopg.Connection]:
        if not settings.database_url:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DATABASE_URL is not set")
        return connect(settings.database_url)

    return open_


TalkFactory = Annotated[MakeTalk, Depends(get_talk_factory)]
OpenerDep = Annotated[OpenConnection, Depends(get_connection_opener)]
RememberDep = Annotated[Remember, Depends(get_remember)]


# --- Reading ----------------------------------------------------------------------


def _settings(cursor: psycopg.Cursor, org_id: str) -> dict[str, Any]:
    cursor.execute(
        "select value from public.system_flags where org_id = %s and key = %s", (org_id, FLAG)
    )
    row = cursor.fetchone()
    return DEFAULTS | (row["value"] if row and isinstance(row["value"], dict) else {})


def _talker(cursor: psycopg.Cursor, name: str) -> dict[str, Any]:
    cursor.execute(
        "select a.id, a.name, a.role_type, a.enabled, a.department_id, d.name as department "
        "from public.agents a left join public.departments d on d.id = a.department_id "
        "where a.name = %s",
        (name,),
    )
    agent = cursor.fetchone()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent named {name!r}")
    if agent["role_type"] not in TALKERS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{name} is a worker: talk with its department head or the Chief of Staff",
        )
    return agent


def _message(row: dict[str, Any], cursor: psycopg.Cursor) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "role": row["role"],
        "text": row["body"],
        "at": row["created_at"].isoformat(),
        "conversation_id": str(row["conversation_id"]) if row.get("conversation_id") else None,
        "order": order_card(cursor, row["task_id"]) if row["task_id"] else None,
    }


def thread(
    cursor: psycopg.Cursor, agent_id: UUID, limit: int, conversation: UUID | None = None
) -> list[dict[str, Any]]:
    """One conversation with an agent, oldest first: the one named, else the
    latest."""
    if conversation is None:
        cursor.execute(
            "select id from public.chat_conversations where agent_id = %s "
            "order by last_at desc limit 1",
            (str(agent_id),),
        )
        latest = cursor.fetchone()
        if latest is None:
            return []
        conversation = latest["id"]
    cursor.execute(
        "select * from (select id, role, body, created_at, task_id, conversation_id "
        "from public.chat_messages where agent_id = %s and conversation_id = %s "
        "order by created_at desc, id desc limit %s) m order by created_at, id",
        (str(agent_id), str(conversation), limit),
    )
    return [_message(r, cursor) for r in cursor.fetchall()]


def conversations(cursor: psycopg.Cursor, agent_id: UUID, limit: int) -> list[dict[str, Any]]:
    """Past chats with an agent, newest first."""
    cursor.execute(
        "select c.id, c.title, c.created_at, c.last_at, "
        "(select count(*) from public.chat_messages m where m.conversation_id = c.id) as n "
        "from public.chat_conversations c where c.agent_id = %s "
        "order by c.last_at desc limit %s",
        (str(agent_id), limit),
    )
    return [
        {
            "id": str(r["id"]),
            "title": r["title"] or "A conversation",
            "started_at": r["created_at"].isoformat(),
            "last_at": r["last_at"].isoformat(),
            "messages": r["n"],
        }
        for r in cursor.fetchall()
    ]


def _conversation(
    cursor: psycopg.Cursor,
    org_id: str,
    user_id: str,
    agent_id: UUID,
    body: "Say",
    conf: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Which conversation this message belongs to: the one named, a new one
    when asked for or after `idle_hours` of quiet, else the latest."""
    if body.conversation_id is not None:
        cursor.execute(
            "select id, title from public.chat_conversations where id = %s and agent_id = %s",
            (str(body.conversation_id), str(agent_id)),
        )
        found = cursor.fetchone()
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation with this agent")
        return found
    if not body.new:
        cursor.execute(
            "select id, title from public.chat_conversations where agent_id = %s "
            "and last_at > now() - %s * interval '1 hour' order by last_at desc limit 1",
            (str(agent_id), float(conf["idle_hours"])),
        )
        found = cursor.fetchone()
        if found is not None:
            return found
    cursor.execute(
        "insert into public.chat_conversations (org_id, agent_id, title, created_by) "
        "values (%s, %s, %s, %s) returning id, title",
        (org_id, str(agent_id), " ".join(text.split())[:120], user_id),
    )
    return cursor.fetchone()


def _prompt(cursor: psycopg.Cursor, org_id: str, agent: dict[str, Any]) -> str:
    """The agent's `chat` prompt; the starter for its role, stored, on first use."""
    from app.agents.starter_prompts import CHAT_STARTER_PROMPTS

    cursor.execute(
        "select body from public.agent_prompts where agent_id = %s and slot = %s and active",
        (str(agent["id"]), PROMPT_SLOT),
    )
    row = cursor.fetchone()
    if row is not None:
        return row["body"]
    body = CHAT_STARTER_PROMPTS[agent["role_type"]]
    cursor.execute(
        "insert into public.agent_prompts (org_id, agent_id, slot, version, body, note, active) "
        "select %s, %s, %s, 1, %s, 'Starting prompt', true where not exists "
        "(select 1 from public.agent_prompts where agent_id = %s and slot = %s)",
        (org_id, str(agent["id"]), PROMPT_SLOT, body, str(agent["id"]), PROMPT_SLOT),
    )
    return body


def _context(
    cursor: psycopg.Cursor,
    org_id: str,
    agent: dict[str, Any],
    conf: dict[str, Any],
    facts: list[str],
) -> str:
    """Who the agent is and the state of things, fresh for this message."""
    from app.brain.memory import preamble

    now = owner_now()
    role = "Chief of Staff" if agent["role_type"] == "chief_of_staff" else "head"
    parts = [
        f"You are {agent['name']}, {role} of the {agent['department'] or 'company'} "
        f"department. It is {now:%A %-d %B %Y, %H:%M} in New York.",
    ]
    cursor.execute(
        "select d.name, d.enabled, (select a.name from public.agents a where "
        "a.department_id = d.id and a.role_type in ('head', 'chief_of_staff') "
        "order by a.name limit 1) as head from public.departments d order by d.name"
    )
    departments = [
        f"{r['name']} (head: {r['head'] or 'none'}{'' if r['enabled'] else ', switched off'})"
        for r in cursor.fetchall()
    ]
    parts.append("Departments: " + "; ".join(departments) + ".")
    day = pulse(cursor)
    parts.append(
        f"Today so far: ${day['spend_usd']:.2f} spent of ${day['budget_usd']:.2f}, "
        f"{day['tasks_done']} tasks done, {day['tasks_open']} unfinished, "
        f"{day['needs_you_total']} things waiting for the owner."
    )
    if agent["role_type"] == "chief_of_staff":
        cursor.execute(
            "select t.title, t.status, t.result ->> 'summary' as summary from public.tasks t "
            "join public.agents a on a.id = t.assigned_agent_id "
            "where t.parent_task_id is null and a.role_type = 'chief_of_staff' "
            "order by t.created_at desc limit %s",
            (int(conf["orders"]),),
        )
    else:
        cursor.execute(
            "select t.title, t.status, t.result ->> 'summary' as summary from public.tasks t "
            "where t.department_id = %s order by t.created_at desc limit %s",
            (str(agent["department_id"]), int(conf["orders"])),
        )
    recent = [
        f"- {r['title']} ({r['status']})" + (f": {r['summary'][:200]}" if r["summary"] else "")
        for r in cursor.fetchall()
    ]
    if recent:
        parts.append("Recent work:\n" + "\n".join(recent))
    if facts:
        parts.append(
            "What the brain holds nearest the owner's message:\n"
            + "\n".join(f"- {f}" for f in facts)
        )
    preferences = preamble(cursor.connection, org_id)
    if preferences:
        parts.append(preferences)
    return "\n\n".join(parts)


# --- Routes -----------------------------------------------------------------------


class Say(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    #: The browser's key for this send: a retry is one message.
    key: str | None = Field(default=None, max_length=100)
    #: Carry on this conversation (a past chat reopened).
    conversation_id: UUID | None = None
    #: Start a new conversation ("New chat").
    new: bool = False


@router.get("/chat")
def get_talkers(principal: OwnerPrincipal, connection: Connection) -> list[dict[str, Any]]:
    """Who the owner can talk with: the Chief of Staff and every department
    head, each with the last thing said."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select a.name, a.role_type, a.enabled, d.name as department,
                   (select jsonb_build_object('text', left(m.body, 160), 'role', m.role,
                                              'at', m.created_at)
                      from public.chat_messages m where m.agent_id = a.id
                     order by m.created_at desc limit 1) as last
              from public.agents a left join public.departments d on d.id = a.department_id
             where a.role_type = any(%s)
             order by a.role_type = 'chief_of_staff' desc, d.name, a.name
            """,
            (list(TALKERS),),
        )
        return [
            {
                "name": r["name"],
                "role": r["role_type"],
                "enabled": r["enabled"],
                "department": r["department"],
                "last": r["last"],
            }
            for r in cursor.fetchall()
        ]


@router.get("/chat/{agent}")
def get_thread(
    agent: str,
    principal: OwnerPrincipal,
    connection: Connection,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    conversation: UUID | None = None,
) -> list[dict[str, Any]]:
    """A conversation with the agent: the one named, else the latest."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        return thread(cursor, _talker(cursor, agent)["id"], limit, conversation)


@router.get("/chat/{agent}/conversations")
def get_conversations(
    agent: str,
    principal: OwnerPrincipal,
    connection: Connection,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Past chats with the agent, newest first."""
    owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        return conversations(cursor, _talker(cursor, agent)["id"], limit)


@router.post("/chat/{agent}")
def say(
    agent: str,
    body: Say,
    principal: OwnerPrincipal,
    connection: Connection,
    make_talk: TalkFactory,
    remember: RememberDep,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Say something to an agent and get its answer. When the owner asked for
    work, the answer names the order or task it started."""
    org_id = owner_org(connection, principal.user_id)
    out, later = converse(connection, principal.user_id, org_id, agent, body, make_talk)
    if later:
        background.add_task(remember, org_id, principal.user_id, *later)
    return out


@router.post("/chat/{agent}/stream")
def say_streamed(
    agent: str,
    body: Say,
    principal: OwnerPrincipal,
    connection: Connection,
    make_talk: TalkFactory,
    remember: RememberDep,
    open_connection: OpenerDep,
) -> StreamingResponse:
    """The same turn, streamed as server-sent events: `delta` events carry the
    reply as it is written, then one `done` event carries both messages (and
    any work started), or an `error` event says why there is no answer."""
    org_id = owner_org(connection, principal.user_id)
    with acting_as(connection, user_id=principal.user_id) as conn, conn.cursor() as cursor:
        _talker(cursor, agent)  # an unknown agent or a worker is refused before streaming
    if not body.text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Say something first")

    events: queue.Queue[tuple[str, Any] | None] = queue.Queue()

    def work() -> None:
        try:
            with open_connection() as own:
                out, later = converse(
                    own,
                    principal.user_id,
                    org_id,
                    agent,
                    body,
                    make_talk,
                    on_text=lambda piece: events.put(("delta", {"text": piece})),
                )
            events.put(("done", out))
            if later:
                remember(org_id, principal.user_id, *later)
        except HTTPException as error:
            events.put(("error", {"detail": error.detail}))
        except Exception as error:
            events.put(("error", {"detail": f"{agent} could not answer: {error}"}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream() -> Iterator[str]:
        while (item := events.get()) is not None:
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def converse(
    connection: psycopg.Connection,
    user_id: str,
    org_id: str,
    agent: str,
    body: Say,
    make_talk: MakeTalk,
    on_text: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    """One turn of the conversation: the owner's message kept, the agent's
    answer (streamed through `on_text` when given), any work it started.
    Returns the pair of messages, and what to sort into memory afterwards."""
    from app.tasks import TaskError, order

    text = body.text.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Say something first")
    key = f"chat:{body.key or uuid4()}"

    with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:
        talker = _talker(cursor, agent)
        cursor.execute(
            "select id from public.chat_messages where org_id = %s and idempotency_key = %s",
            (org_id, key),
        )
        seen = cursor.fetchone()
        if seen is not None:
            # A retried send: the answer that followed it, when there is one.
            cursor.execute(
                "select id, role, body, created_at, task_id, conversation_id "
                "from public.chat_messages where agent_id = %s and created_at >= "
                "(select created_at from public.chat_messages where id = %s) "
                "order by created_at, id limit 2",
                (str(talker["id"]), str(seen["id"])),
            )
            pair = cursor.fetchall()
            if len(pair) > 1 and pair[1]["role"] == "agent":
                done = {"said": _message(pair[0], cursor), "reply": _message(pair[1], cursor)}
                return done, None
            said = pair[0]  # saved, but never answered: answer it now
            conf = _settings(cursor, org_id)
        else:
            conf = _settings(cursor, org_id)
            chat = _conversation(cursor, org_id, user_id, talker["id"], body, conf, text)
            cursor.execute(
                "insert into public.chat_messages "
                "(org_id, agent_id, conversation_id, role, body, idempotency_key, created_by) "
                "values (%s, %s, %s, 'owner', %s, %s, %s) "
                "returning id, role, body, created_at, task_id, conversation_id",
                (org_id, str(talker["id"]), str(chat["id"]), text, key, user_id),
            )
            said = cursor.fetchone()
        prompt = _prompt(cursor, org_id, talker)
        cursor.execute(
            "select role, body from (select role, body, created_at, id from public.chat_messages "
            "where conversation_id = %s and id <> %s order by created_at desc, id desc "
            "limit %s) m order by created_at, id",
            (str(said["conversation_id"]), str(said["id"]), int(conf["history"])),
        )
        history = [
            {"role": "user" if r["role"] == "owner" else "assistant", "content": r["body"]}
            for r in cursor.fetchall()
        ]

    talk = make_talk(connection, talker["id"])
    facts: list[str] = []
    with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:
        if talk.brain is not None and int(conf["facts"]) > 0:
            try:
                matches = talk.brain.search(text, limit=int(conf["facts"]))
            except (GatewayError, RuntimeError):
                matches = []  # a reply without recall beats no reply
            facts = [m.fact.claim for m in matches]
            if matches:
                # The brain screen draws a thread from the agent to these facts.
                cursor.execute(
                    "insert into public.events (org_id, agent_id, type, payload) "
                    "values (%s, %s, 'chat_recalled', %s)",
                    (
                        org_id,
                        str(talker["id"]),
                        json.dumps({"fact_ids": [str(m.fact.id) for m in matches]}),
                    ),
                )
        system = prompt + "\n\n" + _context(cursor, org_id, talker, conf, facts)

    role = talker["role_type"]
    try:
        with (
            in_session(f"chat-{said['conversation_id']}" if said["conversation_id"] else None),
            acting_as(connection, user_id=user_id),
        ):
            response = talk.gateway.complete(
                on_text=on_text,
                agent_id=talker["id"],
                max_tokens=int(conf["max_tokens"]),
                messages=[
                    {"role": "system", "content": system},
                    *history,
                    {"role": "user", "content": text},
                ],
                tools=[_tool(role)],
                tool_choice="auto",
            )
    except GatewayError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"{agent} could not answer: {error}"
        ) from error

    reply_text = (response.text or "").strip()
    task = None
    call = next((c for c in response.tool_calls if c.name == _TOOL_TEXT[role][0]), None)
    if call is not None:
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        request = str(args.get(_TOOL_TEXT[role][2]) or text).strip()[:4000]
        title = str(args.get("title") or request.splitlines()[0])[:120] or "Order"
        try:
            task = order(
                connection,
                user_id=user_id,
                org_id=org_id,
                agent=talker["name"],
                title=title,
                instructions=request,
                idempotency_key=f"chat:{said['id']}",
            )
        except TaskError as error:
            reply_text = f"I could not start that: {error}"
        else:
            if not reply_text:
                reply_text = f"On it: {title}. I will report back here."
            try:
                with acting_as(connection, user_id=user_id) as conn:
                    conn.execute("select public.start_now()")
            except psycopg.Error:
                pass  # the minute tick starts it instead
    if not reply_text:
        reply_text = "I have nothing to add."

    with acting_as(connection, user_id=user_id) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.chat_messages set task_id = %s where id = %s",
            (str(task.id) if task else None, str(said["id"])),
        )
        cursor.execute(
            "insert into public.chat_messages "
            "(org_id, agent_id, conversation_id, role, body, task_id, meta) "
            "values (%s, %s, %s, 'agent', %s, %s, %s) "
            "returning id, role, body, created_at, task_id, conversation_id",
            (
                org_id,
                str(talker["id"]),
                str(said["conversation_id"]) if said["conversation_id"] else None,
                reply_text[:8000],
                str(task.id) if task else None,
                json.dumps(
                    {
                        "model": response.model,
                        "tokens_in": response.tokens_in,
                        "tokens_out": response.tokens_out,
                        "cost_usd": response.cost_usd,
                        "latency_ms": response.latency_ms,
                    }
                ),
            ),
        )
        reply = cursor.fetchone()
        if said["conversation_id"]:
            cursor.execute(
                "update public.chat_conversations set last_at = clock_timestamp() where id = %s",
                (str(said["conversation_id"]),),
            )
        cursor.execute(
            "select id, role, body, created_at, task_id, conversation_id "
            "from public.chat_messages where id = %s",
            (str(said["id"]),),
        )
        said = cursor.fetchone()
        out = {"said": _message(said, cursor), "reply": _message(reply, cursor)}

    # An order to the Chief of Staff is remembered by the router as it takes
    # it (ADR 034); everything else said is sorted here.
    later = None if task and role == "chief_of_staff" else (str(talker["id"]), str(said["id"]))
    return out, later
