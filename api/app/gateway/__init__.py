"""The model gateway: the only path to models.

Agents call `Gateway.complete`. Nothing else talks to a provider, so budgets,
the kill switch, provider restrictions and cost logging cannot be bypassed by
a caller in a hurry.
"""

from app.gateway.errors import (
    AgentDisabled,
    AgentNotFound,
    BudgetExceeded,
    DepartmentDisabled,
    GatewayError,
    KillSwitchEngaged,
    TierNotConfigured,
    UpstreamError,
)
from app.gateway.gateway import AgentRecord, Gateway
from app.gateway.tiers import TIERS, TierMap
from app.gateway.transport import (
    SENSITIVE_PROVIDER_PREFERENCES,
    ModelResponse,
    OpenRouterTransport,
    Transport,
)

__all__ = [
    "SENSITIVE_PROVIDER_PREFERENCES",
    "TIERS",
    "AgentDisabled",
    "AgentNotFound",
    "AgentRecord",
    "BudgetExceeded",
    "DepartmentDisabled",
    "Gateway",
    "GatewayError",
    "KillSwitchEngaged",
    "ModelResponse",
    "OpenRouterTransport",
    "TierMap",
    "TierNotConfigured",
    "Transport",
    "UpstreamError",
]
