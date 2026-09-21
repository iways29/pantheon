"""The gateway enforces what agents must not be trusted to enforce themselves.

Budget, kill switch, routing and cost logging run against the real database.
Only the provider is faked, and the fake records exactly what it was asked for
so routing assertions are about the real request, not a stub's opinion.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.db import acting_as, as_service_role
from app.gateway import (
    SENSITIVE_PROVIDER_PREFERENCES,
    AgentDisabled,
    BudgetExceeded,
    DepartmentDisabled,
    Gateway,
    KillSwitchEngaged,
    ModelResponse,
    TierMap,
    TierNotConfigured,
)
from tests.conftest_db import Tenants

TIERS = TierMap(
    models={
        "cheap": "vendor/small-model",
        "standard": "vendor/mid-model",
        "frontier": "vendor/large-model",
    }
)


@dataclass
class RecordingTransport:
    """A provider that records requests and returns a fixed, priced reply."""

    cost_usd: float = 0.002
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        provider_preferences: dict[str, Any] | None = None,
    ) -> ModelResponse:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "provider_preferences": provider_preferences,
            }
        )
        return ModelResponse(
            model=model,
            provider="vendor",
            text="ok",
            tokens_in=11,
            tokens_out=7,
            cost_usd=self.cost_usd,
            latency_ms=42,
        )


def make_department(
    db: psycopg.Connection,
    org_id: UUID,
    *,
    name: str = "research",
    budget: str = "1.0000",
    enabled: bool = True,
) -> UUID:
    with as_service_role(db) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            insert into public.departments (org_id, name, daily_budget_usd, enabled)
            values (%s, %s, %s, %s)
            returning id
            """,
            (str(org_id), name, budget, enabled),
        )
        row = cursor.fetchone()
    assert row is not None
    return row["id"]


def make_agent(
    db: psycopg.Connection,
    org_id: UUID,
    *,
    name: str = "worker",
    tier: str = "cheap",
    department_budget: str = "1.0000",
    department_id: UUID | None = None,
    agent_budget: str | None = None,
    enabled: bool = True,
    department_enabled: bool = True,
) -> UUID:
    if department_id is None:
        department_id = make_department(
            db,
            org_id,
            name=f"dept-for-{name}",
            budget=department_budget,
            enabled=department_enabled,
        )

    with as_service_role(db) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            insert into public.agents
                (org_id, department_id, name, role, model_tier, daily_budget_usd, enabled)
            values (%s, %s, %s, 'worker', %s, %s, %s)
            returning id
            """,
            (str(org_id), str(department_id), name, tier, agent_budget, enabled),
        )
        row = cursor.fetchone()
    assert row is not None
    return row["id"]


def set_kill_switch(db: psycopg.Connection, org_id: UUID, *, on: bool) -> None:
    with as_service_role(db) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            insert into public.system_flags (org_id, key, value)
            values (%s, 'kill_switch', %s)
            on conflict (org_id, key) do update set value = excluded.value
            """,
            (str(org_id), json.dumps(on)),
        )


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


# --- Acceptance: a call is blocked when the kill switch is on ---------------


def test_the_kill_switch_blocks_the_call(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a)
    set_kill_switch(db, tenants.org_a, on=True)

    with pytest.raises(KillSwitchEngaged):
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
            )

    assert transport.calls == [], "a blocked call must never reach the provider"


def test_turning_the_kill_switch_off_again_permits_calls(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a)
    set_kill_switch(db, tenants.org_a, on=True)
    set_kill_switch(db, tenants.org_a, on=False)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
        )

    assert len(transport.calls) == 1


# --- Acceptance: a call is blocked when the agent is over budget ------------


def test_an_agent_over_budget_is_blocked(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, department_budget="0.0030")
    transport.cost_usd = 0.002

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        gateway = Gateway(connection, transport, TIERS)
        gateway.complete(agent_id=agent_id, messages=[{"role": "user", "content": "one"}])
        gateway.complete(agent_id=agent_id, messages=[{"role": "user", "content": "two"}])

    # $0.004 spent against a $0.003 budget: the third call must not happen.
    with pytest.raises(BudgetExceeded) as caught:
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "three"}]
            )

    assert caught.value.scope == "department"
    assert caught.value.spent_usd == pytest.approx(0.004)
    assert caught.value.limit_usd == pytest.approx(0.003)
    assert len(transport.calls) == 2


def test_an_unfunded_department_cannot_spend(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    """A budget of zero means nothing approved yet, not unlimited."""
    agent_id = make_agent(db, tenants.org_a, department_budget="0")

    with pytest.raises(BudgetExceeded):
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
            )

    assert transport.calls == []


