"""The chat (ADR 037): the owner talks with the Chief of Staff or a head.

The model is scripted: it answers small talk with words, and a request for
work with a tool call. Everything else is real: the database, RLS, the
gateway's budget and cost logging, the order and its task.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.brain import Brain, HashingEmbedder
from app.db import as_service_role
from app.gateway import Gateway, ModelResponse, ToolCall
from tests.conftest_db import Tenants, admit
from tests.test_gateway import TIERS, make_department


@dataclass
class Talker:
    """The model behind the chat, scripted."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        self.calls.append({"messages": messages, **kw})
        said = messages[-1]["content"]
        tool_calls: tuple[ToolCall, ...] = ()
        text = f"You said: {said}"
        if said.startswith("please "):
            name = kw["tools"][0]["function"]["name"]
            field_ = "order" if name == "give_order" else "task"
            tool_calls = (ToolCall("c1", name, json.dumps({field_: said, "title": "The job"})),)
            text = ""
        return ModelResponse(
            model=model,
            text=text,
            tokens_in=100,
            tokens_out=20,
            cost_usd=0.0002,
            latency_ms=5,
            provider="scripted",
            tool_calls=tool_calls,
        )


@dataclass(frozen=True)
class Office:
    chief: UUID
    head: UUID
    worker: UUID


def _agent(cursor: psycopg.Cursor, org: UUID, dept: UUID, name: str, role: str) -> UUID:
    cursor.execute(
        "insert into public.agents (org_id, department_id, name, role, role_type, runner, "
        "model_tier, enabled) values (%s, %s, %s, %s, %s, %s, 'cheap', true) returning id",
        (str(org), str(dept), name, role, role, "router" if role == "chief_of_staff" else "deep"),
    )
    return cursor.fetchone()["id"]


@pytest.fixture
def office(db: psycopg.Connection, tenants: Tenants) -> Office:
    executive = make_department(db, tenants.org_a, name="executive")
    research = make_department(db, tenants.org_a, name="research")
    with as_service_role(db) as conn, conn.cursor() as cursor:
        chief = _agent(cursor, tenants.org_a, executive, "chief-of-staff", "chief_of_staff")
        head = _agent(cursor, tenants.org_a, research, "research-lead", "head")
        worker = _agent(cursor, tenants.org_a, research, "web-researcher", "worker")
        # A preference every agent reads (ADR 034).
        cursor.execute(
            "insert into public.facts (org_id, claim, admitted_by, kind) "
            "values (%s, 'Call the owner boss.', %s, 'preference')",
            (str(tenants.org_a), str(admit(conn, tenants.org_a).request_id)),
        )
    return Office(chief, head, worker)


@pytest.fixture
def chat(db: psycopg.Connection, settings: Any, office: Office) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from app.chat import Talk, get_remember, get_talk_factory
    from app.config import get_settings
    from app.main import app
    from app.owner_api import get_connection
    from tests.test_health import auth, make_token

    model = Talker()
    remembered: list[str] = []

    def same_connection() -> Any:
        yield db

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_connection] = same_connection
    app.dependency_overrides[get_talk_factory] = lambda: (
        lambda conn, _agent: Talk(Gateway(conn, model, TIERS), Brain(conn, HashingEmbedder()))
    )
    app.dependency_overrides[get_remember] = lambda: lambda *args: remembered.append(args[-1])
    client = TestClient(app, headers=auth(make_token()))
    client.model = model  # type: ignore[attr-defined]
    client.remembered = remembered  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.clear()


def test_small_talk_gets_an_answer_and_starts_nothing(
    db: psycopg.Connection,
    chat: Any,
) -> None:
    body = chat.post("/chat/chief-of-staff", json={"text": "hi", "key": "k1"}).json()

    assert body["said"]["text"] == "hi"
    assert body["reply"]["text"] == "You said: hi"
    assert body["reply"]["order"] is None
    with db.cursor() as cursor:
        cursor.execute("select count(*) as n from public.tasks")
        assert cursor.fetchone()["n"] == 0, "small talk is not an order"
        cursor.execute(
            "select type from public.events where type like 'chat_%' order by created_at"
        )
        assert [r["type"] for r in cursor.fetchall()] == ["chat_said", "chat_replied"]
        cursor.execute("select count(*) as n from public.model_calls")
        assert cursor.fetchone()["n"] == 1, "one model call per message, logged"
    assert chat.remembered == [body["said"]["id"]], "sorted into memory after the reply"


