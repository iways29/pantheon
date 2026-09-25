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
    PriceNotConfigured,
    TierNotConfigured,
    UpstreamError,
)
from app.gateway.factory import (
    gateway_from,
    systemone_transport_from,
    tier_map_from,
    transport_from,
)
from app.gateway.gateway import AgentRecord, Gateway
from app.gateway.model_admin import (
    ModelCatalogue,
    OpenRouterCatalogue,
    UnknownModel,
    assign_model,
    clear_assignment,
)
from app.gateway.systemone import (
    ChoiceAnswer,
    CircuitBreaker,
    NoulAnswer,
    Question,
    ScoreAnswer,
    SystemOneResponse,
    SystemOneTransport,
    TypeSafeTransport,
)
from app.gateway.tiers import EMBEDDING_DIMENSIONS, EMBEDDING_TIER, TIERS, TierMap
from app.gateway.transport import (
    SENSITIVE_PROVIDER_PREFERENCES,
    EmbeddingResponse,
    EmbeddingTransport,
    ModelResponse,
    OpenRouterTransport,
    ToolCall,
    Transport,
)

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_TIER",
    "SENSITIVE_PROVIDER_PREFERENCES",
    "TIERS",
    "AgentDisabled",
    "AgentNotFound",
    "AgentRecord",
    "BudgetExceeded",
    "ChoiceAnswer",
    "CircuitBreaker",
    "DepartmentDisabled",
    "EmbeddingResponse",
    "EmbeddingTransport",
    "Gateway",
    "GatewayError",
    "KillSwitchEngaged",
    "ModelCatalogue",
    "ModelResponse",
    "NoulAnswer",
    "OpenRouterCatalogue",
    "OpenRouterTransport",
    "PriceNotConfigured",
    "Question",
    "ScoreAnswer",
    "SystemOneResponse",
    "SystemOneTransport",
    "TierMap",
    "TierNotConfigured",
    "ToolCall",
    "Transport",
    "TypeSafeTransport",
    "UnknownModel",
    "UpstreamError",
    "assign_model",
    "clear_assignment",
    "gateway_from",
    "systemone_transport_from",
    "tier_map_from",
    "transport_from",
]
