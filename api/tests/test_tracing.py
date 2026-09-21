"""What the gateway sends to Langfuse, checked on the exported spans themselves.

The real Langfuse SDK runs here; only its exporter is swapped for an in-memory
one, so these assertions are about the attributes that would leave the
process, not about what the code meant to send.
"""

import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import Settings
from app.db import acting_as
from app.gateway import Gateway, KillSwitchEngaged, ModelResponse, UpstreamError
from app.tracing import WITHHELD, LangfuseTracer, NullTracer, langfuse_from, tracer_from
from tests.conftest_db import Tenants
from tests.test_gateway import TIERS, RecordingTransport, make_agent, set_kill_switch

SECRET_PROMPT = "the merger closes on the 14th"


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture(scope="module")
def langfuse(exporter: InMemorySpanExporter) -> Langfuse:
    # Langfuse keeps one client per public key, so one per module; a private
    # TracerProvider keeps it off the global OpenTelemetry provider.
    return Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        environment="test",
        span_exporter=exporter,
        tracer_provider=TracerProvider(),
    )


@pytest.fixture
def spans(langfuse: Langfuse, exporter: InMemorySpanExporter) -> Iterator[Any]:
    exporter.clear()

    def finished() -> list[ReadableSpan]:
        langfuse.flush()
        return list(exporter.get_finished_spans())

    yield finished


def call(
    db: psycopg.Connection,
    tenants: Tenants,
    langfuse: Langfuse,
    transport: Any,
    agent_id: Any,
    **kwargs: Any,
) -> None:
    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS, LangfuseTracer(langfuse)).complete(
            agent_id=agent_id,
            messages=[{"role": "user", "content": kwargs.pop("content", "hi")}],
            **kwargs,
        )


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_a_call_is_one_generation_with_model_usage_and_cost(
    db: psycopg.Connection, tenants: Tenants, langfuse: Langfuse, spans: Any
) -> None:
    agent_id = make_agent(db, tenants.org_a, name="tracer", tier="cheap")
    call(db, tenants, langfuse, RecordingTransport(cost_usd=0.0031), agent_id, max_tokens=64)

    [span] = spans()
    a = attrs(span)
    assert span.name == "call-model"
    assert a["langfuse.observation.type"] == "generation"
    assert a["langfuse.observation.model.name"] == "vendor/small-model"
    assert json.loads(a["langfuse.observation.usage_details"]) == {"input": 11, "output": 7}
    assert json.loads(a["langfuse.observation.cost_details"]) == {"total": 0.0031}
    assert json.loads(a["langfuse.observation.input"]) == [{"role": "user", "content": "hi"}]
    assert a["langfuse.observation.output"] == "ok"
    assert a["langfuse.observation.metadata.agent_id"] == str(agent_id)
    assert a["langfuse.observation.metadata.requested_model"] == "vendor/small-model"
    assert set(a["langfuse.trace.tags"]) == {"department:dept-for-tracer", "tier:cheap"}
    assert a["langfuse.environment"] == "test"


def test_a_sensitive_call_never_sends_its_text(
    db: psycopg.Connection, tenants: Tenants, langfuse: Langfuse, spans: Any
) -> None:
    agent_id = make_agent(db, tenants.org_a)
    call(
        db,
        tenants,
        langfuse,
        RecordingTransport(),
        agent_id,
        content=SECRET_PROMPT,
        sensitive=True,
    )

    [span] = spans()
    a = attrs(span)
    assert a["langfuse.observation.input"] == WITHHELD
    assert a["langfuse.observation.output"] == WITHHELD
    assert SECRET_PROMPT not in json.dumps(a, default=str)
    # Usage and cost still arrive; only the text is withheld.
    assert json.loads(a["langfuse.observation.usage_details"]) == {"input": 11, "output": 7}


def test_a_blocked_call_is_a_guardrail_and_no_generation(
    db: psycopg.Connection, tenants: Tenants, langfuse: Langfuse, spans: Any
) -> None:
    agent_id = make_agent(db, tenants.org_a)
    set_kill_switch(db, tenants.org_a, on=True)

    with pytest.raises(KillSwitchEngaged):
        call(db, tenants, langfuse, RecordingTransport(), agent_id)

    [span] = spans()
    assert span.name == "enforce-call-gates"
    assert attrs(span)["langfuse.observation.type"] == "guardrail"
    assert attrs(span)["langfuse.observation.status_message"] == "kill_switch_engaged"


