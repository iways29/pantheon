"""Step 7.2: the tool registry and runtime.

Acceptance: an agent can only call tools it is allowed; a side-effecting
tool called twice with one key acts once; an injection inside a fetched page
is quarantined before the agent sees it.
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from pydantic import BaseModel

from app.brain import Brain, HashingEmbedder
from app.brain.write_gate import BrainWriter
from app.db import acting_as, as_service_role
from app.gateway import Gateway
from app.judge import Judge
from app.judge.screening import Screener
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.knowledge.links import Links
from app.tools import REGISTRY, ToolContext, ToolRuntime, ToolSpec, register, seed_tools
from app.tools.langchain import langchain_tools
from app.tools.selection import offered_tools
from tests.conftest_db import Tenants, admit
from tests.scripted_jev import ScriptedJev, choice, noul
from tests.test_gateway import TIERS, make_agent
from tests.test_judge import set_price
from tests.test_links import PAGE, PLANTED, ClaimsModel, page

STARTING = ["brain_search", "brain_propose_fact", "read_document", "web_fetch_preview"]
EVIDENCE = "Acme Corp was founded in 2019 in Leeds. It makes small wind turbines."


def detector(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
    if "prompt_injection" in questions:
        return {"prompt_injection": noul(0.95 if "AI assistants" in state["text"] else 0.02)}
    return {}


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants) -> UUID:
    agent_id = make_agent(db, tenants.org_a, name="researcher")
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=STARTER_GATES)
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    allow(db, agent_id, STARTING)
    from app.agents import prompts

    prompts.publish(
        db, user_id=tenants.user_a, agent_id=agent_id, slot="extract", body="List claims as JSON."
    )
    return agent_id


def allow(db: psycopg.Connection, agent: UUID, tools: list[str]) -> None:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "update public.agents set allowed_tools = %s where id = %s", (tools, str(agent))
        )


def runtime(
    conn: psycopg.Connection, tenants: Tenants, agent: UUID, jev: ScriptedJev | None = None
) -> ToolRuntime:
    jev = jev or ScriptedJev(respond=detector)
    gateway = Gateway(conn, ClaimsModel(), TIERS, systemone=jev)
    judge = Judge(conn, gateway)
    brain = Brain(conn, HashingEmbedder())
    writer = BrainWriter(conn, brain, judge)
    return ToolRuntime(
        ToolContext(
            connection=conn,
            org_id=tenants.org_a,
            agent_id=agent,
            agent_name="researcher",
            gateway=gateway,
            brain=brain,
            writer=writer,
            links=Links(conn, gateway=gateway, screener=Screener(conn, judge), writer=writer),
            fetcher=lambda url: page(PLANTED if "planted" in url else PAGE),
        )
    )


def calls(db: psycopg.Connection, agent: UUID) -> list[dict[str, Any]]:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select tool, status, error from public.tool_calls where agent_id = %s "
            "order by created_at",
            (str(agent),),
        )
        return cursor.fetchall()


# --- Only allowed tools --------------------------------------------------------


def test_the_starting_tools_are_seeded_as_data(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select name, risk_class from public.tools where org_id = %s order by name",
            (str(tenants.org_a),),
        )
        rows = {r["name"]: r["risk_class"] for r in cursor.fetchall()}
    assert rows == {
        "brain_propose_fact": "R1",
        "brain_search": "R0",
        "read_document": "R0",
        "web_fetch_preview": "R2",
    }


def test_an_agent_can_only_call_tools_it_is_allowed(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    allow(db, agent, ["brain_search"])

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent)
        allowed = rt.call("brain_search", {"query": "Acme founding"})
        refused = rt.call(
            "brain_propose_fact", {"claim": "Acme exists.", "evidence": "Acme exists."}
        )
        unknown = rt.call("send_email", {"to": "x"})

    assert allowed.status == "ok"
    assert refused.status == "refused" and "may not call" in refused.output["error"]
    assert unknown.status == "refused"
    assert [c["status"] for c in calls(db, agent)] == ["ok", "refused", "refused"]


def test_a_switched_off_tool_is_refused(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("update public.tools set enabled = false where name = 'brain_search'")

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        result = runtime(conn, tenants, agent).call("brain_search", {"query": "Acme"})

    assert result.status == "refused" and "switched off" in result.output["error"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "x"},
        {"query": "Acme", "limit": 99},
        {"query": "Acme", "drop": "table"},
        "{not json",
    ],
)
def test_bad_arguments_are_refused_and_explained(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, arguments: Any
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        result = runtime(conn, tenants, agent).call("brain_search", arguments)

    assert result.status == "refused" and "Bad arguments" in result.output["error"]


# --- One key, one action ------------------------------------------------------------


def test_a_side_effecting_tool_called_twice_with_one_key_acts_once(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    args = {"claim": "Acme Corp was founded in 2019 in Leeds.", "evidence": EVIDENCE}
    jev = ScriptedJev()

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, jev)
        first = rt.call("brain_propose_fact", args, idempotency_key="task-7:fact-1")
        again = rt.call("brain_propose_fact", args, idempotency_key="task-7:fact-1")

    assert first.status == "ok" and first.output["outcome"] == "accepted"
    assert again.replayed and again.output == first.output
    assert len(jev.calls_for("support")) == 1, "the gate ran once"
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select count(*) as n from public.facts where org_id = %s", (str(tenants.org_a),)
        )
        assert cursor.fetchone()["n"] == 1


def test_without_a_key_the_same_call_in_the_same_run_is_still_one_action(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    args = {"claim": "Acme Corp was founded in 2019 in Leeds.", "evidence": EVIDENCE}
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, ScriptedJev())
        rt.call("brain_propose_fact", args)
        again = rt.call("brain_propose_fact", args)

    assert again.replayed


# --- Untrusted content -------------------------------------------------------------


def test_an_injection_inside_a_fetched_page_is_quarantined_before_the_agent_sees_it(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        result = runtime(conn, tenants, agent).call(
            "web_fetch_preview", {"url": "https://acme.example/planted"}
        )

    assert result.status == "ok"
    assert result.output["screened"] != "clean"
    assert "claims" not in result.output and "ignore" not in result.text.lower()


def test_a_clean_page_reaches_the_agent_as_claims(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        result = runtime(conn, tenants, agent).call(
            "web_fetch_preview", {"url": "https://acme.example/about"}
        )

    assert result.output["screened"] == "clean" and result.output["claims"]


def test_documents_are_read_in_the_agents_own_scope(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    from app.knowledge.library import Library

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent)
        rt.context.library = Library(conn, embedder=HashingEmbedder())
        result = rt.call("read_document", {"query": "turbines"})

    assert result.status == "ok" and result.output == {"passages": []}


# --- Holding risky tools -------------------------------------------------------------


class SendArgs(BaseModel):
    to: str
    body: str


@pytest.fixture
def send_email(db: psycopg.Connection, tenants: Tenants, agent: UUID) -> Iterator[list[Any]]:
    sent: list[Any] = []
    register(
        ToolSpec(
            name="send_email_test",
            description="Send an email.",
            args=SendArgs,
            risk_class="R4",
            side_effect=True,
            handler=lambda ctx, args: sent.append(args) or {"sent": True},
        )
    )
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    allow(db, agent, [*STARTING, "send_email_test"])
    yield sent
    REGISTRY.pop("send_email_test")


def test_an_irreversible_tool_is_held_for_the_owner_and_not_run(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, send_email: list[Any]
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent)
        held = rt.call("send_email_test", {"to": "a@example.com", "body": "hi"})
        again = rt.call("send_email_test", {"to": "a@example.com", "body": "hi"})

    assert held.status == "held" and send_email == []
    assert again.replayed and again.output["approval_id"] == held.output["approval_id"]
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("select action_type, status, payload from public.approvals")
        rows = cursor.fetchall()
    assert len(rows) == 1 and rows[0]["action_type"] == "tool_call"
    assert rows[0]["payload"]["tool"] == "send_email_test"


def test_the_database_will_not_let_an_r4_tool_run_without_approval(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, send_email: list[Any]
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        with as_service_role(db) as conn, conn.cursor() as cursor:
            cursor.execute(
                "update public.tools set approval = 'auto' where name = 'send_email_test'"
            )


def test_tool_output_is_capped(db: psycopg.Connection, tenants: Tenants, agent: UUID) -> None:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute("update public.tools set max_output_chars = 100 where name = 'brain_search'")
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        brain = Brain(conn, HashingEmbedder())
        for i in range(5):
            brain.insert_fact(
                org_id=tenants.org_a,
                claim=f"Acme fact number {i} is a long sentence about wind turbines in Leeds.",
                admission=admit(conn, tenants.org_a),
            )

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        result = runtime(conn, tenants, agent).call("brain_search", {"query": "Acme wind"})

    assert result.output["truncated"] is True and len(result.text) < 200


# --- LangChain and selection ------------------------------------------------------


def test_langchain_tools_go_through_the_runtime(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    allow(db, agent, ["brain_search"])
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        tools = langchain_tools(runtime(conn, tenants, agent))
        text = tools[0].invoke({"query": "Acme founding"})

    assert [t.name for t in tools] == ["brain_search"]
    assert '"facts"' in text
    assert [c["tool"] for c in calls(db, agent)] == ["brain_search"]


def test_a_long_tool_list_is_narrowed_by_jev(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    extra = [f"dummy_{i}" for i in range(5)]
    for name in extra:
        register(
            ToolSpec(
                name=name,
                description=f"Dummy {name}.",
                args=SendArgs,
                risk_class="R0",
                handler=lambda ctx, args: {},
            )
        )
    try:
        seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
        allow(db, agent, [*STARTING, *extra])

        def rank(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
            options = list(questions["tool"].criteria)
            answer = choice("brain_search", options, p=0.6)
            answer.probabilities.update({"read_document": 0.2, "web_fetch_preview": 0.1})
            return {"tool": answer}

        jev = ScriptedJev(respond=rank)
        with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
            rt = runtime(conn, tenants, agent, jev)
            offered = offered_tools(
                rt,
                Judge(conn, rt.context.gateway),
                task="What do we know about Acme?",
                org_id=tenants.org_a,
                agent_id=agent,
            )
    finally:
        for name in extra:
            REGISTRY.pop(name)

    assert [t["name"] for t in offered] == ["brain_search", "read_document", "web_fetch_preview"]
    asked = jev.calls[0]["questions"]["tool"].criteria
    assert "none_fit" in asked and "dummy_0" in asked


def test_a_short_tool_list_is_offered_whole_without_asking(
    db: psycopg.Connection, tenants: Tenants, agent: UUID
) -> None:
    jev = ScriptedJev()
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, jev)
        offered = offered_tools(
            rt, Judge(conn, rt.context.gateway), task="x", org_id=tenants.org_a, agent_id=agent
        )

    assert len(offered) == 4 and jev.calls == []
