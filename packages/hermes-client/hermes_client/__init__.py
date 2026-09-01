"""Public API for the Hermes Control protocol adapter."""

from .admin import (
    AdminResourceName,
    AdminResourceSnapshot,
    admin_snapshot,
    contains_secret_fields,
    sanitize_admin_payload,
    writable_config_projection,
)
from .attachments import compose_attachment_prompt, project_attachment_prompt
from .email_references import (
    EMAIL_REFERENCE_MARKER_NAME,
    EMAIL_REFERENCE_MARKER_PREFIX,
    EmailReferenceCandidate,
    ParsedEmailReferenceMarker,
    compose_email_reference_prompt,
    email_reference_candidates,
    has_email_reference_instruction,
    project_email_reference_prompt,
    parse_email_reference_marker,
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
    PromptAttachment,
    PromptAttachmentReceipt,
    SessionRoute,
)

__all__ = [
    "AdminResourceName",
    "AdminResourceSnapshot",
    "CapabilitySet",
    "EndpointPolicy",
    "EmailReferenceCandidate",
    "EMAIL_REFERENCE_MARKER_NAME",
    "EMAIL_REFERENCE_MARKER_PREFIX",
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
    "PromptAttachment",
    "PromptAttachmentReceipt",
    "ParsedEmailReferenceMarker",
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
    "compose_attachment_prompt",
    "compose_email_reference_prompt",
    "email_reference_candidates",
    "has_email_reference_instruction",
    "resolve_endpoint",
    "project_attachment_prompt",
    "project_email_reference_prompt",
    "parse_email_reference_marker",
    "sanitize_admin_payload",
    "validate_endpoint",
    "writable_config_projection",
]
