"""Step 8.1: the Research and Intelligence department runs its morning routine.

One morning, end to end and committed, as the scheduler would drive it: the
routine trigger fires, the Research Lead hands the owner's source to the web
researcher and hygiene to the fact curator, the web researcher previews and
pushes the page through the write gate, the curator scans the brain, and the
Lead reports. Then the department report counts the morning.

The model and Jev are scripted; everything else is real.
"""

import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest

from app.agents.runs import Runtime, advance_run
from app.brain.embeddings import HashingEmbedder
from app.db import as_service_role, connect
from app.departments.apply import apply_charter, enable
from app.departments.charter import publish, seed_charters
from app.departments.report import department_report
from app.departments.starter_charters import RESEARCH, STARTER_CHARTERS
from app.gateway import ModelResponse, ToolCall
from app.judge.starter_gates import STARTER_GATES
from app.judge.store import seed_gates
from app.knowledge.wiring import agent_services
from app.tasks import order
from app.tools import seed_tools
from app.tracing import NullTracer
from tests.scripted_jev import ScriptedJev
from tests.test_judge import MODEL
from tests.test_links import CLAIMS, page
from tests.test_runners import TIERS, status, tick

SOURCE = "https://acme.example/about"
# A Monday, 06:35 in New York.
MORNING = "2026-09-28 10:35:00+00"


@dataclass
class ResearchTeam:
    """Plays the Lead, the web researcher, the curator and the page reader."""

    calls: list[str] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **kw: Any) -> ModelResponse:
        system = " ".join(m["content"] or "" for m in messages if m["role"] == "system")
        user = next(m["content"] for m in messages if m["role"] == "user")
        done = [m for m in messages if m["role"] == "tool"]

        if "Extract standalone factual claims" in system:
            return self._say("reader", json.dumps({"claims": CLAIMS}))
        if "You lead the Research and Intelligence department" in system:
            if "Results from your sub-tasks" in user:
                if not done:
                    return self._tool(
                        "lead", "report_result", {"summary": "Brief: 2 facts added; 0 to look at."}
                    )
                return self._say("lead", "Reported.")
            sources = re.findall(r"https://[^\s\"']+", user)
            steps = [
                (
                    "create_task",
                    {
                        "assign_to": "web-researcher",
                        "title": "Read today's sources",
                        "instructions": f"Read {', '.join(sources)} for the topic Acme Corp.",
                    },
                ),
                (
                    "create_task",
                    {
                        "assign_to": "fact-curator",
                        "title": "Brain hygiene",
                        "instructions": "Run the hygiene scan and report.",
                    },
                ),
            ]
            if len(done) < len(steps):
                return self._tool("lead", *steps[len(done)])
            return self._say("lead", "Handed out.")
        if "You read the web pages you are given" in system:
            if not done:
                url = re.findall(r"https://[^\s,\"']+", user)[0]
                return self._tool("web", "web_fetch_preview", {"url": url})
            if len(done) == 1:
                preview = json.loads(done[0]["content"])
                return self._tool("web", "web_push_preview", {"preview_id": preview["preview_id"]})
            if len(done) == 2:
                pushed = json.loads(done[1]["content"])
                outcomes = [r["outcome"] for r in pushed["results"]]
                return self._tool(
                    "web", "report_result", {"summary": f"1 page read; outcomes {outcomes}."}
                )
            return self._say("web", "Done.")
        if "You keep the company brain accurate" in system:
            if not done:
                return self._tool("curator", "brain_hygiene_scan", {})
            if len(done) == 1:
                return self._tool(
                    "curator", "report_result", {"summary": "Nothing stale or disputed."}
                )
            return self._say("curator", "Done.")
        raise AssertionError(f"Unexpected call: {system[:80]}")

    def _tool(self, who: str, name: str, args: dict[str, Any]) -> ModelResponse:
        self.calls.append(f"{who}:{name}")
        return ModelResponse(
            model="vendor/small",
            text="",
            tokens_in=200,
            tokens_out=30,
            cost_usd=0.0002,
            latency_ms=1,
            provider="scripted",
            tool_calls=(ToolCall(f"c{len(self.calls)}", name, json.dumps(args)),),
            finish_reason="tool_calls",
        )

    def _say(self, who: str, text: str) -> ModelResponse:
        self.calls.append(f"{who}:say")
        return ModelResponse(
            model="vendor/small",
            text=text,
            tokens_in=200,
            tokens_out=30,
            cost_usd=0.0002,
            latency_ms=1,
            provider="scripted",
            finish_reason="stop",
        )


