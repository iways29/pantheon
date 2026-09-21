"""The run lifecycle, end to end against real Postgres and a real checkpointer.

Step 3 acceptance:
- a run can be killed mid-way and resumed from its checkpoint;
- the run's total cost and token use are visible;
- hitting the step or token cap stops the run cleanly.

Unlike the rest of the suite these tests commit: advance_run opens its own
connections (as a serverless invocation would), and the checkpointer writes on
a connection of its own, so a rolled-back fixture transaction would hide
everything. Each test gets a throwaway org, deleted afterwards; the cascade
from orgs removes its runs, facts, events, ledger rows and checkpoints.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agents.research import EXTRACT_SYSTEM, RunScope, Session, build_graph
from app.agents.runs import RunBusy, Runtime, advance_run, report, start_run
from app.brain import Brain
from app.brain.embeddings import HashingEmbedder
from app.db import acting_as, as_service_role, connect
from app.gateway import Gateway, ModelResponse, TierMap
from app.tracing import LangfuseTracer, NullTracer

TIERS = TierMap(
    models={"cheap": "vendor/small", "standard": "vendor/mid", "frontier": "vendor/big"}
)
QUESTION = "What is the boiling point of water at sea level?"
ANSWER = "Water boils at 100 degrees Celsius at sea level."
CLAIMS = ["Water boils at 100 degrees Celsius at sea level.", "Sea-level pressure is 1 atm."]


class Crash(BaseException):
    """Stands in for the process dying: nothing may catch it and tidy up."""


@dataclass
class ScriptedModel:
    """Answers the research agent's two prompts, and records what it was asked."""

    cost_usd: float = 0.001
    crash_on: str | None = None
    on_call: Any = None
    calls: list[str] = field(default_factory=list)

    def complete(self, *, model: str, messages: list[dict[str, Any]], **_: object) -> ModelResponse:
        kind = "extract" if messages[0]["content"] == EXTRACT_SYSTEM else "answer"
        self.calls.append(kind)
        if self.crash_on == kind:
            self.crash_on = None
            raise Crash
        if self.on_call is not None:
            self.on_call(kind)
        text = json.dumps({"claims": CLAIMS}) if kind == "extract" else ANSWER
        return ModelResponse(
            model=model,
            provider="scripted",
            text=text,
            tokens_in=100,
            tokens_out=50,
            cost_usd=self.cost_usd,
            latency_ms=1,
        )


@dataclass(frozen=True)
class LiveOrg:
    org_id: uuid.UUID
    user_id: uuid.UUID
    department_id: uuid.UUID
    agent_id: uuid.UUID


@pytest.fixture
def live(dsn: str) -> Iterator[LiveOrg]:
    try:
        connection = connect(dsn)
    except psycopg.OperationalError as error:
        pytest.skip(f"No database at {dsn}: {error}")
    ids = LiveOrg(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            "insert into auth.users (id, email) values (%s, %s)",
            (str(ids.user_id), f"{ids.user_id}@example.com"),
        )
        cursor.execute("insert into public.orgs (id, name) values (%s, 'live')", (str(ids.org_id),))
        cursor.execute(
            "insert into public.org_members (org_id, user_id) values (%s, %s)",
            (str(ids.org_id), str(ids.user_id)),
        )
        cursor.execute(
            "insert into public.departments (id, org_id, name, daily_budget_usd) "
            "values (%s, %s, 'research', 1)",
            (str(ids.department_id), str(ids.org_id)),
        )
        cursor.execute(
            "insert into public.agents (id, org_id, department_id, name, role, model_tier) "
            "values (%s, %s, %s, 'researcher', 'research', 'cheap')",
            (str(ids.agent_id), str(ids.org_id), str(ids.department_id)),
        )
    try:
        yield ids
    finally:
        with as_service_role(connection) as conn, conn.cursor() as cursor:
            cursor.execute("delete from public.orgs where id = %s", (str(ids.org_id),))
            cursor.execute("delete from auth.users where id = %s", (str(ids.user_id),))
        connection.close()


def runtime(dsn: str, model: ScriptedModel) -> Runtime:
    return Runtime(
        dsn=dsn, transport=model, tiers=TIERS, embedder=HashingEmbedder(), tracer=NullTracer()
    )


def new_run(dsn: str, live: LiveOrg, **kwargs: Any) -> uuid.UUID:
    with connect(dsn) as connection:
        return start_run(
            connection,
            org_id=live.org_id,
            agent_id=live.agent_id,
            requested_by=live.user_id,
            question=QUESTION,
            idempotency_key=kwargs.pop("idempotency_key", str(uuid.uuid4())),
            **kwargs,
        )


def sql(dsn: str, query: str, *params: Any) -> list[dict[str, Any]]:
    with connect(dsn) as connection, as_service_role(connection) as conn:
        return conn.execute(query, params).fetchall()


def set_kill_switch(dsn: str, org_id: uuid.UUID, *, on: bool) -> None:
    sql(
        dsn,
        "insert into public.system_flags (org_id, key, value) values (%s, 'kill_switch', %s) "
        "on conflict (org_id, key) do update set value = excluded.value returning key",
        str(org_id),
        json.dumps(on),
    )