def test_the_agent_knows_who_it_is_and_the_owners_preferences(chat: Any) -> None:
    chat.post("/chat/chief-of-staff", json={"text": "how are we doing?"})

    system = chat.model.calls[-1]["messages"][0]["content"]
    assert system.startswith("You are the Chief of Staff of The Unreal Lab")
    assert "You are chief-of-staff, Chief of Staff of the executive department" in system
    assert "research (head: research-lead)" in system
    assert "Call the owner boss." in system
    assert chat.model.calls[-1]["tools"][0]["function"]["name"] == "give_order"


def test_asking_for_work_starts_an_order_at_once(
    db: psycopg.Connection,
    chat: Any,
    office: Office,
) -> None:
    body = chat.post("/chat/chief-of-staff", json={"text": "please find three founders"}).json()

    assert body["reply"]["text"] == "On it: The job. I will report back here."
    card = body["reply"]["order"]
    assert card["text"] == "please find three founders"
    with db.cursor() as cursor:
        cursor.execute("select assigned_agent_id, status from public.tasks")
        task = cursor.fetchone()
    assert task["assigned_agent_id"] == office.chief
    assert task["status"] == "running", "started now, not at the next minute"
    assert chat.remembered == [], "the router remembers an order as it takes it"


def test_a_head_takes_work_for_its_department(
    db: psycopg.Connection,
    chat: Any,
    office: Office,
) -> None:
    body = chat.post("/chat/research-lead", json={"text": "please read Show HN today"}).json()

    assert chat.model.calls[-1]["tools"][0]["function"]["name"] == "give_task"
    assert body["reply"]["order"]["text"] == "please read Show HN today"
    with db.cursor() as cursor:
        cursor.execute("select assigned_agent_id from public.tasks")
        assert cursor.fetchone()["assigned_agent_id"] == office.head


def test_the_thread_remembers_the_conversation(chat: Any) -> None:
    chat.post("/chat/chief-of-staff", json={"text": "first"})
    chat.post("/chat/chief-of-staff", json={"text": "second"})

    sent = chat.model.calls[-1]["messages"]
    assert [m["content"] for m in sent[1:]] == ["first", "You said: first", "second"]
    thread = chat.get("/chat/chief-of-staff").json()
    assert [(m["role"], m["text"]) for m in thread] == [
        ("owner", "first"),
        ("agent", "You said: first"),
        ("owner", "second"),
        ("agent", "You said: second"),
    ]


def test_a_retried_send_is_one_message(chat: Any) -> None:
    first = chat.post("/chat/chief-of-staff", json={"text": "hello", "key": "same"}).json()
    again = chat.post("/chat/chief-of-staff", json={"text": "hello", "key": "same"}).json()

    assert again == first
    assert len(chat.model.calls) == 1


def test_workers_are_not_talked_to_directly(chat: Any) -> None:
    assert chat.post("/chat/web-researcher", json={"text": "hi"}).status_code == 409
    assert chat.post("/chat/nobody", json={"text": "hi"}).status_code == 404
    talkers = [t["name"] for t in chat.get("/chat").json()]
    assert talkers == ["chief-of-staff", "research-lead"]


def test_the_chat_prompt_is_data(db: psycopg.Connection, chat: Any, office: Office) -> None:
    chat.post("/chat/research-lead", json={"text": "hi"})
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select version, active from public.agent_prompts "
            "where agent_id = %s and slot = 'chat'",
            (str(office.head),),
        )
        assert [(r["version"], r["active"]) for r in cursor.fetchall()] == [(1, True)]
    from app.agents import prompts
    from tests.conftest_db import USER_A

    prompts.publish(db, user_id=USER_A, agent_id=office.head, slot="chat", body="You are terse.")
    chat.post("/chat/research-lead", json={"text": "again"})
    assert chat.model.calls[-1]["messages"][0]["content"].startswith("You are terse.")
