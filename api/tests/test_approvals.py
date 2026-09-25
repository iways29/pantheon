"""Step 7.5: approvals, the decision desk and the tool-risk gate (ADR 021).

These drive `ToolRuntime` directly inside one rolled-back transaction. The
run-level behaviour (a run pausing at a held call and resuming once the
owner decides) is in test_approval_runs.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import BaseModel

from app.agents import prompts
from app.approvals import ApprovalError, decide, list_pending, remember
from app.brain import Brain, HashingEmbedder
from app.brain.write_gate import BrainWriter
from app.db import acting_as, as_service_role
from app.gateway import Gateway, ModelResponse
from app.judge import Judge
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.tools import REGISTRY, ToolContext, ToolRuntime, ToolSpec, register, seed_tools
from tests.conftest_db import Tenants
from tests.scripted_jev import ScriptedJev, choice
from tests.test_gateway import TIERS, make_agent
from tests.test_judge import set_price


@dataclass
class Explainer:
    """The cheap model that writes the card's explanation."""

    calls: list[Any] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(
            model=model,
            text="Publishes a post; the owner approved similar posts before.",
            tokens_in=50,
            tokens_out=10,
            cost_usd=0.0001,
            latency_ms=1,
            provider="scripted",
        )


class PostArgs(BaseModel):
    text: str


@dataclass
class Effects:
    published: list[str] = field(default_factory=list)
    noted: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)


@pytest.fixture
def effects(db: psycopg.Connection, tenants: Tenants) -> Iterator[Effects]:
    done = Effects()
    specs = [
        ToolSpec(
            name="publish_test",
            description="Publish a post on the website.",
            args=PostArgs,
            risk_class="R4",
            side_effect=True,
            approval="approval",
            handler=lambda ctx, a: done.published.append(a.text) or {"published": a.text},
        ),
        ToolSpec(
            name="note_test",
            description="Add a note to a shared calendar.",
            args=PostArgs,
            risk_class="R3",
            side_effect=True,
            handler=lambda ctx, a: done.noted.append(a.text) or {"noted": a.text},
        ),
        ToolSpec(
            name="lookup_test",
            description="Look something up on a public website.",
            args=PostArgs,
            risk_class="R2",
            handler=lambda ctx, a: done.read.append(a.text) or {"screened": "clean"},
        ),
    ]
    for spec in specs:
        register(spec)
    try:
        yield done
    finally:
        for spec in specs:
            REGISTRY.pop(spec.name)


@pytest.fixture
def agent(db: psycopg.Connection, tenants: Tenants, effects: Effects) -> UUID:
    agent_id = make_agent(db, tenants.org_a, name="publisher")
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=STARTER_GATES)
    seed_tools(db, user_id=tenants.user_a, org_id=tenants.org_a)
    with as_service_role(db) as conn:
        conn.execute(
            "update public.agents set allowed_tools = %s where id = %s",
            (["publish_test", "note_test", "lookup_test", "brain_search"], str(agent_id)),
        )
    return agent_id


def runtime(
    conn: psycopg.Connection,
    tenants: Tenants,
    agent: UUID,
    jev: ScriptedJev | None,
    *,
    model: Explainer | None = None,
) -> ToolRuntime:
    gateway = Gateway(conn, model or Explainer(), TIERS, systemone=jev)
    judge = Judge(conn, gateway) if jev is not None else None
    return ToolRuntime(
        ToolContext(
            connection=conn,
            org_id=tenants.org_a,
            agent_id=agent,
            agent_name="publisher",
            gateway=gateway,
            brain=Brain(conn, HashingEmbedder()),
            judge=judge,
        )
    )


def owner_rule(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, jev: ScriptedJev, text: str
) -> None:
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        gateway = Gateway(conn, Explainer(), TIERS, systemone=jev)
        brain = Brain(conn, HashingEmbedder())
        result = remember(
            BrainWriter(conn, brain, Judge(conn, gateway)),
            org_id=tenants.org_a,
            agent_id=agent,
            statement=text,
            ref="policy:test",
        )
    assert result.outcome == "accepted", result.reasons


def approval(db: psycopg.Connection) -> dict[str, Any]:
    with as_service_role(db) as conn:
        rows = conn.execute("select * from public.approvals").fetchall()
    assert len(rows) == 1
    return rows[0]