# --- The happy path, and what it leaves behind --------------------------------


def test_a_run_answers_stores_facts_and_rolls_up_cost(dsn: str, live: LiveOrg) -> None:
    model = ScriptedModel(cost_usd=0.001)
    run_id = new_run(dsn, live)

    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("succeeded", "completed")
    assert result.steps_taken == 4
    assert model.calls == ["answer", "extract"]
    # Acceptance: total cost and tokens are visible on the run.
    assert (result.tokens_in, result.tokens_out) == (200, 100)
    assert result.cost_usd == Decimal("0.002")
    assert result.output["answer"] == ANSWER
    stored = sql(
        dsn,
        "select claim, source, created_by_run_id from public.facts where org_id = %s",
        str(live.org_id),
    )
    assert sorted(f["claim"] for f in stored) == sorted(CLAIMS)
    assert all(f["created_by_run_id"] == run_id and f["source"] == "agent:research" for f in stored)


def test_every_lifecycle_transition_is_an_event(dsn: str, live: LiveOrg) -> None:
    run_id = new_run(dsn, live)
    advance_run(runtime(dsn, ScriptedModel()), run_id, deadline_seconds=60)

    events = [
        (e["type"], e["payload"].get("node"))
        for e in sql(
            dsn,
            "select type, payload from public.events where run_id = %s order by created_at, id",
            str(run_id),
        )
        if e["type"].startswith("run_")
    ]
    assert events[:2] == [("run_created", None), ("run_invoked", None)]
    assert [node for kind, node in events if kind == "run_step"] == [
        "recall",
        "answer",
        "extract",
        "store",
    ]
    assert events[-1] == ("run_succeeded", None)


def test_the_same_idempotency_key_starts_one_run(dsn: str, live: LiveOrg) -> None:
    first = new_run(dsn, live, idempotency_key="trigger-1")
    second = new_run(dsn, live, idempotency_key="trigger-1")

    assert first == second
    assert len(sql(dsn, "select id from public.runs where org_id = %s", str(live.org_id))) == 1


# --- Acceptance: killed mid-way, resumed from the checkpoint ------------------


def test_a_run_killed_mid_way_resumes_from_its_checkpoint(dsn: str, live: LiveOrg) -> None:
    model = ScriptedModel(crash_on="extract")
    run_id = new_run(dsn, live)

    with pytest.raises(Crash):
        advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    # The process "died" during step 3: nothing tidied up, the lease is held.
    assert report(connect(dsn), run_id).status == "running"
    with pytest.raises(RunBusy):
        advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    # Once the dead invocation's lease lapses, the next one resumes.
    sql(
        dsn,
        "update public.runs set lease_expires_at = now() - interval '1 second' "
        "where id = %s returning id",
        str(run_id),
    )
    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("succeeded", "completed")
    # recall and answer came from the checkpoint; only the step in flight re-ran.
    assert model.calls == ["answer", "extract", "extract"]
    assert result.steps_taken == 4
    assert result.cost_usd == Decimal("0.002"), "the crashed call never completed, so costs nothing"


def test_a_repeated_store_step_does_not_duplicate_facts(dsn: str, live: LiveOrg) -> None:
    """A step that re-runs after its writes committed must be a no-op.

    Re-executes the whole graph for a run that already stored its facts, as a
    resume would after a crash between the store step's commit and its
    checkpoint.
    """
    run_id = new_run(dsn, live)
    advance_run(runtime(dsn, ScriptedModel()), run_id, deadline_seconds=60)

    with connect(dsn) as connection:

        @contextmanager
        def session() -> Iterator[Session]:
            with acting_as(connection, user_id=str(live.user_id)) as conn:
                yield Session(Gateway(conn, ScriptedModel(), TIERS), Brain(conn, HashingEmbedder()))

        scope = RunScope(run_id, live.org_id, live.agent_id, session)
        build_graph(scope).invoke({"question": QUESTION})

    assert (
        len(sql(dsn, "select id from public.facts where created_by_run_id = %s", str(run_id))) == 2
    )


# --- Acceptance: caps stop the run cleanly ------------------------------------


def test_the_step_cap_stops_the_run_on_a_step_boundary(dsn: str, live: LiveOrg) -> None:
    model = ScriptedModel()
    run_id = new_run(dsn, live, max_steps=2)

    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("failed", "max_steps")
    assert result.steps_taken == 2
    assert model.calls == ["answer"], "no step after the cap may start"
    assert result.cost_usd == Decimal("0.001")


def test_the_token_cap_stops_the_run_on_a_step_boundary(dsn: str, live: LiveOrg) -> None:
    model = ScriptedModel()
    run_id = new_run(dsn, live, max_tokens=150)

    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("failed", "max_tokens")
    assert model.calls == ["answer"]
    assert result.tokens_in + result.tokens_out == 150


def test_a_capped_run_cannot_be_resumed(dsn: str, live: LiveOrg) -> None:
    run_id = new_run(dsn, live, max_steps=1)
    advance_run(runtime(dsn, ScriptedModel()), run_id, deadline_seconds=60)

    with pytest.raises(RunBusy, match="already failed"):
        advance_run(runtime(dsn, ScriptedModel()), run_id, deadline_seconds=60)


