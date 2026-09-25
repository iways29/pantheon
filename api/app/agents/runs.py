"""Run lifecycle: start a run, advance it in bounded invocations, resume it.

A serverless function has minutes, so a run is never executed in one go. Each
call to `advance_run` claims the run under a lease, resumes its graph from the
last checkpoint, and executes steps until the graph finishes, a cap is hit, or
the invocation's time budget runs out. Whatever stops it is recorded, and the
next call picks up from the checkpoint.

What stops a run, and what that leaves it as:

| Reason | Status | Resumable |
| --- | --- | --- |
| completed | succeeded | no |
| max_steps, max_tokens | failed | no: a cap is a hard stop |
| deadline | paused | yes: the next invocation continues |
| kill_switch, budget_exceeded, agent_disabled, department_disabled | paused | yes, once lifted |
| prompt_missing | paused | yes, once the agent has an active prompt for each slot |
| upstream_error | paused | yes: provider failures are often transient |
| error | failed | no: a bug, recorded with its message |

A process that dies mid-step records nothing: the run stays `running` with a
lease that expires, after which the next invocation resumes from the last
checkpoint. The step in flight re-runs; that is safe because every side
effect in a step is idempotent (facts are keyed by run and claim).

Caps are checked between steps, never mid-step, so a run stops cleanly on a
step boundary. The token cap can therefore be overshot by at most one step's
calls, which the per-call max_tokens ceilings bound.

Lifecycle bookkeeping (lease, status, cost rollup, events) runs as
service_role: it is the backend administering its own runs. The agent's work
inside each step runs as the user the run was requested by, under RLS.
"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from app.agents import deep
from app.agents.checkpointer import checkpointer
from app.agents.prompts import PromptMissing, resolve_for_run
from app.agents.research import PROMPT_SLOTS, RunScope, Session, build_graph
from app.brain import Brain, GatewayEmbedder
from app.brain.embeddings import Embedder
from app.brain.write_gate import BrainWriter
from app.db import acting_as, as_service_role, connect
from app.gateway import (
    Gateway,
    GatewayError,
    SystemOneTransport,
    TierMap,
    Transport,
    UpstreamError,
)
from app.judge import Judge
from app.tracing import Tracer

#: Gateway refusals that pause a run until the condition is lifted.
_PAUSING_REFUSALS = {
    "kill_switch_engaged": "kill_switch",
    "budget_exceeded": "budget_exceeded",
    "agent_disabled": "agent_disabled",
    "department_disabled": "department_disabled",
}
_TERMINAL = ("succeeded", "failed", "cancelled")


class RunNotFound(LookupError):
    pass


class RunBusy(RuntimeError):
    """Another invocation holds the run's lease."""


@dataclass(frozen=True)
class Runtime:
    """Everything an invocation needs, built once from configuration."""

    dsn: str
    transport: Transport
    tiers: TierMap
    #: None: embed through the gateway with the org's assigned model (ADR 013),
    #: costed to the run's agent. Tests pass the free HashingEmbedder.
    embedder: Embedder | None
    tracer: Tracer
    #: TypeSafe, for the brain write gate. None means no fact can be written.
    systemone: SystemOneTransport | None = None
    #: Extra services for agents' tools (ADR 020): `fetcher` (a URL to a
    #: page) and `links` (a session to a Links). Absent: those tools say so.
    services: dict[str, Any] | None = None
    #: How long a claim lasts without renewal. Renewed after every step, so it
    #: only needs to cover one step plus slack.
    lease_seconds: int = 300


@dataclass(frozen=True)
class RunReport:
    run_id: UUID
    status: str
    stop_reason: str | None
    steps_taken: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    output: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class _Run:
    id: UUID
    org_id: UUID
    agent_id: UUID
    agent_name: str
    requested_by: UUID
    status: str
    input: dict[str, Any]
    steps_taken: int
    max_steps: int
    max_tokens: int
    prompt_versions: dict[str, int]
    runner: str = "pipeline"
    task_id: UUID | None = None