class FailingTransport:
    def complete(self, **_: object) -> ModelResponse:
        raise UpstreamError("OpenRouter returned 502: bad gateway", status=502)


def test_a_provider_failure_marks_the_generation_as_an_error(
    db: psycopg.Connection, tenants: Tenants, langfuse: Langfuse, spans: Any
) -> None:
    agent_id = make_agent(db, tenants.org_a)

    with pytest.raises(UpstreamError):
        call(db, tenants, langfuse, FailingTransport(), agent_id)

    [span] = spans()
    assert attrs(span)["langfuse.observation.level"] == "ERROR"
    assert "upstream_error" in attrs(span)["langfuse.observation.status_message"]


def test_calls_in_one_run_share_a_trace(langfuse: Langfuse, spans: Any) -> None:
    """Seeded from run_id, so a run resumed in a later invocation joins it too."""
    for run in ("run-1", "run-1", "run-2"):
        call_in_run(langfuse, run)

    by_run: dict[str, set[int]] = {}
    for span in spans():
        by_run.setdefault(attrs(span)["langfuse.observation.metadata.run_id"], set()).add(
            span.context.trace_id
        )
    assert {run: len(ids) for run, ids in by_run.items()} == {"run-1": 1, "run-2": 1}
    assert by_run["run-1"] != by_run["run-2"]
    assert by_run["run-1"] == {int(langfuse.create_trace_id(seed="run-1"), 16)}


def test_trace_names_are_stable_whatever_comes_first(langfuse: Langfuse, spans: Any) -> None:
    """A run's trace is `agent-run` even when its first observation is a refusal."""
    tracer = LangfuseTracer(langfuse)
    tracer.blocked(context={"run_id": "run-9"}, tags=[], run_id="run-9", detail={"code": "x"})
    call_in_run(langfuse, "run-9")
    with tracer.model_call(
        context={}, tags=[], run_id=None, model="m", messages=[], max_tokens=None, sensitive=False
    ):
        pass

    observed = {
        (
            span.name,
            attrs(span).get("langfuse.observation.metadata.run_id"),
            attrs(span).get("langfuse.trace.name"),
        )
        for span in spans()
    }
    assert observed == {
        ("enforce-call-gates", "run-9", "agent-run"),
        ("call-model", "run-9", "agent-run"),
        ("call-model", None, "call-model"),
    }


def call_in_run(langfuse: Langfuse, run: str) -> None:
    # The gateway's run_id is a runs.id foreign key, so grouping is checked at
    # the tracer boundary rather than by seeding runs rows.
    with LangfuseTracer(langfuse).model_call(
        context={"run_id": run},
        tags=[],
        run_id=run,
        model="vendor/small-model",
        messages=[],
        max_tokens=None,
        sensitive=False,
    ) as recorder:
        recorder.succeeded(
            text="ok",
            served_model="vendor/small-model",
            provider=None,
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            latency_ms=1,
        )


def test_tracing_is_off_without_keys() -> None:
    settings = Settings(_env_file=None, langfuse_public_key=None, langfuse_secret_key=None)
    assert langfuse_from(settings) is None
    assert isinstance(tracer_from(None), NullTracer)


def test_a_call_inside_an_open_observation_nests_under_it(langfuse: Langfuse, spans: Any) -> None:
    """Step 3's run observation will be the parent; the run_id seed then steps aside."""
    with langfuse.start_as_current_observation(name="run-agent", as_type="agent") as root:
        call_in_run(langfuse, "run-3")

    by_name = {span.name: span for span in spans()}
    child = by_name["call-model"]
    assert child.context.trace_id == by_name["run-agent"].context.trace_id
    assert child.parent is not None and child.parent.span_id == by_name["run-agent"].context.span_id
    assert root.trace_id != langfuse.create_trace_id(seed="run-3")