# --- Pauses: kill switch, budget, deadline ------------------------------------


def test_the_kill_switch_stops_a_run_from_starting(dsn: str, live: LiveOrg) -> None:
    model = ScriptedModel()
    run_id = new_run(dsn, live)
    set_kill_switch(dsn, live.org_id, on=True)

    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason, result.steps_taken) == ("paused", "kill_switch", 0)
    assert model.calls == []

    set_kill_switch(dsn, live.org_id, on=False)
    assert advance_run(runtime(dsn, model), run_id, deadline_seconds=60).status == "succeeded"


def test_the_kill_switch_stops_a_run_between_steps(dsn: str, live: LiveOrg) -> None:
    model = ScriptedModel(on_call=lambda kind: set_kill_switch(dsn, live.org_id, on=True))
    run_id = new_run(dsn, live)

    paused = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)
    assert (paused.status, paused.stop_reason, paused.steps_taken) == ("paused", "kill_switch", 2)

    set_kill_switch(dsn, live.org_id, on=False)
    model.on_call = None
    resumed = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)
    assert resumed.status == "succeeded"
    assert model.calls == ["answer", "extract"], "the answer step was not repeated"


def test_an_exhausted_budget_pauses_the_run(dsn: str, live: LiveOrg) -> None:
    sql(
        dsn,
        "update public.departments set daily_budget_usd = 0.0015 where id = %s returning id",
        str(live.department_id),
    )
    model = ScriptedModel(cost_usd=0.002)
    run_id = new_run(dsn, live)

    result = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)

    assert (result.status, result.stop_reason) == ("paused", "budget_exceeded")
    assert model.calls == ["answer"], "the gateway refused the second call before it was sent"
    assert "daily budget" in result.error


def test_an_invocation_that_runs_out_of_time_pauses_and_the_next_continues(
    dsn: str, live: LiveOrg
) -> None:
    model = ScriptedModel()
    run_id = new_run(dsn, live)

    first = advance_run(runtime(dsn, model), run_id, deadline_seconds=0)
    assert (first.status, first.stop_reason, first.steps_taken) == ("paused", "deadline", 1)

    second = advance_run(runtime(dsn, model), run_id, deadline_seconds=60)
    assert (second.status, second.steps_taken) == ("succeeded", 4)


# --- The checkpoint tables ------------------------------------------------------


def test_checkpoints_carry_the_runs_org(dsn: str, live: LiveOrg) -> None:
    run_id = new_run(dsn, live)
    advance_run(runtime(dsn, ScriptedModel()), run_id, deadline_seconds=60)

    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        orgs = sql(
            dsn, f"select distinct org_id from langgraph.{table} where thread_id = %s", str(run_id)
        )
        assert [row["org_id"] for row in orgs] == [live.org_id], table


def test_a_checkpoint_for_a_thread_that_is_not_a_run_is_refused(dsn: str, live: LiveOrg) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="is not a run"):
        sql(
            dsn,
            "insert into langgraph.checkpoints (thread_id, checkpoint_id, checkpoint) "
            "values (%s, 'c1', '{}') returning thread_id",
            str(uuid.uuid4()),
        )


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_api_roles_cannot_reach_checkpoint_state(dsn: str, role: str) -> None:
    with connect(dsn) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(f"set local role {role}")
                connection.execute("select 1 from langgraph.checkpoints limit 1")


# --- Tracing ------------------------------------------------------------------


def test_a_traced_run_nests_each_call_under_the_step_that_made_it(dsn: str, live: LiveOrg) -> None:
    exporter = InMemorySpanExporter()
    langfuse = Langfuse(
        public_key="pk-lf-runs",
        secret_key="sk-lf-runs",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
        tracer_provider=TracerProvider(),
    )
    traced = Runtime(
        dsn=dsn,
        transport=ScriptedModel(),
        tiers=TIERS,
        embedder=HashingEmbedder(),
        tracer=LangfuseTracer(langfuse, public_key="pk-lf-runs"),
    )
    run_id = new_run(dsn, live)
    result = advance_run(traced, run_id, deadline_seconds=60)
    assert (result.status, result.stop_reason, result.error) == ("succeeded", "completed", None)

    spans = exporter.get_finished_spans()
    names = {span.context.span_id: span.name for span in spans}
    tree = {(span.name, names.get(span.parent.span_id) if span.parent else None) for span in spans}
    assert tree == {
        ("advance-run", None),
        ("research-agent", "advance-run"),
        ("recall", "research-agent"),
        ("answer", "research-agent"),
        ("extract", "research-agent"),
        ("store", "research-agent"),
        ("call-model", "answer"),
        ("call-model", "extract"),
    }
    seeded = int(langfuse.create_trace_id(seed=str(run_id)), 16)
    assert {span.context.trace_id for span in spans} == {seeded}
    root = next(span for span in spans if span.name == "advance-run")
    assert root.attributes["langfuse.trace.name"] == "agent-run"
    assert root.attributes["user.id"] == str(live.user_id)