def test_a_disabled_agent_is_blocked(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, enabled=False)

    with pytest.raises(AgentDisabled):
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
            )

    assert transport.calls == []


# --- Acceptance: cost and tokens are recorded per call ----------------------


def test_every_call_is_recorded_with_its_cost_and_tokens(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
        )

    with acting_as(db, user_id=str(tenants.user_a)) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select model, provider, tokens_in, tokens_out, cost_usd, latency_ms "
            "from public.model_calls where agent_id = %s",
            (str(agent_id),),
        )
        rows = cursor.fetchall()

    assert len(rows) == 1
    assert rows[0]["model"] == "vendor/small-model"
    assert rows[0]["provider"] == "vendor"
    assert rows[0]["tokens_in"] == 11
    assert rows[0]["tokens_out"] == 7
    assert rows[0]["cost_usd"] == Decimal("0.002000")
    assert rows[0]["latency_ms"] == 42


def test_spend_accumulates_across_calls(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        gateway = Gateway(connection, transport, TIERS)
        gateway.complete(agent_id=agent_id, messages=[{"role": "user", "content": "one"}])
        gateway.complete(agent_id=agent_id, messages=[{"role": "user", "content": "two"}])

        assert gateway.spent_today_usd(agent_id) == Decimal("0.004000")


# --- Acceptance: changing a tier changes the model, with no code change -----


@pytest.mark.parametrize(
    ("tier", "expected_model"),
    [
        ("cheap", "vendor/small-model"),
        ("standard", "vendor/mid-model"),
        ("frontier", "vendor/large-model"),
    ],
)
def test_an_agents_tier_selects_the_model(
    db: psycopg.Connection,
    tenants: Tenants,
    transport: RecordingTransport,
    tier: str,
    expected_model: str,
) -> None:
    agent_id = make_agent(db, tenants.org_a, name=f"worker-{tier}", tier=tier)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
        )

    assert transport.calls[0]["model"] == expected_model


def test_repointing_a_tier_repoints_every_agent_on_it(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    """The config is the only thing that moves; no agent row is touched."""
    agent_id = make_agent(db, tenants.org_a, tier="cheap")
    retiered = TierMap(models={**TIERS.models, "cheap": "vendor/replacement-model"})

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, retiered).complete(
            agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
        )

    assert transport.calls[0]["model"] == "vendor/replacement-model"


def test_an_unconfigured_tier_refuses_loudly(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, tier="frontier")

    with pytest.raises(TierNotConfigured):
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TierMap(models={"cheap": "x"})).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
            )


# --- Sensitive routing and the audit trail ---------------------------------


def test_sensitive_work_restricts_the_provider(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=agent_id,
            messages=[{"role": "user", "content": "confidential"}],
            sensitive=True,
        )

    assert transport.calls[0]["provider_preferences"] == SENSITIVE_PROVIDER_PREFERENCES


def test_ordinary_work_sends_no_provider_restriction(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
        )

    assert transport.calls[0]["provider_preferences"] is None


def test_a_successful_call_emits_an_event(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
        )

    with acting_as(db, user_id=str(tenants.user_a)) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select type, payload from public.events where agent_id = %s", (str(agent_id),)
        )
        rows = cursor.fetchall()

    assert [row["type"] for row in rows] == ["model_call"]
    assert rows[0]["payload"]["cost_usd"] == pytest.approx(0.002)


def test_a_blocked_call_is_also_on_the_audit_trail(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    """A refusal is as much a part of the record as a call that ran.

    The error is caught inside the transaction on purpose. The event is
    written on the caller's connection, so letting the exception escape the
    block would roll the audit row back along with everything else -- see the
    note on Gateway. This is the supported pattern, and the test documents it.
    """
    agent_id = make_agent(db, tenants.org_a)
    set_kill_switch(db, tenants.org_a, on=True)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        with pytest.raises(KillSwitchEngaged):
            Gateway(connection, transport, TIERS).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
            )

    with acting_as(db, user_id=str(tenants.user_a)) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select type, payload from public.events where agent_id = %s", (str(agent_id),)
        )
        rows = cursor.fetchall()

    assert [row["type"] for row in rows] == ["model_call_blocked"]
    assert rows[0]["payload"]["code"] == "kill_switch_engaged"


# --- Departments hold the budget; agents share it --------------------------


