"""Step 8.3: Marketing and Content drafts, their checks and their facts (ADR 029).

Acceptance: a draft relies only on public facts and records them; every
factual sentence is checked against the brain, and an unsupported one blocks
the draft and names the sentence; banned words and Devanagari are blocked in
code; a passing draft waits on the owner's approval card, and the owner's
edits are kept; only a person approves, rejects, posts, or makes a fact public.

Jev is scripted; everything else is real.
"""

import dataclasses
import json
import re
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.agents.runs import Runtime, advance_run
from app.approvals import decide
from app.brain import Brain, HashingEmbedder
from app.content import check, save
from app.content.drafts import sentences
from app.db import acting_as, as_service_role, connect
from app.departments.apply import apply_charter, enable
from app.departments.charter import publish
from app.departments.report import department_report
from app.departments.starter_charters import MARKETING
from app.gateway import Gateway, ModelResponse, ToolCall
from app.judge import Judge
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.knowledge.wiring import agent_services
from app.tools import seed_tools
from app.tracing import NullTracer
from tests.conftest_db import Tenants, admit
from tests.scripted_jev import ScriptedJev, choice
from tests.test_gateway import TIERS, RecordingTransport, make_agent
from tests.test_judge import MODEL, set_price
from tests.test_research_department import Lab
from tests.test_runners import TIERS as RUN_TIERS
from tests.test_runners import status, tick

BANNED = ["revolutionary", "Harvey Specter"]
MUMBA = "Mumba.ai lets people branch any AI reply into parallel threads."
INTERNAL = "The owner rejected a request to use generate_video."

GOOD_VOICE = {"on_brand": 2.0, "specific": 1.5, "clear": 2.0}


def claims_from(verdicts: dict[str, str]) -> Any:
    """A Jev that judges each sentence as `verdicts` says (default: not a claim)."""

    def respond(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        if "claim" not in questions:
            return {}
        options = list(questions["claim"].criteria)
        for fragment, verdict in verdicts.items():
            if fragment in state["sentence"]:
                return {"claim": choice(verdict, options)}
        return {"claim": choice("not_a_claim", options)}

    return respond


@dataclasses.dataclass
class Desk:
    db: psycopg.Connection
    tenants: Tenants
    agent: UUID
    public_fact: UUID
    internal_fact: UUID


@pytest.fixture
def desk(db: psycopg.Connection, tenants: Tenants) -> Iterator[Desk]:
    agent = make_agent(db, tenants.org_a, name="writer")
    set_price(db, tenants.org_a)
    seed_gates(db, user_id=tenants.user_a, org_id=tenants.org_a, gates=STARTER_GATES)
    with acting_as(db, user_id=str(tenants.user_a)) as conn:
        brain = Brain(conn, HashingEmbedder())
        public = brain.insert_fact(
            org_id=tenants.org_a,
            claim=MUMBA,
            source="web:mumba.ai",
            admission=dataclasses.replace(admit(conn, tenants.org_a), visibility="public"),
        )
        internal = brain.insert_fact(
            org_id=tenants.org_a,
            claim=INTERNAL,
            source="owner",
            admission=admit(conn, tenants.org_a),
        )
    yield Desk(db, tenants, agent, public.id, internal.id)


def write(desk: Desk, body: str, *, facts: list[UUID] | None = None, key: str = "k1") -> Any:
    with acting_as(desk.db, user_id=str(desk.tenants.user_a)) as conn, conn.cursor() as cursor:
        return save(
            cursor,
            org_id=desk.tenants.org_a,
            agent_id=desk.agent,
            channel="x",
            format="post",
            title="Branching thought",
            body=body,
            idempotency_key=key,
            fact_ids=[str(f) for f in facts or []],
        )


def run_checks(desk: Desk, draft_id: UUID, jev: ScriptedJev) -> Any:
    with acting_as(desk.db, user_id=str(desk.tenants.user_a)) as conn, conn.cursor() as cursor:
        gateway = Gateway(conn, RecordingTransport(), TIERS, systemone=jev)
        return check(
            cursor,
            judge=Judge(conn, gateway),
            brain=Brain(conn, HashingEmbedder()),
            org_id=desk.tenants.org_a,
            agent_id=desk.agent,
            draft_id=draft_id,
            banned_phrases=BANNED,
        )


def rows(db: psycopg.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    with as_service_role(db) as conn, conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def draft_row(desk: Desk, draft_id: UUID) -> dict[str, Any]:
    return rows(desk.db, "select * from public.drafts where id = %s", str(draft_id))[0]


# --- Sentences -------------------------------------------------------------------------


def test_sentences_drop_markup_and_short_fragments() -> None:
    body = "# Heading\n\nOne fort at a time. Mumba has 3 branches!\n- A list item here\nOk."
    assert sentences(body) == ["One fort at a time.", "Mumba has 3 branches!", "A list item here"]


# --- Saving ----------------------------------------------------------------------------


def test_a_draft_records_only_the_public_facts_it_relied_on(desk: Desk) -> None:
    saved = write(desk, "Branch any reply.", facts=[desk.public_fact, desk.internal_fact])

    assert saved.relied_on == [str(desk.public_fact)]
    assert "internal" in saved.skipped[str(desk.internal_fact)]
    again = write(desk, "Branch any reply.", facts=[desk.public_fact])
    assert again.draft_id == saved.draft_id, "saving twice changes nothing"
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="internal"),
        acting_as(desk.db, user_id=str(desk.tenants.user_a)) as conn,
    ):
        conn.execute(
            "insert into public.artifact_claims (org_id, draft_id, fact_id, relation) "
            "values (%s, %s, %s, 'relied_on')",
            (str(desk.tenants.org_a), str(saved.draft_id), str(desk.internal_fact)),
        )


