"""The only path to models.

Agents never call a provider directly. Every call passes through here so that
four things are true without any agent remembering them: the kill switch is
honoured, the agent's daily budget is enforced, the cost and tokens are
recorded, and an event is written to the audit trail.

The order of the checks matters. The kill switch comes before the budget, and
both come before any money is spent, because a blocked call should cost
nothing.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from app.gateway.errors import (
    AgentDisabled,
    AgentNotFound,
    BudgetExceeded,
    DepartmentDisabled,
    GatewayError,
    KillSwitchEngaged,
    PriceNotConfigured,
    UpstreamError,
)
from app.gateway.systemone import (
    PROVIDER as SYSTEMONE_PROVIDER,
)
from app.gateway.systemone import (
    JsonText,
    Question,
    SystemOneResponse,
    SystemOneTransport,
)
from app.gateway.tiers import TierMap
from app.gateway.transport import SENSITIVE_PROVIDER_PREFERENCES, ModelResponse, Transport
from app.tracing import NullTracer, Tracer


@dataclass(frozen=True)
class AgentRecord:
    """An agent and the department whose budget governs it."""

    id: UUID
    org_id: UUID
    name: str
    model_tier: str
    enabled: bool
    department_id: UUID
    department_name: str
    department_budget_usd: Decimal
    department_enabled: bool
    #: Optional sub-cap. None means only the department budget applies.
    daily_budget_usd: Decimal | None
    #: The model assigned to this agent's tier in the database, department
    #: override first. None means no row, so the MODEL_TIERS default applies.
    assigned_model: str | None = None


class Gateway:
    """Budget, kill switch, routing and cost logging for one connection.

    The connection carries the caller's identity, so which agents are visible
    is decided by RLS rather than by an org_id argument passed in here.

    Refusals are audited on the same connection, which has a consequence worth
    knowing: the `model_call_blocked` event is written inside the caller's
    transaction. Catch `GatewayError` *inside* the transaction block and the
    event persists; let it escape and the rollback takes the audit record with
    it. Surviving the caller's rollback would need a second connection of its
    own, since PostgreSQL has no autonomous transactions -- deliberately not
    built yet, because nothing depends on it before the approval queue.
    """

    def __init__(
        self,
        connection: psycopg.Connection,
        transport: Transport,
        tiers: TierMap,
        tracer: Tracer | None = None,
        systemone: SystemOneTransport | None = None,
    ) -> None:
        self._connection = connection
        self._transport = transport
        self._tiers = tiers
        self._tracer = tracer or NullTracer()
        self._systemone = systemone

    def complete(
        self,
        *,
        agent_id: UUID | str,
        messages: list[dict[str, Any]],
        sensitive: bool = False,
        run_id: UUID | str | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        """Run one model call on behalf of an agent.

        `sensitive=True` restricts routing to providers that will not retain or
        train on the input. It is opt-in per call rather than a property of the
        agent, because the same agent may handle both kinds of work.
        """
        agent = self._load_agent(agent_id)
        run = str(run_id) if run_id else None
        trace_context = _trace_context(agent, run)
        trace_tags = [f"department:{agent.department_name}", f"tier:{agent.model_tier}"]

        self._admit(agent, run_id, tags=trace_tags)

        model = agent.assigned_model or self._tiers.model_for(agent.model_tier)
        preferences = dict(SENSITIVE_PROVIDER_PREFERENCES) if sensitive else None

        with self._tracer.model_call(
            context=trace_context,
            tags=trace_tags,
            run_id=run,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            sensitive=sensitive,
        ) as recorder:
            try:
                response = self._transport.complete(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    provider_preferences=preferences,
                )
            except UpstreamError as error:
                recorder.failed(code=error.code, message=str(error))
                raise
            recorder.succeeded(
                text=response.text,
                served_model=response.model,
                provider=response.provider,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
                latency_ms=response.latency_ms,
            )

        self._record_call(
            agent,
            run_id,
            model=response.model,
            provider=response.provider,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
        )
        self._emit_event(
            agent,
            run_id,
            "model_call",
            {
                "tier": agent.model_tier,
                "requested_model": model,
                "model": response.model,
                "provider": response.provider,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "cost_usd": response.cost_usd,
                "latency_ms": response.latency_ms,
                "sensitive": sensitive,
            },
        )
        return response

    def admit(self, agent_id: UUID | str, *, run_id: UUID | str | None = None) -> AgentRecord:
        """Load an agent and apply every call gate, without making a call.

        For callers that must know before doing other work that the agent may
        spend at all (the judge checks the kill switch before anything else).
        A refusal is audited exactly as a refused call is.
        """
        agent = self._load_agent(agent_id)
        self._admit(agent, run_id)
        return agent

    def evaluate(
        self,
        *,
        agent_id: UUID | str,
        model: str,
        state: JsonText,
        questions: dict[str, Question],
        run_id: UUID | str | None = None,
    ) -> SystemOneResponse:
        """Ask TypeSafe's System One model typed questions about a state.

        Same guarantees as `complete`: kill switch, department budget and
        agent sub-cap first, then one row in model_calls and a `model_call`
        event. TypeSafe reports tokens but not cost, so the price comes from
        model_prices and a model with no price is refused before any request.

        There is no `sensitive` flag: TypeSafe offers no zero-retention route
        below enterprise plans, so sensitive state must never reach this
        method (open decision 11). The judge refuses it before calling.
        """
        if self._systemone is None:
            raise RuntimeError("This gateway has no TypeSafe transport; see judge_from")

        agent = self._load_agent(agent_id)
        self._admit(agent, run_id)
        run = str(run_id) if run_id else None

        price = self._price(agent, SYSTEMONE_PROVIDER, model)
        if price is None:
            error = PriceNotConfigured(SYSTEMONE_PROVIDER, model)
            self._emit_event(agent, run_id, "model_call_blocked", error.detail())
            raise error

        with self._tracer.model_call(
            context=_trace_context(agent, run),
            tags=[f"department:{agent.department_name}", f"provider:{SYSTEMONE_PROVIDER}"],
            run_id=run,
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "state": state,
                            "questions": {k: q.payload() for k, q in questions.items()},
                        }
                    ),
                }
            ],
            max_tokens=None,
            sensitive=False,
        ) as recorder:
            try:
                response = self._systemone.evaluate(model=model, state=state, questions=questions)
            except UpstreamError as error:
                recorder.failed(code=error.code, message=str(error))
                if error.billed is not None:
                    billed_model, tokens_in, tokens_out = error.billed
                    self._record_call(
                        agent,
                        run_id,
                        model=billed_model or model,
                        provider=SYSTEMONE_PROVIDER,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=_cost(price, tokens_in, tokens_out),
                        latency_ms=None,
                    )
                raise
            cost_usd = _cost(price, response.tokens_in, response.tokens_out)
            recorder.succeeded(
                text=json.dumps(
                    {key: answer.model_dump() for key, answer in response.answers.items()}
                ),
                served_model=response.model,
                provider=SYSTEMONE_PROVIDER,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=cost_usd,
                latency_ms=response.latency_ms,
            )

        response = SystemOneResponse(
            model=response.model,
            answers=response.answers,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            latency_ms=response.latency_ms,
            cost_usd=cost_usd,
            raw=response.raw,
        )
        self._record_call(
            agent,
            run_id,
            model=response.model,
            provider=SYSTEMONE_PROVIDER,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=cost_usd,
            latency_ms=response.latency_ms,
        )
        self._emit_event(
            agent,
            run_id,
            "model_call",
            {
                "requested_model": model,
                "model": response.model,
                "provider": SYSTEMONE_PROVIDER,
                "questions": len(questions),
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "cost_usd": cost_usd,
                "latency_ms": response.latency_ms,
                "sensitive": False,
            },
        )
        return response

    def spent_today_usd(self, agent_id: UUID | str) -> Decimal:
        """What this agent has spent since midnight UTC.

        Read from model_calls rather than a running counter: the ledger is the
        source of truth, and a counter would drift the first time a write
        failed halfway.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                select coalesce(sum(cost_usd), 0) as spent
                from public.model_calls
                where agent_id = %s
                  and created_at >= date_trunc('day', now() at time zone 'utc')
                """,
                (str(agent_id),),
            )
            row = cursor.fetchone()
        return Decimal(row["spent"]) if row else Decimal(0)

    def department_spent_today_usd(self, department_id: UUID | str) -> Decimal:
        """What a whole department has spent since midnight UTC.

        Joined through agents rather than denormalised onto model_calls: the
        department an agent belongs to can change, and the ledger should
        reflect where the agent sits now rather than a copy taken at the time.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                select coalesce(sum(mc.cost_usd), 0) as spent
                from public.model_calls mc
                join public.agents a on a.id = mc.agent_id
                where a.department_id = %s
                  and mc.created_at >= date_trunc('day', now() at time zone 'utc')
                """,
                (str(department_id),),
            )
            row = cursor.fetchone()
        return Decimal(row["spent"]) if row else Decimal(0)

    def _load_agent(self, agent_id: UUID | str) -> AgentRecord:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                select a.id, a.org_id, a.name, a.model_tier, a.enabled,
                       a.daily_budget_usd,
                       d.id as department_id,
                       d.name as department_name,
                       d.daily_budget_usd as department_budget_usd,
                       d.enabled as department_enabled,
                       assigned.model as assigned_model
                from public.agents a
                join public.departments d on d.id = a.department_id
                -- ADR 003: the tier's model is data. A department override
                -- wins over the org-wide row; with neither, the gateway falls
                -- back to MODEL_TIERS. Read on every call, in this same
                -- query, so a change applies to the very next call.
                left join lateral (
                    select m.model
                    from public.model_tier_assignments m
                    where m.org_id = a.org_id
                      and m.tier = a.model_tier
                      and (m.department_id = a.department_id or m.department_id is null)
                    order by m.department_id nulls last
                    limit 1
                ) assigned on true
                where a.id = %s
                """,
                (str(agent_id),),
            )
            row = cursor.fetchone()

        if row is None:
            # Indistinguishable from "exists in another org", deliberately:
            # RLS hides it, and so does this.
            raise AgentNotFound(str(agent_id))

        sub_cap = row["daily_budget_usd"]
        return AgentRecord(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            model_tier=row["model_tier"],
            enabled=row["enabled"],
            department_id=row["department_id"],
            department_name=row["department_name"],
            department_budget_usd=Decimal(row["department_budget_usd"]),
            department_enabled=row["department_enabled"],
            daily_budget_usd=None if sub_cap is None else Decimal(sub_cap),
            assigned_model=row["assigned_model"],
        )

    def _admit(
        self,
        agent: AgentRecord,
        run_id: UUID | str | None,
        *,
        tags: list[str] | None = None,
    ) -> None:
        """Apply the call gates; audit and trace a refusal, then re-raise it."""
        run = str(run_id) if run_id else None
        try:
            self._check_permitted(agent)
        except GatewayError as error:
            self._emit_event(agent, run_id, "model_call_blocked", error.detail())
            self._tracer.blocked(
                context=_trace_context(agent, run),
                tags=tags or [f"department:{agent.department_name}"],
                run_id=run,
                detail=error.detail(),
            )
            raise

    def _price(
        self, agent: AgentRecord, provider: str, model: str
    ) -> tuple[Decimal, Decimal] | None:
        """USD per million input and output tokens, read on every call."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                select input_usd_per_mtok, output_usd_per_mtok
                from public.model_prices
                where org_id = %s and provider = %s and model = %s
                """,
                (str(agent.org_id), provider, model),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return Decimal(row["input_usd_per_mtok"]), Decimal(row["output_usd_per_mtok"])

    def _check_permitted(self, agent: AgentRecord) -> None:
        """Every gate, cheapest and broadest first.

        The org kill switch outranks the department switch, which outranks the
        agent's own. Budgets come last because they cost a query, and the
        department budget is checked before the agent sub-cap: the department
        is the real limit, the sub-cap only narrows it.
        """
        if not agent.enabled:
            raise AgentDisabled(str(agent.id))
        if not agent.department_enabled:
            raise DepartmentDisabled(str(agent.department_id))

        with self._connection.cursor() as cursor:
            cursor.execute("select public.kill_switch_on(%s) as engaged", (str(agent.org_id),))
            row = cursor.fetchone()
        if row and row["engaged"]:
            raise KillSwitchEngaged(str(agent.org_id))

        department_spent = self.department_spent_today_usd(agent.department_id)
        if department_spent >= agent.department_budget_usd:
            raise BudgetExceeded(
                scope="department",
                subject_id=str(agent.department_id),
                spent_usd=float(department_spent),
                limit_usd=float(agent.department_budget_usd),
            )

        if agent.daily_budget_usd is not None:
            agent_spent = self.spent_today_usd(agent.id)
            if agent_spent >= agent.daily_budget_usd:
                raise BudgetExceeded(
                    scope="agent",
                    subject_id=str(agent.id),
                    spent_usd=float(agent_spent),
                    limit_usd=float(agent.daily_budget_usd),
                )

    def _record_call(
        self,
        agent: AgentRecord,
        run_id: UUID | str | None,
        *,
        model: str,
        provider: str | None,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int | None,
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.model_calls
                    (org_id, run_id, agent_id, model, provider,
                     tokens_in, tokens_out, cost_usd, latency_ms)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(agent.org_id),
                    str(run_id) if run_id else None,
                    str(agent.id),
                    model,
                    provider,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    latency_ms,
                ),
            )

    def _emit_event(
        self,
        agent: AgentRecord,
        run_id: UUID | str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.events (org_id, run_id, agent_id, type, payload)
                values (%s, %s, %s, %s, %s)
                """,
                (
                    str(agent.org_id),
                    str(run_id) if run_id else None,
                    str(agent.id),
                    event_type,
                    json.dumps(payload),
                ),
            )


def _cost(price: tuple[Decimal, Decimal], tokens_in: int, tokens_out: int) -> float:
    per_input, per_output = price
    return float((per_input * tokens_in + per_output * tokens_out) / Decimal(1_000_000))


def _trace_context(agent: AgentRecord, run_id: str | None) -> dict[str, str]:
    """Who made the call, for filtering traces. Ids only; no message content."""
    context = {
        "org_id": str(agent.org_id),
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "department_id": str(agent.department_id),
        "tier": agent.model_tier,
    }
    if run_id:
        context["run_id"] = run_id
    return context