@dataclass(frozen=True)
class Lab:
    org_id: uuid.UUID
    user_id: uuid.UUID


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
    seed_charters(connection, user_id=user_id, org_id=org_id, charters=STARTER_CHARTERS)
    routine = RESEARCH.routine[0].model_copy(
        update={"input": {"topics": ["Acme Corp"], "sources": [SOURCE]}}
    )
    publish(
        connection,
        user_id=user_id,
        org_id=org_id,
        department="research",
        charter=RESEARCH.model_copy(update={"routine": [routine]}),
        note="Topics and sources",
    )
    apply_charter(connection, user_id=user_id, org_id=org_id, department="research")
    enable(connection, user_id=user_id, org_id=org_id, department="research")
    try:
        yield Lab(org_id, user_id)
    finally:
        with as_service_role(connection) as conn:
            conn.execute("delete from public.orgs where id = %s", (str(org_id),))
            conn.execute("delete from auth.users where id = %s", (str(user_id),))
        connection.close()


def runtime(dsn: str, model: ResearchTeam) -> Runtime:
    services = agent_services()
    services["fetcher"] = lambda url: page()
    return Runtime(
        dsn=dsn,
        transport=model,
        tiers=TIERS,
        embedder=HashingEmbedder(),
        tracer=NullTracer(),
        systemone=ScriptedJev(),
        services=services,
    )


def fire(dsn: str) -> list[uuid.UUID]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return [
            r["id"]
            for r in conn.execute(
                "select public.dispatch_due_triggers(%s::timestamptz) as id", (MORNING,)
            ).fetchall()
        ]


def drive(dsn: str, rt: Runtime) -> int:
    """Run every run the scheduler would start, until nothing is left."""
    rounds = 0
    while runs := tick(dsn):
        for run_id in runs:
            result = advance_run(rt, run_id, deadline_seconds=60)
            assert result.status == "succeeded", (result.stop_reason, result.error)
        rounds += 1
        assert rounds < 6
    return rounds


def test_one_unattended_morning_adds_checked_facts_and_reports(dsn: str, lab: Lab) -> None:
    model = ResearchTeam()
    rt = runtime(dsn, model)

    (brief,) = fire(dsn)
    assert fire(dsn) == [], "one routine task per morning"
    rounds = drive(dsn, rt)

    assert rounds == 3, "lead plans, the two workers work, the lead reports"
    assert status(dsn, brief) == "done"
    with connect(dsn) as connection, as_service_role(connection) as conn:
        facts = conn.execute(
            "select claim, source, source_ref from public.facts where org_id = %s order by claim",
            (str(lab.org_id),),
        ).fetchall()
        result = conn.execute(
            "select result from public.tasks where id = %s", (str(brief),)
        ).fetchone()["result"]
        unscreened = conn.execute(
            "select count(*) as n from public.judgments where org_id = %s and gate = 'tool_risk'",
            (str(lab.org_id),),
        ).fetchone()["n"]
    assert [f["claim"] for f in facts] == sorted(CLAIMS)
    assert {f["source"] for f in facts} == {"web:acme.example"}
    assert result["summary"].startswith("Brief:")
    assert unscreened > 0, "the web read (R2) went through the tool-risk gate at L1"

    with connect(dsn) as connection:
        report = department_report(
            connection, user_id=lab.user_id, org_id=lab.org_id, department="research", days=1
        )
    (today,) = report["days"]
    assert today["ok"] and today["routine"][0]["status"] == "done"
    assert report["facts"] == {"accepted": 2}
    assert report["unapproved_outside_actions"] == 0
    assert report["mornings_in_a_row"] == 1 and not report["done"], "needs five, and an order"


def test_the_owner_steps_in_with_an_order_and_it_is_obeyed(dsn: str, lab: Lab) -> None:
    model = ResearchTeam()
    rt = runtime(dsn, model)
    with connect(dsn) as connection:
        task = order(
            connection,
            user_id=lab.user_id,
            org_id=lab.org_id,
            agent="research-lead",
            title="Extra brief on Acme",
            instructions=f"Read {SOURCE} again today.",
            input={"sources": [SOURCE], "topics": ["Acme Corp"]},
        )
    drive(dsn, rt)

    assert status(dsn, task.id) == "done"
    with connect(dsn) as connection:
        report = department_report(
            connection, user_id=lab.user_id, org_id=lab.org_id, department="research", days=1
        )
    assert report["owner_orders"] == {"given": 1, "done": 1}