# --- Checking --------------------------------------------------------------------------


def test_a_supported_draft_goes_to_the_owner_as_an_approval_card(desk: Desk) -> None:
    saved = write(desk, f"{MUMBA} Think in branches.", facts=[desk.public_fact])
    jev = ScriptedJev(respond=claims_from({"Mumba.ai": "supported"}), scores=GOOD_VOICE)

    result = run_checks(desk, saved.draft_id, jev)

    assert result.status == "ready" and not result.blocking
    assert result.voice_score and result.voice_score > 0.5
    draft = draft_row(desk, saved.draft_id)
    assert draft["status"] == "ready" and draft["checks"]["sentences_checked"] == 2
    (card,) = rows(
        desk.db, "select * from public.approvals where id = %s", str(draft["approval_id"])
    )
    assert card["action_type"] == "draft_review" and card["status"] == "pending"
    assert card["payload"]["body"] == draft["body"]
    relations = rows(
        desk.db,
        "select relation, verdict from public.artifact_claims "
        "where draft_id = %s order by relation",
        str(saved.draft_id),
    )
    assert [(r["relation"], r["verdict"]) for r in relations] == [
        ("checked", "supported"),
        ("relied_on", None),
    ]
    assert len(jev.calls_for("claim")) == 2, "one claim check per sentence"


def test_an_unsupported_claim_blocks_the_draft_and_names_the_sentence(desk: Desk) -> None:
    saved = write(desk, "Mumba.ai has 40,000 users. Think in branches.")
    jev = ScriptedJev(respond=claims_from({"40,000": "unsupported"}), scores=GOOD_VOICE)

    result = run_checks(desk, saved.draft_id, jev)

    assert result.status == "blocked"
    (problem,) = result.blocking
    assert problem["sentence"] == "Mumba.ai has 40,000 users."
    assert "Not in the brain" in problem["reason"]
    assert draft_row(desk, saved.draft_id)["approval_id"] is None
    assert rows(desk.db, "select 1 from public.approvals where action_type = 'draft_review'") == []


def test_banned_words_and_devanagari_are_blocked_in_code(desk: Desk) -> None:
    saved = write(desk, "A revolutionary canvas, Harvey Specter style. कर्म")
    result = run_checks(desk, saved.draft_id, ScriptedJev(scores=GOOD_VOICE))

    reasons = " ".join(b["reason"] for b in result.blocking)
    assert result.status == "blocked"
    assert "'revolutionary'" in reasons and "'Harvey Specter'" in reasons
    assert "Devanagari" in reasons


