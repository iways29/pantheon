"""Step 7.1: gateway tool calling and the LangChain adapter.

Acceptance: a tool-using loop is blocked mid-way when the department is over
budget or the kill switch is on; cost and tokens are recorded for each call
in the loop.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.db import acting_as, as_service_role
from app.gateway import (
    BudgetExceeded,
    Gateway,
    KillSwitchEngaged,
    ModelResponse,
    OpenRouterTransport,
    ToolCall,
)
from app.gateway.chat_model import GatewayChatModel, to_openai
from tests.conftest_db import Tenants
from tests.test_gateway import TIERS, make_agent, set_kill_switch


@tool
def lookup(term: str) -> str:
    """Look a term up in the company brain."""
    return f"{term}: founded 2019"


@dataclass
class ToolLoopModel:
    """Asks for `lookup` until it has `rounds` results, then answers."""

    rounds: int = 3
    cost_usd: float = 0.01
    calls: list[dict[str, Any]] = field(default_factory=list)
    on_call: Any = None

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        self.calls.append({"messages": messages, **kw})
        if self.on_call:
            self.on_call(len(self.calls))
        results = sum(1 for m in messages if m["role"] == "tool")
        calls: tuple[ToolCall, ...] = ()
        text = "Acme was founded in 2019."
        if results < self.rounds:
            calls = (ToolCall(f"call_{results}", "lookup", json.dumps({"term": f"acme{results}"})),)
            text = ""
        return ModelResponse(
            model=model,
            provider="scripted",
            text=text,
            tokens_in=100,
            tokens_out=10,
            cost_usd=self.cost_usd,
            latency_ms=1,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
        )


def run_loop(db: psycopg.Connection, tenants: Tenants, agent: UUID, model: ToolLoopModel) -> str:
    """A minimal tool loop through the adapter, as LangGraph would drive it."""
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        chat = GatewayChatModel(gateway=Gateway(conn, model, TIERS), agent_id=str(agent))
        bound = chat.bind_tools([lookup])
        messages: list[Any] = [SystemMessage("Use tools."), HumanMessage("When was Acme founded?")]
        for _ in range(10):
            reply = bound.invoke(messages)
            messages.append(reply)
            if not reply.tool_calls:
                return str(reply.content)
            for call in reply.tool_calls:
                messages.append(ToolMessage(lookup.invoke(call["args"]), tool_call_id=call["id"]))
    raise AssertionError("loop did not finish")


def ledger(db: psycopg.Connection, agent: UUID) -> list[dict[str, Any]]:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select tokens_in, tokens_out, cost_usd from public.model_calls where agent_id = %s",
            (str(agent),),
        )
        return cursor.fetchall()


def test_a_tool_loop_runs_through_the_gateway_and_every_turn_is_costed(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)
    model = ToolLoopModel(rounds=2)

    answer = run_loop(db, tenants, agent, model)

    assert answer == "Acme was founded in 2019."
    assert len(model.calls) == 3
    first = model.calls[0]
    assert first["tools"][0]["function"]["name"] == "lookup"
    assert first["provider_preferences"] == {"require_parameters": True}
    # Tools are resent on every turn, and results go back as `tool` messages.
    assert all(c["tools"] for c in model.calls)
    last = model.calls[-1]["messages"]
    assert [m["role"] for m in last] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert last[2]["tool_calls"][0]["function"]["name"] == "lookup"
    assert last[3]["tool_call_id"] == "call_0"
    assert [(r["tokens_in"], r["tokens_out"]) for r in ledger(db, agent)] == [(100, 10)] * 3


def test_the_kill_switch_stops_a_tool_loop_mid_way(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)

    def switch_on_after_first(count: int) -> None:
        if count == 1:
            set_kill_switch(db, tenants.org_a, on=True)

    model = ToolLoopModel(rounds=5, on_call=switch_on_after_first)
    with pytest.raises(KillSwitchEngaged):
        run_loop(db, tenants, agent, model)
    assert len(model.calls) == 1, "the second turn never reached the provider"


def test_a_budget_stops_a_tool_loop_mid_way(db: psycopg.Connection, tenants: Tenants) -> None:
    agent = make_agent(db, tenants.org_a, department_budget="0.0250")
    model = ToolLoopModel(rounds=5, cost_usd=0.01)

    with pytest.raises(BudgetExceeded):
        run_loop(db, tenants, agent, model)
    assert len(model.calls) == 3, "$0.03 spent against $0.025: the fourth turn is refused"


def test_messages_convert_to_the_openai_format() -> None:
    from langchain_core.messages import AIMessage

    ai = AIMessage("", tool_calls=[{"name": "lookup", "args": {"term": "x"}, "id": "c1"}])
    assert to_openai(ai) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"term": "x"}'},
            }
        ],
    }
    assert to_openai(ToolMessage("ok", tool_call_id="c1")) == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "ok",
    }


def test_the_transport_sends_tools_and_parses_tool_calls() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "vendor/small",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"term": "acme"}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "cost": 0.0001},
            },
        )

    transport = OpenRouterTransport(
        "k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    response = transport.complete(
        model="vendor/small",
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice="auto",
    )

    body = json.loads(seen[0].content)
    assert body["tools"] == tools and body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == (ToolCall("call_1", "lookup", '{"term": "acme"}'),)
    assert response.assistant_message()["tool_calls"][0]["id"] == "call_1"


def test_malformed_tool_arguments_become_invalid_calls_not_crashes(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    agent = make_agent(db, tenants.org_a)

    @dataclass
    class Garbled:
        def complete(self, *, model: str, messages: Any, **_: Any) -> ModelResponse:
            return ModelResponse(
                model=model,
                text="",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0,
                latency_ms=1,
                tool_calls=(ToolCall("c", "lookup", "{not json"),),
            )

    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        reply = GatewayChatModel(
            gateway=Gateway(conn, Garbled(), TIERS), agent_id=str(agent)
        ).invoke("hi")

    assert reply.tool_calls == [] and reply.invalid_tool_calls[0]["name"] == "lookup"