def events(db: psycopg.Connection, *types: str) -> list[dict[str, Any]]:
    with as_service_role(db) as conn:
        return conn.execute(
            "select type, payload from public.events where type = any(%s) order by created_at, id",
            (list(types),),
        ).fetchall()


# --- Holding, with a decision card ----------------------------------------------------


def test_an_r4_call_is_held_with_a_recommendation_facts_and_an_explanation(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    jev = ScriptedJev(choices={"recommendation": "approve"})
    owner_rule(db, tenants, agent, jev, "Posts on the website are written in plain English.")
    prompts.publish(
        db,
        user_id=tenants.user_a,
        agent_id=agent,
        slot="explain",
        body="Explain to the owner, in two sentences, what this action does.",
    )
    model = Explainer()

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        held = runtime(conn, tenants, agent, jev, model=model).call(
            "publish_test", {"text": "Our new post"}
        )

    assert held.status == "held" and effects.published == []
    card = approval(db)
    assert card["id"] == UUID(held.output["approval_id"])
    assert card["action_key"] == "tool:publish_test"
    assert card["agent_id"] == agent and card["tool_call_id"] == held.call_id
    assert card["recommendation"] == "approve"
    assert card["recommendation_probs"]["approve"] == pytest.approx(0.95)
    assert card["recommendation_request_id"] is not None
    assert [f["claim"] for f in card["facts_checked"]] == [
        "Posts on the website are written in plain English."
    ]
    assert card["explanation"].startswith("Publishes a post")
    assert card["conflicts"] == []
    assert len(model.calls) == 1, "one cheap call for the explanation"
    # The recommendation is a judgment like any other, stored raw.
    with as_service_role(db) as conn:
        stored = conn.execute(
            "select count(*) as n from public.judgments where request_id = %s",
            (card["recommendation_request_id"],),
        ).fetchone()["n"]
    assert stored == 1
    assert [e["type"] for e in events(db, "approval_requested")] == ["approval_requested"]


def test_without_typesafe_a_held_call_asks_the_owner_to_look_closer(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        held = runtime(conn, tenants, agent, None).call("publish_test", {"text": "Hi"})

    assert held.status == "held"
    card = approval(db)
    assert card["recommendation"] == "look_closer" and card["recommendation_probs"] is None
    assert card["explanation"] is None, "no explain prompt, no model call"


def test_a_proposal_that_goes_against_an_owner_decision_says_so(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    jev = ScriptedJev(
        nouls={"conflicts": 0.92},
        respond=lambda state, questions: (
            {"recommendation": choice("reject", list(questions["recommendation"].criteria))}
            if "recommendation" in questions
            else {}
        ),
    )
    owner_rule(db, tenants, agent, jev, "The owner never publishes posts on a Sunday.")

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        runtime(conn, tenants, agent, jev).call("publish_test", {"text": "Sunday special"})

    card = approval(db)
    assert card["recommendation"] == "reject"
    assert [(c["claim"], c["outcome"]) for c in card["conflicts"]] == [
        ("The owner never publishes posts on a Sunday.", "conflict")
    ]
    assert card["conflicts"][0]["probability"] == pytest.approx(0.92)
    (asked,) = jev.calls_for("conflicts")
    assert asked["state"]["owner_decision"] == "The owner never publishes posts on a Sunday."


def test_a_consistent_proposal_shows_no_conflict(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    jev = ScriptedJev()
    owner_rule(db, tenants, agent, jev, "The owner never publishes posts on a Sunday.")

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        runtime(conn, tenants, agent, jev).call("publish_test", {"text": "Monday post"})

    assert approval(db)["conflicts"] == []


# --- Deciding ---------------------------------------------------------------------------


def test_approving_with_edits_runs_the_call_once_with_the_approved_arguments(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    jev = ScriptedJev(choices={"recommendation": "approve"})
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        held = runtime(conn, tenants, agent, jev).call("publish_test", {"text": "Draft"})

    decided = decide(
        db,
        user_id=tenants.user_a,
        approval_id=held.output["approval_id"],
        decision="approve",
        edited_arguments={"text": "Edited by the owner"},
    )
    assert decided["status"] == "approved"

    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, jev)
        ran = rt.call("publish_test", {"text": "Draft"})
        again = rt.call("publish_test", {"text": "Draft"})

    assert ran.status == "ok" and ran.output == {"published": "Edited by the owner"}
    assert again.replayed and again.output == ran.output
    assert effects.published == ["Edited by the owner"], "ran once, as edited"
    with as_service_role(db) as conn:
        rows = conn.execute(
            "select status, arguments from public.tool_calls where tool = 'publish_test'"
        ).fetchall()
    assert rows == [{"status": "ok", "arguments": {"text": "Edited by the owner"}}]
    # The whole trail is in events (one transaction here, so one timestamp:
    # the order is checked in test_approval_runs).
    trail = sorted(
        (e["type"], e["payload"].get("status") or e["payload"].get("decision"))
        for e in events(db, "tool_called", "approval_requested", "approval_decided")
    )
    assert trail == [
        ("approval_decided", "approve"),
        ("approval_requested", None),
        ("tool_called", "held"),
        ("tool_called", "ok"),
    ]
    decided_event = events(db, "approval_decided")[0]["payload"]
    assert decided_event["edited"] is True and decided_event["recommendation"] == "approve"


def test_a_redirect_returns_the_owners_note_and_nothing_runs(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        held = runtime(conn, tenants, agent, ScriptedJev()).call("publish_test", {"text": "x"})

    decide(
        db,
        user_id=tenants.user_a,
        approval_id=held.output["approval_id"],
        decision="redirect",
        note="Save it as a draft instead.",
    )
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        after = runtime(conn, tenants, agent, ScriptedJev()).call("publish_test", {"text": "x"})

    assert after.status == "refused" and "Save it as a draft instead." in after.output["error"]
    assert effects.published == []


def test_deciding_twice_changes_nothing_and_a_redirect_needs_a_note(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        held = runtime(conn, tenants, agent, ScriptedJev()).call("publish_test", {"text": "x"})
    approval_id = held.output["approval_id"]

    assert list_pending(db, user_id=tenants.user_a)[0]["id"] == UUID(approval_id)
    with pytest.raises(ApprovalError):
        decide(db, user_id=tenants.user_a, approval_id=approval_id, decision="redirect")
    decide(db, user_id=tenants.user_a, approval_id=approval_id, decision="cancel", note="No.")
    second = decide(db, user_id=tenants.user_a, approval_id=approval_id, decision="approve")

    assert second["status"] == "rejected"
    assert list_pending(db, user_id=tenants.user_a) == []
    assert len(events(db, "approval_decided")) == 1


def test_an_agent_can_never_decide_its_own_approval(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        held = runtime(conn, tenants, agent, ScriptedJev()).call("publish_test", {"text": "x"})
    approval_id = held.output["approval_id"]

    attempts = [
        ("select public.decide_approval(%s, 'approve')", (approval_id,)),
        ("update public.approvals set status = 'approved', decided_at = now()", ()),
        ("update public.tool_calls set status = 'approved' where id = %s", (str(held.call_id),)),
        ("update public.tool_calls set arguments = '{}' where id = %s", (str(held.call_id),)),
    ]
    for sql, params in attempts:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
                conn.execute(sql, params)
    assert approval(db)["status"] == "pending"


# --- The tool-risk gate ----------------------------------------------------------------


def test_a_low_risk_call_runs_only_above_its_threshold(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    jev = ScriptedJev()
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, jev)
        clear = rt.call("note_test", {"text": "clearly safe"})
        jev.nouls["external_effect"] = 0.2  # unsure: an R3 call asks, an R2 read runs
        unsure_r3 = rt.call("note_test", {"text": "maybe seen outside"})
        unsure_r2 = rt.call("lookup_test", {"text": "maybe seen outside"})
        jev.nouls["external_effect"] = 0.02
        jev.nouls["off_task"] = 0.95
        off_task = rt.call("note_test", {"text": "nothing to do with the task"})

    assert clear.status == "ok" and effects.noted == ["clearly safe"]
    assert unsure_r3.status == "held"
    assert unsure_r2.status == "ok" and effects.read == ["maybe seen outside"]
    assert off_task.status == "refused" and "does not serve the task" in off_task.output["error"]
    assert len(jev.calls_for("irreversible")) == 4, "every R2 and R3 call was checked"
    assert "external_effect" in approval(db)["payload"]["reason"]


def test_without_typesafe_an_outside_effect_waits_for_a_person(
    db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, None)
        r3 = rt.call("note_test", {"text": "a"})
        r2 = rt.call("lookup_test", {"text": "a"})
        r0 = rt.call("brain_search", {"query": "anything"})

    assert (r3.status, r2.status, r0.status) == ("held", "ok", "ok"), r0.output
    assert effects.noted == []


# --- Agreement (right-hand idea 2) ------------------------------------------------------


def test_agreement_counts_how_often_the_recommendation_matched_the_owner(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    rows = [
        ("approve", "approved"),
        ("approve", "approved"),
        ("approve", "rejected"),
        ("reject", "rejected"),
        ("look_closer", "approved"),
        (None, "approved"),
        ("approve", "pending"),
    ]
    with as_service_role(db) as conn, conn.cursor() as cursor:
        for recommendation, status in rows:
            cursor.execute(
                "insert into public.approvals (org_id, action_type, action_key, recommendation, "
                "status, decided_at) values (%s, 'tool_call', 'tool:publish_test', %s, %s, "
                "case when %s = 'pending' then null else now() end)",
                (str(tenants.org_a), recommendation, status, status),
            )
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        row = conn.execute(
            "select decided, recommended, agreed, agreement from public.approval_agreement "
            "where action_key = 'tool:publish_test'"
        ).fetchone()
    with acting_as(db, user_id=str(tenants.user_b)) as conn:
        hidden = conn.execute("select count(*) as n from public.approval_agreement").fetchone()

    assert (row["decided"], row["recommended"], row["agreed"]) == (6, 5, 3)
    assert float(row["agreement"]) == pytest.approx(0.6)
    assert hidden["n"] == 0, "RLS: another org sees none of it"


# --- Through the API ----------------------------------------------------------------


@pytest.fixture
def api(db: psycopg.Connection, agent: UUID, settings: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app
    from app.owner_api import get_connection, get_writer_factory
    from tests.test_health import auth, make_token

    def same_connection() -> Any:
        yield db

    def writer(conn: psycopg.Connection, _agent: UUID) -> BrainWriter:
        gateway = Gateway(conn, Explainer(), TIERS, systemone=ScriptedJev())
        return BrainWriter(conn, Brain(conn, HashingEmbedder()), Judge(conn, gateway))

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_connection] = same_connection
    app.dependency_overrides[get_writer_factory] = lambda: writer
    yield TestClient(app, headers=auth(make_token()))
    app.dependency_overrides.clear()


def test_the_owner_sees_decides_and_the_reason_becomes_a_fact(
    api: Any, db: psycopg.Connection, tenants: Tenants, agent: UUID, effects: Effects
) -> None:
    jev = ScriptedJev(choices={"recommendation": "approve"})
    with acting_as(db, user_id=str(tenants.user_a), agent_id=str(agent)) as conn:
        rt = runtime(conn, tenants, agent, jev)
        first = rt.call("publish_test", {"text": "One"})
        second = rt.call("publish_test", {"text": "Two"})

    pending = api.get("/approvals")
    assert pending.status_code == 200, pending.text
    assert {p["id"] for p in pending.json()} == {
        first.output["approval_id"],
        second.output["approval_id"],
    }
    assert all(p["recommendation"] == "approve" for p in pending.json())

    approved = api.post(
        f"/approvals/{first.output['approval_id']}/approve",
        json={"edited_arguments": {"text": "One, edited"}},
    )
    rejected = api.post(
        f"/approvals/{second.output['approval_id']}/reject",
        json={"mode": "redirect", "note": "We do not post twice in one day."},
    )
    bad = api.post(f"/approvals/{uuid4()}/approve", json={})

    assert approved.json()["status"] == "approved" and approved.json()["remembered"] is None
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["remembered"] == "accepted"
    assert bad.status_code == 404
    assert api.get("/approvals").json() == []
    with as_service_role(db) as conn:
        fact = conn.execute("select claim, source from public.facts").fetchone()
    assert fact == {
        "claim": "The owner rejected a request to use publish_test: "
        "We do not post twice in one day.",
        "source": "owner",
    }


def test_a_standing_rule_is_written_to_the_brain_as_the_owners(
    api: Any, db: psycopg.Connection, tenants: Tenants
) -> None:
    made = api.post(
        "/policies",
        json={"statement": "Newsletters are always written in English.", "agent": "publisher"},
    )
    again = api.post(
        "/policies",
        json={"statement": "Newsletters are always written in English.", "agent": "publisher"},
    )

    assert made.status_code == 201 and made.json()["outcome"] == "accepted", made.text
    assert again.json()["outcome"] in ("accepted", "duplicate")
    with as_service_role(db) as conn:
        rows = conn.execute("select source from public.facts").fetchall()
    assert rows == [{"source": "owner"}]