def test_an_off_brand_voice_is_sent_back(desk: Desk) -> None:
    saved = write(desk, "Think in branches.")
    jev = ScriptedJev(nouls={"hype": 0.9}, scores=GOOD_VOICE)

    result = run_checks(desk, saved.draft_id, jev)

    assert result.status == "blocked"
    assert result.blocking[0]["check"] == "draft_voice" and "Hype" in result.blocking[0]["reason"]


def test_fund_language_reaches_the_owner_flagged(desk: Desk) -> None:
    saved = write(desk, "We are looking for our first backers.")
    jev = ScriptedJev(nouls={"fund_solicitation": 0.6}, scores=GOOD_VOICE)

    result = run_checks(desk, saved.draft_id, jev)

    assert result.status == "ready"
    assert "fund solicitation" in result.flags[0]["reason"]
    (card,) = rows(desk.db, "select recommendation from public.approvals")
    assert card["recommendation"] == "look_closer"


# --- The owner decides -------------------------------------------------------------------


def ready_draft(desk: Desk) -> Any:
    saved = write(desk, "Think in branches.")
    return run_checks(desk, saved.draft_id, ScriptedJev(scores=GOOD_VOICE))


def test_the_owners_edit_is_kept_as_a_voice_example(desk: Desk) -> None:
    ready = ready_draft(desk)
    edited = "Think in branches. Merge the best."

    decide(
        desk.db,
        user_id=desk.tenants.user_a,
        approval_id=ready.approval_id,
        decision="approve",
        edited_arguments={"body": edited},
    )

    draft = draft_row(desk, ready.draft_id)
    assert draft["status"] == "approved" and draft["owner_body"] == edited
    events = rows(desk.db, "select payload from public.events where type = 'draft_approved'")
    assert events[0]["payload"]["edited"] is True

    with acting_as(desk.db, user_id=str(desk.tenants.user_a)) as conn:
        conn.execute(
            "update public.drafts set status = 'published', published_url = %s, "
            "published_at = now() where id = %s",
            ("https://x.com/theunreallab/status/1", str(ready.draft_id)),
        )
    assert draft_row(desk, ready.draft_id)["status"] == "published"


def test_a_rejection_keeps_the_owners_note(desk: Desk) -> None:
    ready = ready_draft(desk)
    decide(
        desk.db,
        user_id=desk.tenants.user_a,
        approval_id=ready.approval_id,
        decision="redirect",
        note="Too abstract; name the feature.",
    )
    draft = draft_row(desk, ready.draft_id)
    assert (
        draft["status"] == "rejected" and draft["owner_note"] == "Too abstract; name the feature."
    )


def test_only_a_person_approves_posts_or_makes_a_fact_public(desk: Desk) -> None:
    ready = ready_draft(desk)
    agent_session = {"user_id": str(desk.tenants.user_a), "agent_id": str(desk.agent)}

    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        acting_as(desk.db, **agent_session) as conn,
    ):
        conn.execute(
            "update public.drafts set status = 'approved' where id = %s", (str(ready.draft_id),)
        )
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        acting_as(desk.db, **agent_session) as conn,
    ):
        conn.execute(
            "update public.facts set visibility = 'public' where id = %s",
            (str(desk.internal_fact),),
        )
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="approved"),
        acting_as(desk.db, user_id=str(desk.tenants.user_a)) as conn,
    ):
        conn.execute(
            "update public.drafts set status = 'published' where id = %s", (str(ready.draft_id),)
        )

    with acting_as(desk.db, user_id=str(desk.tenants.user_a)) as conn:
        conn.execute(
            "update public.facts set visibility = 'public' where id = %s",
            (str(desk.internal_fact),),
        )
    (event,) = rows(
        desk.db, "select payload from public.events where type = 'fact_visibility_changed'"
    )
    assert event["payload"]["to"] == "public"


def test_a_ready_draft_needs_its_approval_card(desk: Desk) -> None:
    saved = write(desk, "Think in branches.")
    with (
        pytest.raises(psycopg.errors.CheckViolation, match="draft_review"),
        acting_as(desk.db, user_id=str(desk.tenants.user_a)) as conn,
    ):
        conn.execute(
            "update public.drafts set status = 'ready' where id = %s", (str(saved.draft_id),)
        )