def test_one_department_budget_is_shared_across_its_agents(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    """The point of a department budget: one agent's spend limits its siblings."""
    department_id = make_department(db, tenants.org_a, budget="0.0030")
    first = make_agent(db, tenants.org_a, name="first", department_id=department_id)
    second = make_agent(db, tenants.org_a, name="second", department_id=department_id)
    transport.cost_usd = 0.002

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=first, messages=[{"role": "user", "content": "hi"}]
        )
        Gateway(connection, transport, TIERS).complete(
            agent_id=second, messages=[{"role": "user", "content": "hi"}]
        )

    # $0.004 spent between them, against a $0.003 department budget.
    with pytest.raises(BudgetExceeded) as caught:
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=second, messages=[{"role": "user", "content": "again"}]
            )

    assert caught.value.scope == "department"
    assert caught.value.subject_id == str(department_id)
    assert len(transport.calls) == 2


def test_department_spend_aggregates_across_agents(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    department_id = make_department(db, tenants.org_a, budget="1.0000")
    first = make_agent(db, tenants.org_a, name="first", department_id=department_id)
    second = make_agent(db, tenants.org_a, name="second", department_id=department_id)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        gateway = Gateway(connection, transport, TIERS)
        gateway.complete(agent_id=first, messages=[{"role": "user", "content": "hi"}])
        gateway.complete(agent_id=second, messages=[{"role": "user", "content": "hi"}])

        # Per-agent tracking still works alongside the department total.
        assert gateway.spent_today_usd(first) == Decimal("0.002000")
        assert gateway.spent_today_usd(second) == Decimal("0.002000")
        assert gateway.department_spent_today_usd(department_id) == Decimal("0.004000")


def test_a_disabled_department_blocks_its_agents(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, department_enabled=False)

    with pytest.raises(DepartmentDisabled):
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "hi"}]
            )

    assert transport.calls == []


# --- The per-agent sub-cap narrows a department budget, never widens it ----


def test_no_sub_cap_means_the_department_budget_alone_governs(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    agent_id = make_agent(db, tenants.org_a, department_budget="1.0000", agent_budget=None)

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        gateway = Gateway(connection, transport, TIERS)
        for _ in range(3):
            gateway.complete(agent_id=agent_id, messages=[{"role": "user", "content": "hi"}])

    assert len(transport.calls) == 3


def test_a_sub_cap_stops_one_agent_inside_a_funded_department(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    """Throttling a noisy worker without touching the rest of its team."""
    department_id = make_department(db, tenants.org_a, budget="1.0000")
    noisy = make_agent(
        db, tenants.org_a, name="noisy", department_id=department_id, agent_budget="0.0030"
    )
    quiet = make_agent(db, tenants.org_a, name="quiet", department_id=department_id)
    transport.cost_usd = 0.002

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        gateway = Gateway(connection, transport, TIERS)
        gateway.complete(agent_id=noisy, messages=[{"role": "user", "content": "one"}])
        gateway.complete(agent_id=noisy, messages=[{"role": "user", "content": "two"}])

    with pytest.raises(BudgetExceeded) as caught:
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=noisy, messages=[{"role": "user", "content": "three"}]
            )

    assert caught.value.scope == "agent"
    assert caught.value.subject_id == str(noisy)

    # Its colleague is unaffected: the department still has room.
    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        Gateway(connection, transport, TIERS).complete(
            agent_id=quiet, messages=[{"role": "user", "content": "hi"}]
        )

    assert len(transport.calls) == 3


def test_a_sub_cap_cannot_outlive_its_department_budget(
    db: psycopg.Connection, tenants: Tenants, transport: RecordingTransport
) -> None:
    """A generous sub-cap does not buy an agent past an exhausted department."""
    agent_id = make_agent(db, tenants.org_a, department_budget="0.0030", agent_budget="100.0000")
    transport.cost_usd = 0.002

    with acting_as(db, user_id=str(tenants.user_a)) as connection:
        gateway = Gateway(connection, transport, TIERS)
        gateway.complete(agent_id=agent_id, messages=[{"role": "user", "content": "one"}])
        gateway.complete(agent_id=agent_id, messages=[{"role": "user", "content": "two"}])

    with pytest.raises(BudgetExceeded) as caught:
        with acting_as(db, user_id=str(tenants.user_a)) as connection:
            Gateway(connection, transport, TIERS).complete(
                agent_id=agent_id, messages=[{"role": "user", "content": "three"}]
            )

    assert caught.value.scope == "department"


def test_an_agent_cannot_join_another_orgs_department(
    db: psycopg.Connection, tenants: Tenants
) -> None:
    """The composite foreign key, not application code, refuses this."""
    foreign_department = make_department(db, tenants.org_b, name="theirs")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with as_service_role(db) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                insert into public.agents
                    (org_id, department_id, name, role, model_tier)
                values (%s, %s, 'sneaky', 'worker', 'cheap')
                """,
                (str(tenants.org_a), str(foreign_department)),
            )
