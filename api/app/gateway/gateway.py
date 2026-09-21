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
    GatewayError,
    KillSwitchEngaged,
)
from app.gateway.tiers import TierMap
from app.gateway.transport import SENSITIVE_PROVIDER_PREFERENCES, ModelResponse, Transport


@dataclass(frozen=True)
class AgentRecord:
    id: UUID
    org_id: UUID
    name: str
    model_tier: str
    daily_budget_usd: Decimal
    enabled: bool


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
    ) -> None:
        self._connection = connection
        self._transport = transport
        self._tiers = tiers

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

        try:
            self._check_permitted(agent)
        except GatewayError as error:
            self._emit_event(agent, run_id, "model_call_blocked", error.detail())
            raise

        model = self._tiers.model_for(agent.model_tier)
        preferences = dict(SENSITIVE_PROVIDER_PREFERENCES) if sensitive else None

        response = self._transport.complete(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            provider_preferences=preferences,
        )

        self._record_call(agent, run_id, response)
        self._emit_event(
            agent,
            run_id,
            "model_call",
            {
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

    def _load_agent(self, agent_id: UUID | str) -> AgentRecord:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                select id, org_id, name, model_tier, daily_budget_usd, enabled
                from public.agents
                where id = %s
                """,
                (str(agent_id),),
            )
            row = cursor.fetchone()

        if row is None:
            # Indistinguishable from "exists in another org", deliberately:
            # RLS hides it, and so does this.
            raise AgentNotFound(str(agent_id))

        return AgentRecord(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            model_tier=row["model_tier"],
            daily_budget_usd=Decimal(row["daily_budget_usd"]),
            enabled=row["enabled"],
        )

    def _check_permitted(self, agent: AgentRecord) -> None:
        if not agent.enabled:
            raise AgentDisabled(str(agent.id))

        with self._connection.cursor() as cursor:
            cursor.execute("select public.kill_switch_on(%s) as engaged", (str(agent.org_id),))
            row = cursor.fetchone()
        if row and row["engaged"]:
            raise KillSwitchEngaged(str(agent.org_id))

        spent = self.spent_today_usd(agent.id)
        if spent >= agent.daily_budget_usd:
            raise BudgetExceeded(str(agent.id), float(spent), float(agent.daily_budget_usd))

    def _record_call(
        self,
        agent: AgentRecord,
        run_id: UUID | str | None,
        response: ModelResponse,
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
                    response.model,
                    response.provider,
                    response.tokens_in,
                    response.tokens_out,
                    response.cost_usd,
                    response.latency_ms,
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