# --- One unattended morning, end to end --------------------------------------------------

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A Monday, 06:50 in New York.
MORNING = "2026-09-28 10:50:00+00"


@dataclasses.dataclass
class ContentTeam:
    """Plays the Content Lead, the topic researcher, the writer and the editor."""

    calls: list[str] = dataclasses.field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        system = " ".join(m["content"] or "" for m in messages if m["role"] == "system")
        user = next(m["content"] for m in messages if m["role"] == "user")
        done = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]

        if "You lead Marketing and Content" in system:
            results = user.count('"agent":')
            if results == 0:
                steps = [
                    ("list_drafts", {}),
                    (
                        "create_task",
                        {
                            "assign_to": "topic-researcher",
                            "title": "Three ideas",
                            "instructions": "Three topic ideas for today.",
                        },
                    ),
                ]
            elif results == 1:
                fact = _UUID.findall(user.split("Results from your sub-tasks")[1])[-1]
                steps = [
                    (
                        "create_task",
                        {
                            "assign_to": "writer",
                            "title": "One X post",
                            "instructions": f"Write an X post on branching. Rely on fact {fact}.",
                        },
                    )
                ]
            elif results == 2:
                draft = re.search(r"draft ([0-9a-f-]{36})", user).group(1)
                steps = [
                    (
                        "create_task",
                        {
                            "assign_to": "editor",
                            "title": "Check the draft",
                            "instructions": f"Check draft {draft} and fix it if it fails.",
                        },
                    )
                ]
            else:
                steps = [("report_result", {"summary": "Three ideas; one X post waiting."})]
            return self._next("lead", steps, done)
        if "You find angles" in system:
            if not done:
                return self._tool("researcher", "brain_search", {"query": "Mumba branching"})
            fact = done[0]["facts"][0]["id"]
            return self._next(
                "researcher",
                [
                    ("brain_search", {"query": "Mumba branching"}),
                    ("report_result", {"summary": f"Idea 1: branching, fact {fact}."}),
                ],
                done,
            )
        if "You write one piece" in system:
            fact = _UUID.findall(user)[-1]
            if not done:
                return self._tool(
                    "writer",
                    "save_draft",
                    {
                        "channel": "x",
                        "format": "post",
                        "title": "Branch any reply",
                        "body": f"{MUMBA} Think in branches.",
                        "fact_ids": [fact],
                    },
                )
            draft = done[0]["draft_id"]
            return self._next(
                "writer",
                [("save_draft", {}), ("report_result", {"summary": f"Saved draft {draft}."})],
                done,
            )
        if "You check drafts" in system:
            draft = re.search(r"draft ([0-9a-f-]{36})", user).group(1)
            return self._next(
                "editor",
                [
                    ("check_draft", {"draft_id": draft}),
                    ("report_result", {"summary": f"Draft {draft} is ready."}),
                ],
                done,
            )
        raise AssertionError(f"Unexpected call: {system[:80]}")

    def _next(self, who: str, steps: list[tuple[str, dict]], done: list[Any]) -> ModelResponse:
        if len(done) < len(steps):
            return self._tool(who, *steps[len(done)])
        return self._say(who)

    def _tool(self, who: str, name: str, args: dict[str, Any]) -> ModelResponse:
        self.calls.append(f"{who}:{name}")
        return ModelResponse(
            model=kw_model(who),
            text="",
            tokens_in=300,
            tokens_out=40,
            cost_usd=0.0003,
            latency_ms=1,
            provider="scripted",
            tool_calls=(ToolCall(f"c{len(self.calls)}", name, json.dumps(args)),),
            finish_reason="tool_calls",
        )

    def _say(self, who: str) -> ModelResponse:
        self.calls.append(f"{who}:say")
        return ModelResponse(
            model=kw_model(who),
            text="Done.",
            tokens_in=300,
            tokens_out=10,
            cost_usd=0.0003,
            latency_ms=1,
            provider="scripted",
            finish_reason="stop",
        )


def kw_model(who: str) -> str:
    return "vendor/mid" if who == "writer" else "vendor/small"


