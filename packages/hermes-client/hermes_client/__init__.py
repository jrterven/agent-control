"""Public API for the Hermes Control protocol adapter."""

from .admin import (
    AdminResourceName,
    AdminResourceSnapshot,
    admin_snapshot,
    contains_secret_fields,
    sanitize_admin_payload,
    writable_config_projection,
)
from .normalization import EventNormalizer
from .provider import (
    HermesGatewayProvider,
    HermesProvider,
    InMemoryHermesProvider,
    ProviderConnection,
    RuntimeGenerationChanged,
    SessionHistoryNotFound,
)
from .replay import ReplayDecision, ReplayState
from .routing import HermesSessionRouter, ProviderPool, RouteMismatchError
from .security import EndpointPolicy, ResolvedEndpoint, UnsafeEndpointError, resolve_endpoint, validate_endpoint
from .transport import JsonRpcClient, JsonRpcError
from .types import (
    CapabilitySet,
    HermesAutomation,
    HermesProfile,
    HermesRunReceipt,
    HermesSearchResult,
    HermesSession,
    NormalizedEvent,
    PromptReceipt,
    SessionRoute,
)

__all__ = [
    "AdminResourceName",
    "AdminResourceSnapshot",
    "CapabilitySet",
    "EndpointPolicy",
    "EventNormalizer",
    "HermesAutomation",
    "HermesGatewayProvider",
    "HermesProfile",
    "HermesRunReceipt",
    "HermesSearchResult",
    "HermesProvider",
    "HermesSession",
    "HermesSessionRouter",
    "InMemoryHermesProvider",
    "JsonRpcClient",
    "JsonRpcError",
    "NormalizedEvent",
    "PromptReceipt",
    "ProviderConnection",
    "ProviderPool",
    "ReplayDecision",
    "ReplayState",
    "ResolvedEndpoint",
    "RouteMismatchError",
    "RuntimeGenerationChanged",
    "SessionHistoryNotFound",
    "SessionRoute",
    "UnsafeEndpointError",
    "admin_snapshot",
    "contains_secret_fields",
    "resolve_endpoint",
    "sanitize_admin_payload",
    "validate_endpoint",
    "writable_config_projection",
]