@dataclass(frozen=True)
class _Stop:
    status: str
    reason: str
    output: dict[str, Any] | None = None
    error: str | None = None


def start_run(
    connection: psycopg.Connection,
    *,
    org_id: UUID | str,
    agent_id: UUID | str,
    requested_by: UUID | str,
    question: str,
    idempotency_key: str,
    trigger: str = "manual",
    max_steps: int | None = None,
    max_tokens: int | None = None,
) -> UUID:
    """Create a run, or return the one already created with this key.

    The unique (org_id, idempotency_key) constraint is what makes a repeated
    trigger produce one run; this only reads back the winner.
    """
    if not question.strip():
        raise ValueError("A research run needs a question")
    columns = {
        "org_id": str(org_id),
        "agent_id": str(agent_id),
        "requested_by": str(requested_by),
        "trigger": trigger,
        "idempotency_key": idempotency_key,
        "input": json.dumps({"question": question.strip()}),
    }
    # Omitted rather than passed as NULL, so the table's defaults apply.
    if max_steps is not None:
        columns["max_steps"] = str(max_steps)
    if max_tokens is not None:
        columns["max_tokens"] = str(max_tokens)

    with acting_as(connection, user_id=str(requested_by)) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"insert into public.runs ({', '.join(columns)}) "
            f"values ({', '.join(['%s'] * len(columns))}) "
            "on conflict (org_id, idempotency_key) do nothing returning id",
            tuple(columns.values()),
        )
        row = cursor.fetchone()
        if row is not None:
            _event(cursor, org_id, row["id"], agent_id, "run_created", {"trigger": trigger})
            return row["id"]
        cursor.execute(
            "select id from public.runs where org_id = %s and idempotency_key = %s",
            (str(org_id), idempotency_key),
        )
        existing = cursor.fetchone()
    if existing is None:
        raise RunNotFound(f"No run for idempotency key {idempotency_key!r}")
    return existing["id"]


