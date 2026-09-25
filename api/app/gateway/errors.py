"""Structured refusals from the gateway.

Every block carries a machine-readable code and the numbers behind the
decision, so a caller can tell "you are over budget by $0.03" from "the kill
switch is on" without parsing prose.
"""

from typing import Any


class GatewayError(RuntimeError):
    """Base for every refusal. `code` is stable; the message is not."""

    code = "gateway_error"

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


class KillSwitchEngaged(GatewayError):
    code = "kill_switch_engaged"

    def __init__(self, org_id: str) -> None:
        super().__init__("The kill switch is on; no model calls are permitted")
        self.org_id = org_id


class AgentDisabled(GatewayError):
    code = "agent_disabled"

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"Agent {agent_id} is disabled")
        self.agent_id = agent_id


class AgentNotFound(GatewayError):
    code = "agent_not_found"

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"No agent {agent_id} is visible to this caller")
        self.agent_id = agent_id


class DepartmentDisabled(GatewayError):
    code = "department_disabled"

    def __init__(self, department_id: str) -> None:
        super().__init__(f"Department {department_id} is disabled")
        self.department_id = department_id


class BudgetExceeded(GatewayError):
    """A daily allowance is spent.

    `scope` says which one: the department budget that governs the whole team,
    or an optional per-agent sub-cap inside it. A caller that wants to raise
    the right limit needs to know which was hit.

    A budget of zero is not "unlimited", it is "nothing approved yet". Cost
    control is the first priority in CLAUDE.md, so an unfunded department
    cannot spend.
    """

    code = "budget_exceeded"

    def __init__(
        self,
        *,
        scope: str,
        subject_id: str,
        spent_usd: float,
        limit_usd: float,
    ) -> None:
        super().__init__(
            f"{scope.capitalize()} {subject_id} has spent ${spent_usd:.6f} "
            f"of its ${limit_usd:.4f} daily budget"
        )
        self.scope = scope
        self.subject_id = subject_id
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd

    def detail(self) -> dict[str, Any]:
        return {
            **super().detail(),
            "scope": self.scope,
            "subject_id": self.subject_id,
            "spent_usd": self.spent_usd,
            "limit_usd": self.limit_usd,
        }


class TierNotConfigured(GatewayError):
    code = "tier_not_configured"

    def __init__(self, tier: str) -> None:
        super().__init__(
            f"No model is configured for tier {tier!r}. Set MODEL_TIERS; see .env.example."
        )
        self.tier = tier


class UpstreamError(GatewayError):
    """The provider refused or failed.

    `reason` says which way, for callers that react differently to each (the
    judge applies a gate's fail mode to any of them): `timeout`,
    `unreachable`, `rate_limited`, `overloaded`, `server_error`, `rejected`
    (a 4xx the provider will not change its mind about), `circuit_open`
    (recent failures, so this call was not attempted) and `malformed` (an
    answer that does not match the contract). None where not classified.
    """

    code = "upstream_error"

    def __init__(
        self, message: str, *, status: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        #: (model, tokens_in, tokens_out) when the provider answered and billed
        #: but the answer was unusable; None otherwise.
        self.billed: tuple[str, int, int] | None = None

    def detail(self) -> dict[str, Any]:
        return {**super().detail(), "status": self.status, "reason": self.reason}


class PriceNotConfigured(GatewayError):
    """A provider that does not report cost, with no price in model_prices.

    Refused rather than logged at zero: an unpriced call would slip past the
    department budget.
    """

    code = "price_not_configured"

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(
            f"No price for {provider} model {model!r} in model_prices; add one before calling it"
        )
        self.provider = provider
        self.model = model

    def detail(self) -> dict[str, Any]:
        return {**super().detail(), "provider": self.provider, "model": self.model}
