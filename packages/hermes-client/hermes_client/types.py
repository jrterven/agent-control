from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SessionRoute:
    """Unambiguous identity for a Hermes session.

    ``stored_session_id`` survives gateway restarts while ``runtime_session_id``
    identifies only the currently attached process.
    """

    gateway_id: str
    profile_name: str
    stored_session_id: str
    runtime_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    protocol: str = "dashboard-jsonrpc"
    version: str | None = None
    source_sha: str | None = None
    methods: frozenset[str] = field(default_factory=frozenset)
    features: frozenset[str] = field(default_factory=frozenset)

    def supports(self, name: str) -> bool:
        return name in self.methods or name in self.features

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "version": self.version,
            "sourceSha": self.source_sha,
            "methods": sorted(self.methods),
            "features": sorted(self.features),
        }


@dataclass(slots=True)
class NormalizedEvent:
    event_id: str
    type: str
    timestamp: datetime
    gateway_id: str
    profile_name: str
    stored_session_id: str | None
    runtime_session_id: str | None
    sequence: int | None
    replay_epoch: str | None
    correlation_id: str | None
    data: dict[str, Any]
    # Internal connection-generation identity.  It is intentionally not part
    # of the public NormalizedEvent contract; Control uses it to reject a
    # runtime id that Hermes reuses after a reconnect or process restart.
    runtime_generation: str | None = None

    @classmethod
    def create(
        cls,
        *,
        type: str,
        gateway_id: str,
        profile_name: str,
        data: dict[str, Any] | None = None,
        stored_session_id: str | None = None,
        runtime_session_id: str | None = None,
        sequence: int | None = None,
        replay_epoch: str | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
        runtime_generation: str | None = None,
    ) -> "NormalizedEvent":
        return cls(
            event_id=event_id or uuid4().hex,
            type=type,
            timestamp=utc_now(),
            gateway_id=gateway_id,
            profile_name=profile_name,
            stored_session_id=stored_session_id,
            runtime_session_id=runtime_session_id,
            sequence=sequence,
            replay_epoch=replay_epoch,
            correlation_id=correlation_id,
            data=data or {},
            runtime_generation=runtime_generation,
        )

    def to_dict(self) -> dict[str, Any]:
        canonical = {
            "eventId": self.event_id,
            "type": self.type,
            "occurredAt": self.timestamp.isoformat(),
            "gatewayId": self.gateway_id,
            "profileName": self.profile_name,
            "storedSessionId": self.stored_session_id,
            "runtimeSessionId": self.runtime_session_id,
            "seq": self.sequence,
            "replayEpoch": self.replay_epoch,
            "correlationId": self.correlation_id,
            "data": self.data,
            "_runtimeGeneration": self.runtime_generation,
        }
        # Temporary aliases for the first mobile prototype. New consumers must
        # use eventId/occurredAt/seq/data from the canonical contract.
        canonical.update(
            {
                "id": self.event_id,
                "timestamp": self.timestamp.isoformat(),
                "sequence": self.sequence,
                "payload": self.data,
            }
        )
        return canonical


@dataclass(frozen=True, slots=True)
class HermesProfile:
    name: str
    display_name: str
    status: Literal["online", "offline", "degraded", "unknown"] = "unknown"
    model: str | None = None


@dataclass(frozen=True, slots=True)
class HermesSession:
    stored_session_id: str
    runtime_session_id: str | None
    title: str | None = None
    status: str = "idle"
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class HermesSearchResult:
    """One bounded hit returned by Hermes' authoritative session index."""

    stored_session_id: str
    snippet: str = ""
    title: str | None = None
    role: str | None = None
    lineage_root: str | None = None


@dataclass(frozen=True, slots=True)
class PromptReceipt:
    operation_id: str
    status: Literal["accepted", "streaming", "completed"] = "accepted"
    accepted_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class PromptAttachment:
    """One browser-selected attachment held only for the prompt hand-off."""

    kind: Literal["image", "file"]
    name: str
    media_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class PromptAttachmentReceipt:
    """Private Hermes attachment result used while dispatching a prompt."""

    reference: str | None = None
    detach_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HermesAutomation:
    automation_id: str
    name: str
    schedule: str
    timezone: str
    enabled: bool
    prompt: str
    next_runs: tuple[datetime, ...] = ()


@dataclass(frozen=True, slots=True)
class HermesRunReceipt:
    run_id: str | None
    status: str = "queued"
    stored_session_id: str | None = None
    runtime_session_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