def retry_run(connection: psycopg.Connection, run_id: UUID | str) -> None:
    """Put a run that failed on an error back to where it can be resumed.

    Only runs that failed with `error` qualify: a hit cap is a decision, not a
    fault, and stays final. The run keeps its checkpoint, so resuming it
    re-runs only the step that failed, and every side effect in a step is
    idempotent (facts are keyed by run and claim), so the retry cannot repeat
    an action that already happened.
    """
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            update public.runs
               set status = 'paused', stop_reason = 'retry', error = null,
                   ended_at = null, lease_expires_at = null, wake_count = 0
             where id = %s and status = 'failed' and stop_reason = 'error'
            returning org_id, agent_id
            """,
            (str(run_id),),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "select status, stop_reason from public.runs where id = %s", (str(run_id),)
            )
            current = cursor.fetchone()
            if current is None:
                raise RunNotFound(str(run_id))
            raise RunBusy(
                f"Run {run_id} is {current['status']} ({current['stop_reason']}); "
                "only a run that failed on an error can be retried"
            )
        _event(cursor, row["org_id"], run_id, row["agent_id"], "run_retry_requested", {})


def advance_run(runtime: Runtime, run_id: UUID | str, *, deadline_seconds: float) -> RunReport:
    """Advance a run as far as it will go within `deadline_seconds`."""
    deadline = time.monotonic() + deadline_seconds
    connection = connect(runtime.dsn)
    try:
        run = _claim(connection, run_id, runtime.lease_seconds)
        stop = _execute(runtime, connection, run, deadline)
        _finish(connection, run, stop)
        return report(connection, run.id)
    finally:
        connection.close()
        runtime.tracer.flush()


def report(connection: psycopg.Connection, run_id: UUID | str) -> RunReport:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            select id, status, stop_reason, steps_taken, tokens_in, tokens_out,
                   cost_usd, output, error
            from public.runs where id = %s
            """,
            (str(run_id),),
        )
        row = cursor.fetchone()
    if row is None:
        raise RunNotFound(str(run_id))
    return RunReport(
        run_id=row["id"],
        status=row["status"],
        stop_reason=row["stop_reason"],
        steps_taken=row["steps_taken"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=Decimal(row["cost_usd"]),
        output=row["output"],
        error=row["error"],
    )


def _execute(runtime: Runtime, connection: psycopg.Connection, run: _Run, deadline: float) -> _Stop:
    if _kill_switch_on(connection, run.org_id):
        return _Stop("paused", "kill_switch")

    # Which code runs this agent is data (ADR 020): its runner.
    if run.runner not in ("pipeline", "deep"):
        return _Stop(
            "failed", "runner_not_built", error=f"The {run.runner!r} runner arrives in Step 8.2"
        )
    slots = deep.PROMPT_SLOTS if run.runner == "deep" else PROMPT_SLOTS
    try:
        prompts = resolve_for_run(
            connection,
            run_id=run.id,
            agent_id=run.agent_id,
            slots=slots,
            pinned=run.prompt_versions,
        )
    except PromptMissing as error:
        return _Stop("paused", "prompt_missing", error=str(error))

    session = lambda: _session(runtime, connection, run)  # noqa: E731
    if run.runner == "deep":
        deep_scope = deep.DeepScope(
            run_id=run.id,
            org_id=run.org_id,
            agent_id=run.agent_id,
            agent_name=run.agent_name,
            task_id=run.task_id,
            system_prompt=prompts["system"].body,
            session=session,
            services=runtime.services,
        )
        build = lambda saver: deep.build_deep_graph(deep_scope, checkpointer=saver)  # noqa: E731
        first_input = lambda: deep.first_message(deep_scope, run.input)  # noqa: E731
        summarise = deep.output
    else:
        scope = RunScope(
            run_id=run.id,
            org_id=run.org_id,
            agent_id=run.agent_id,
            prompts={slot: prompt.body for slot, prompt in prompts.items()},
            session=session,
        )
        build = lambda saver: build_graph(scope, checkpointer=saver)  # noqa: E731
        # A task given to the research pipeline carries its question as the
        # task's instructions or title.
        first_input = lambda: {  # noqa: E731
            **run.input,
            "question": run.input.get("question")
            or run.input.get("instructions")
            or run.input.get("title", ""),
        }
        summarise = _output
    steps = run.steps_taken
    with (
        runtime.tracer.agent_run(
            run_id=str(run.id),
            agent_name=f"{run.runner}-agent",
            user_id=str(run.requested_by),
            input=run.input,
            context={
                "run_id": str(run.id),
                "agent_id": str(run.agent_id),
                "prompt_versions": ",".join(f"{s}={p.version}" for s, p in prompts.items()),
            },
            tags=[f"agent:{run.agent_name}"],
        ) as recorder,
        checkpointer(runtime.dsn) as saver,
    ):
        graph = build(saver)
        config: dict[str, Any] = {
            "configurable": {"thread_id": str(run.id)},
            "callbacks": runtime.tracer.callbacks(),
            "recursion_limit": run.max_steps + 1,
        }
        snapshot = graph.get_state(config)
        started = bool(snapshot.values)
        if started and not snapshot.next:
            stop = _Stop("succeeded", "completed", summarise(snapshot.values))
            recorder.stopped(status=stop.status, stop_reason=stop.reason, output=stop.output)
            return stop

        _lifecycle_event(
            connection,
            run,
            "run_invoked",
            {
                "resumed": started,
                "step": steps,
                "prompt_versions": {slot: p.version for slot, p in prompts.items()},
            },
        )
        stop = None
        try:
            if steps >= run.max_steps:
                stop = _Stop("failed", "max_steps")
            else:
                for update in graph.stream(
                    None if started else first_input(),
                    config,
                    stream_mode="updates",
                    # LangGraph's default, "async", saves a step's checkpoint
                    # while the next step already runs: a crash can lose it,
                    # and the state read below can still see the previous
                    # one, which would end a run early as "completed".
                    durability="sync",
                ):
                    steps += 1
                    tokens = _record_step(
                        connection,
                        run,
                        steps,
                        node=next(iter(update)),
                        lease_seconds=runtime.lease_seconds,
                    )
                    if not graph.get_state(config).next:
                        break  # the graph is finished; nothing left to cap
                    stop = _next_step_blocked(connection, run, steps, tokens, deadline)
                    if stop is not None:
                        break
        except GatewayError as error:
            reason = _PAUSING_REFUSALS.get(error.code)
            if reason is not None:
                stop = _Stop("paused", reason, error=str(error))
            elif isinstance(error, UpstreamError):
                stop = _Stop("paused", "upstream_error", error=str(error))
            else:
                stop = _Stop("failed", "error", error=f"{error.code}: {error}")
        except Exception as error:  # recorded, not raised: the run must end in a known state
            stop = _Stop("failed", "error", error=f"{type(error).__name__}: {error}"[:2000])

        if stop is None:
            stop = _Stop("succeeded", "completed", summarise(graph.get_state(config).values))
        recorder.stopped(status=stop.status, stop_reason=stop.reason, output=stop.output)
        return stop


def _next_step_blocked(
    connection: psycopg.Connection, run: _Run, steps: int, tokens: int, deadline: float
) -> _Stop | None:
    """Whether the run may take another step, checked between steps."""
    if steps >= run.max_steps:
        return _Stop("failed", "max_steps")
    if tokens >= run.max_tokens:
        return _Stop("failed", "max_tokens")
    if _kill_switch_on(connection, run.org_id):
        return _Stop("paused", "kill_switch")
    if time.monotonic() >= deadline:
        return _Stop("paused", "deadline")
    return None


@contextmanager
def _session(runtime: Runtime, connection: psycopg.Connection, run: _Run) -> Iterator[Session]:
    # The run acts for its agent: document scopes (ADR 015) apply to it.
    with acting_as(connection, user_id=str(run.requested_by), agent_id=str(run.agent_id)) as conn:
        gateway = Gateway(
            conn, runtime.transport, runtime.tiers, runtime.tracer, systemone=runtime.systemone
        )
        embedder = runtime.embedder or GatewayEmbedder(
            gateway, agent_id=run.agent_id, run_id=run.id
        )
        brain = Brain(conn, embedder)
        writer = BrainWriter(conn, brain, Judge(conn, gateway)) if runtime.systemone else None
        yield Session(
            gateway=gateway, brain=brain, writer=writer, connection=conn, embedder=embedder
        )


def _claim(connection: psycopg.Connection, run_id: UUID | str, lease_seconds: int) -> _Run:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            update public.runs r
               set status = 'running',
                   started_at = coalesce(r.started_at, now()),
                   lease_expires_at = now() + make_interval(secs => %s)
              from public.agents a
             where r.id = %s
               and a.id = r.agent_id
               and r.status in ('pending', 'running', 'paused')
               and (r.lease_expires_at is null or r.lease_expires_at < now())
            returning r.id, r.org_id, r.agent_id, a.name as agent_name, r.requested_by,
                      r.status, r.input, r.steps_taken, r.max_steps, r.max_tokens,
                      r.prompt_versions, a.runner, r.task_id
            """,
            (lease_seconds, str(run_id)),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "select status, lease_expires_at > now() as leased from public.runs where id = %s",
                (str(run_id),),
            )
            current = cursor.fetchone()
    if row is None:
        if current is None:
            raise RunNotFound(str(run_id))
        if current["status"] in _TERMINAL:
            raise RunBusy(f"Run {run_id} is already {current['status']}")
        raise RunBusy(f"Run {run_id} is being advanced by another invocation")
    if row["requested_by"] is None:
        raise RunBusy(f"Run {run_id} has no requesting user to act as")
    return _Run(**row)


def _record_step(
    connection: psycopg.Connection, run: _Run, steps: int, *, node: str, lease_seconds: int
) -> int:
    """Roll cost up from the ledger, renew the lease, emit the step event.

    Returns the run's total tokens so far, for the token cap.
    """
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            update public.runs r
               set steps_taken = %s,
                   tokens_in = c.tokens_in,
                   tokens_out = c.tokens_out,
                   cost_usd = c.cost_usd,
                   lease_expires_at = now() + make_interval(secs => %s)
              from (
                select coalesce(sum(tokens_in), 0) as tokens_in,
                       coalesce(sum(tokens_out), 0) as tokens_out,
                       coalesce(sum(cost_usd), 0) as cost_usd
                from public.model_calls where run_id = %s
              ) c
             where r.id = %s
            returning r.tokens_in, r.tokens_out, r.cost_usd
            """,
            (steps, lease_seconds, str(run.id), str(run.id)),
        )
        totals = cursor.fetchone()
        _event(
            cursor,
            run.org_id,
            run.id,
            run.agent_id,
            "run_step",
            {
                "node": node,
                "step": steps,
                "tokens_in": totals["tokens_in"],
                "tokens_out": totals["tokens_out"],
                "cost_usd": float(totals["cost_usd"]),
            },
        )
    return int(totals["tokens_in"]) + int(totals["tokens_out"])


