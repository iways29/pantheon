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
from app.gateway.factory import gateway_from, tier_map_from, transport_from
from app.gateway.gateway import AgentRecord, Gateway
from app.gateway.model_admin import (
    ModelCatalogue,
    OpenRouterCatalogue,
    UnknownModel,
    assign_model,
    clear_assignment,
)
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
    "ModelCatalogue",
    "ModelResponse",
    "OpenRouterCatalogue",
    "OpenRouterTransport",
    "TierMap",
    "TierNotConfigured",
    "Transport",
    "UnknownModel",
    "UpstreamError",
    "assign_model",
    "clear_assignment",
    "gateway_from",
    "tier_map_from",
    "transport_from",
]