@pytest.fixture
def lab(dsn: str) -> Iterator[Lab]:
    try:
        connection = connect(dsn)
    except psycopg.OperationalError as error:
        pytest.skip(f"No database at {dsn}: {error}")
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into auth.users (id, email) values (%s, %s)", (str(user_id), f"{user_id}@x.com")
        )
        cursor.execute("insert into public.orgs (id, name) values (%s, 'lab')", (str(org_id),))
        cursor.execute(
            "insert into public.org_members (org_id, user_id) values (%s, %s)",
            (str(org_id), str(user_id)),
        )
        cursor.execute(
            "insert into public.model_prices (org_id, provider, model, input_usd_per_mtok) "
            "values (%s, 'typesafe', %s, 0.042)",
            (str(org_id), MODEL),
        )
    seed_gates(connection, user_id=user_id, org_id=org_id, gates=STARTER_GATES)
    seed_tools(connection, user_id=user_id, org_id=org_id)
    # Higgsfield tools come from the owner's Higgsfield MCP server, not here.
    head = MARKETING.head.model_copy(
        update={
            "allowed_tools": [t for t in MARKETING.head.allowed_tools if not t.startswith("mcp_")]
        }
    )
    publish(
        connection,
        user_id=user_id,
        org_id=org_id,
        department="marketing",
        charter=MARKETING.model_copy(update={"head": head}),
    )
    apply_charter(connection, user_id=user_id, org_id=org_id, department="marketing")
    enable(connection, user_id=user_id, org_id=org_id, department="marketing")
    with acting_as(connection, user_id=str(user_id)) as conn:
        Brain(conn, HashingEmbedder()).insert_fact(
            org_id=org_id,
            claim=MUMBA,
            source="web:mumba.ai",
            admission=dataclasses.replace(admit(conn, org_id), visibility="public"),
        )
    try:
        yield Lab(org_id, user_id)
    finally:
        with as_service_role(connection) as conn:
            conn.execute("delete from public.orgs where id = %s", (str(org_id),))
            conn.execute("delete from auth.users where id = %s", (str(user_id),))
        connection.close()


def test_one_unattended_morning_leaves_a_checked_draft_for_the_owner(dsn: str, lab: Lab) -> None:
    model = ContentTeam()
    rt = Runtime(
        dsn=dsn,
        transport=model,
        tiers=RUN_TIERS,
        embedder=HashingEmbedder(),
        tracer=NullTracer(),
        systemone=ScriptedJev(respond=claims_from({"Mumba.ai": "supported"}), scores=GOOD_VOICE),
        services=agent_services(),
    )
    with connect(dsn) as connection, as_service_role(connection) as conn:
        (root,) = [
            r["id"]
            for r in conn.execute(
                "select public.dispatch_due_triggers(%s::timestamptz) as id", (MORNING,)
            ).fetchall()
        ]
    rounds = drive(dsn, rt)

    assert rounds == 7, "lead, researcher, lead, writer, lead, editor, lead"
    assert status(dsn, root) == "done"
    assert "editor:check_draft" in model.calls
    with connect(dsn) as connection, as_service_role(connection) as conn:
        (draft,) = conn.execute(
            "select status, approval_id, channel from public.drafts where org_id = %s",
            (str(lab.org_id),),
        ).fetchall()
        result = conn.execute(
            "select result from public.tasks where id = %s", (str(root),)
        ).fetchone()["result"]
        held = conn.execute(
            "select count(*) as n from public.tool_calls where org_id = %s and status = 'held'",
            (str(lab.org_id),),
        ).fetchone()["n"]
    assert draft["status"] == "ready" and draft["approval_id"] is not None
    assert result["summary"].startswith("Three ideas")
    assert held == 0, "at L1 the team's own work runs without waiting"

    with connect(dsn) as connection:
        report = department_report(
            connection, user_id=lab.user_id, org_id=lab.org_id, department="marketing", days=1
        )
    assert report["days"][0]["routine"][0]["status"] == "done"
    assert report["unapproved_outside_actions"] == 0


def drive(dsn: str, rt: Runtime) -> int:
    rounds = 0
    while runs := tick(dsn):
        for run_id in runs:
            result = advance_run(rt, run_id, deadline_seconds=60)
            assert result.status == "succeeded", (result.stop_reason, result.error)
        rounds += 1
        assert rounds < 10
    return rounds