def _finish(connection: psycopg.Connection, run: _Run, stop: _Stop) -> None:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            update public.runs
               set status = %s,
                   stop_reason = %s,
                   error = %s,
                   output = coalesce(%s::jsonb, output),
                   ended_at = case when %s in ('succeeded', 'failed', 'cancelled')
                                   then now() end,
                   lease_expires_at = null
             where id = %s
            returning steps_taken, tokens_in, tokens_out, cost_usd
            """,
            (
                stop.status,
                stop.reason,
                stop.error,
                json.dumps(stop.output) if stop.output is not None else None,
                stop.status,
                str(run.id),
            ),
        )
        totals = cursor.fetchone()
        _event(
            cursor,
            run.org_id,
            run.id,
            run.agent_id,
            f"run_{stop.status}",
            {
                "stop_reason": stop.reason,
                "error": stop.error,
                "steps_taken": totals["steps_taken"],
                "tokens_in": totals["tokens_in"],
                "tokens_out": totals["tokens_out"],
                "cost_usd": float(totals["cost_usd"]),
            },
        )


def _kill_switch_on(connection: psycopg.Connection, org_id: UUID) -> bool:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        cursor.execute("select public.kill_switch_on(%s) as engaged", (str(org_id),))
        row = cursor.fetchone()
    return bool(row and row["engaged"])


def _lifecycle_event(
    connection: psycopg.Connection, run: _Run, event_type: str, payload: dict[str, Any]
) -> None:
    with as_service_role(connection) as conn, conn.cursor() as cursor:
        _event(cursor, run.org_id, run.id, run.agent_id, event_type, payload)


def _event(
    cursor: psycopg.Cursor,
    org_id: UUID | str,
    run_id: UUID | str,
    agent_id: UUID | str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    cursor.execute(
        "insert into public.events (org_id, run_id, agent_id, type, payload) "
        "values (%s, %s, %s, %s, %s)",
        (str(org_id), str(run_id), str(agent_id), event_type, json.dumps(payload)),
    )


def _output(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "answer": values.get("answer"),
        "claims": values.get("claims", []),
        "stored_fact_ids": values.get("stored_fact_ids", []),
        "fact_writes": values.get("fact_writes", []),
        "recalled_fact_ids": [f["id"] for f in values.get("recalled", [])],
    }
