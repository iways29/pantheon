"""Langfuse tracing, kept to one module so no caller imports the SDK directly.

ADR 004. Postgres stays the source of truth for cost and audit (`model_calls`,
`events`); traces are the debugging view, and they expire after 30 days on the
Hobby tier.

Tracing is off unless both Langfuse keys are set, so tests and local work need
no account. When it is off, `NullTracer` stands in and does nothing.

Trace shape, per Langfuse's best-practices guide:

- one trace per agent run. Every model call in a run carries the same trace
  id, seeded from `run_id`, so a run resumed in a later serverless invocation
  still lands in the same trace. Once Step 3 opens a root observation for the
  run, calls nest under it instead and the seed is not used.
- each model call is a `generation` named `call-model`, with the model
  OpenRouter actually served, token usage, and OpenRouter's reported cost
  ingested directly (Langfuse cannot price OpenRouter slugs itself).
- a call the gateway refuses is a `guardrail` named `enforce-call-gates`, so
  a budget or kill-switch block is visible beside the calls that did run.
- department and tier are tags, for cost breakdowns in dashboards.
- names are stable and never include a model, so swapping models (ADR 003)
  does not break filters or evaluators.

Sensitive calls: the prompt and completion text are never handed to the SDK.
The observation records that text was withheld, plus usage and cost. Masking
at export would also work, but not sending the data at all cannot fail open.
"""

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from langfuse import Langfuse, LangfuseGeneration, propagate_attributes
from opentelemetry import trace

from app.config import Settings

WITHHELD = "[withheld: sensitive call]"


def langfuse_from(settings: Settings) -> Langfuse | None:
    """The configured Langfuse client, or None when tracing is not set up."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        # Keeps preview and local traces out of production dashboards.
        environment=settings.environment,
        # Vercel sets this per deployment, so each trace names its build.
        release=os.getenv("VERCEL_GIT_COMMIT_SHA"),
    )


class CallRecorder(Protocol):
    """Filled in by the gateway once the provider has answered."""

    def succeeded(
        self,
        *,
        text: str,
        served_model: str,
        provider: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int,
    ) -> None: ...

    def failed(self, *, code: str, message: str) -> None: ...


class Tracer(Protocol):
    def model_call(
        self,
        *,
        context: dict[str, str],
        tags: list[str],
        run_id: str | None,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        sensitive: bool,
    ) -> AbstractContextManager[CallRecorder]: ...

    def blocked(
        self,
        *,
        context: dict[str, str],
        tags: list[str],
        run_id: str | None,
        detail: dict[str, Any],
    ) -> None: ...


class _NullRecorder:
    def succeeded(self, **_: object) -> None:
        pass

    def failed(self, **_: object) -> None:
        pass


class NullTracer:
    """Tracing switched off. Same shape, no effect."""

    @contextmanager
    def model_call(self, **_: object) -> Iterator[CallRecorder]:
        yield _NullRecorder()

    def blocked(self, **_: object) -> None:
        pass


class _GenerationRecorder:
    def __init__(self, generation: LangfuseGeneration, *, sensitive: bool) -> None:
        self._generation = generation
        self._sensitive = sensitive

    def succeeded(
        self,
        *,
        text: str,
        served_model: str,
        provider: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int,
    ) -> None:
        self._generation.update(
            output=WITHHELD if self._sensitive else text,
            # What OpenRouter reports serving, which can differ from what was
            # asked for; the requested slug is in metadata.
            model=served_model,
            # OpenRouter's prompt_tokens and completion_tokens are disjoint, so
            # they map onto Langfuse's input and output buckets without the
            # double counting its docs warn about.
            usage_details={"input": tokens_in, "output": tokens_out},
            # OpenRouter's own charge for this request, ingested rather than
            # inferred, so the trace agrees with model_calls.
            cost_details={"total": cost_usd},
            # Latency is not repeated here: Langfuse measures it from the
            # observation's own start and end.
            metadata={"provider": provider or ""},
        )

    def failed(self, *, code: str, message: str) -> None:
        self._generation.update(level="ERROR", status_message=f"{code}: {message}"[:500])


class LangfuseTracer:
    def __init__(self, langfuse: Langfuse) -> None:
        self._langfuse = langfuse

    @contextmanager
    def model_call(
        self,
        *,
        context: dict[str, str],
        tags: list[str],
        run_id: str | None,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None,
        sensitive: bool,
    ) -> Iterator[CallRecorder]:
        trace_context, trace_name = self._trace_placement(run_id, default="call-model")
        with (
            propagate_attributes(tags=tags, trace_name=trace_name),
            self._langfuse.start_as_current_observation(
                trace_context=trace_context,
                name="call-model",
                as_type="generation",
                model=model,
                # Already in OpenAI message format, which Langfuse renders as a
                # role-labelled conversation rather than raw JSON.
                input=WITHHELD if sensitive else messages,
                model_parameters={"max_tokens": max_tokens} if max_tokens else None,
                metadata={**context, "requested_model": model, "sensitive": str(sensitive)},
            ) as generation,
        ):
            yield _GenerationRecorder(generation, sensitive=sensitive)

    def blocked(
        self,
        *,
        context: dict[str, str],
        tags: list[str],
        run_id: str | None,
        detail: dict[str, Any],
    ) -> None:
        trace_context, trace_name = self._trace_placement(run_id, default="call-model")
        with (
            propagate_attributes(tags=tags, trace_name=trace_name),
            self._langfuse.start_as_current_observation(
                trace_context=trace_context,
                name="enforce-call-gates",
                as_type="guardrail",
                input=context,
                output=detail,
                level="WARNING",
                status_message=str(detail.get("code", "blocked")),
                metadata=context,
            ),
        ):
            pass

    def _trace_placement(
        self, run_id: str | None, *, default: str
    ) -> tuple[dict[str, str] | None, str | None]:
        """Where an observation lands: its trace context and the trace's name.

        Inside an open observation (Step 3's run), it simply nests and leaves
        the trace name to the parent. Otherwise a run's calls share a trace
        seeded from run_id, named `agent-run`; a call outside any run is its
        own `call-model` trace. Naming the trace explicitly keeps it stable:
        left alone, it would take the name of whichever observation came
        first, so a run that opened with a refused call would be named after
        the guardrail and drop out of dashboard filters.
        """
        # Asked of OpenTelemetry directly: Langfuse's get_current_trace_id()
        # logs an error whenever no span is open, which here is the normal case.
        if trace.get_current_span().get_span_context().is_valid:
            return None, None
        if run_id is None:
            return None, default
        return {"trace_id": self._langfuse.create_trace_id(seed=run_id)}, "agent-run"


def tracer_from(langfuse: Langfuse | None) -> Tracer:
    return LangfuseTracer(langfuse) if langfuse is not None else NullTracer()
