from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_CRON_TOKEN = re.compile(r"^[A-Za-z0-9*,/\-]+$")
_RESERVED_PROFILE_NAMES = {
    "default",
    "hermes",
    "root",
    "sudo",
    "test",
    "tmp",
}
_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_WEEKDAY_NAMES = {
    name: index
    for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}
_CRON_FIELDS = (
    (0, 59, {}),
    (0, 23, {}),
    (1, 31, {}),
    (1, 12, _MONTH_NAMES),
    (0, 7, _WEEKDAY_NAMES),
)


def validate_cron_5(value: str) -> str:
    """Validate the five-field Unix cron subset accepted by Control.

    Keeping the accepted grammar deliberately small makes validation
    deterministic across Hermes versions. Wildcards, lists, ranges, steps and
    month/weekday names are supported; nicknames and seconds/year fields are not.
    """

    normalized = " ".join(value.split())
    fields = normalized.split(" ") if normalized else []
    if len(fields) != 5:
        raise ValueError("Schedule must contain exactly five cron fields")
    for field, definition in zip(fields, _CRON_FIELDS, strict=True):
        if not _CRON_TOKEN.fullmatch(field):
            raise ValueError("Schedule contains an unsupported cron token")
        _validate_cron_field(field, *definition)
    return normalized


def _validate_cron_field(
    field: str,
    minimum: int,
    maximum: int,
    aliases: dict[str, int],
) -> None:
    for item in field.split(","):
        if not item:
            raise ValueError("Cron lists cannot contain empty values")
        base, separator, step_text = item.partition("/")
        if separator:
            if "/" in step_text or not step_text.isdigit() or int(step_text) <= 0:
                raise ValueError("Cron step must be a positive integer")
        if base == "*":
            continue
        if "-" in base:
            start_text, separator, end_text = base.partition("-")
            if not separator or "-" in end_text:
                raise ValueError("Cron range is invalid")
            start = _cron_number(start_text, minimum, maximum, aliases)
            end = _cron_number(end_text, minimum, maximum, aliases)
            if start > end:
                raise ValueError("Cron range start cannot exceed its end")
            continue
        _cron_number(base, minimum, maximum, aliases)


def _cron_number(
    value: str,
    minimum: int,
    maximum: int,
    aliases: dict[str, int],
) -> int:
    normalized = value.upper()
    if normalized in aliases:
        return aliases[normalized]
    if not normalized.isdigit():
        raise ValueError("Cron values must be numeric")
    number = int(normalized)
    if number < minimum or number > maximum:
        raise ValueError(f"Cron value must be between {minimum} and {maximum}")
    return number


def validate_iana_timezone(value: str) -> str:
    normalized = value.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Timezone must be a valid IANA timezone") from exc
    return normalized


def validate_automation_timezone(value: str) -> str:
    normalized = value.strip()
    if normalized == "Hermes local":
        return normalized
    return validate_iana_timezone(normalized)


def validate_trusted_source_sha(value: str | None) -> str | None:
    """Normalize an operator-entered full commit SHA.

    Short SHAs and server-reported values are never sufficient to enable a
    mutation contract. ``None`` is reserved for explicit trust revocation on
    PATCH; an empty string is rejected instead of silently changing policy.
    """

    if value is None:
        return None
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise ValueError(
            "Trusted Hermes source SHA must contain exactly 40 hexadecimal characters"
        )
    return normalized


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(item.capitalize() for item in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class ErrorEnvelope(ApiModel):
    code: str
    message: str
    request_id: str | None = None
    retryable: bool = False


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


class AuthView(ApiModel):
    id: str
    name: str
    user_id: str
    username: str
    is_admin: bool
    csrf_token: str | None = None


class GatewayCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    rest_url: str
    ws_url: str
    api_url: str | None = None
    connection_mode: str = Field(default="private", pattern="^(public|private|tunnel)$")
    dashboard_token: str | None = Field(default=None, max_length=4096)
    api_key: str | None = Field(default=None, max_length=4096)
    trusted_source_sha: str | None = None

    @field_validator("trusted_source_sha")
    @classmethod
    def valid_trusted_source_sha(cls, value: str | None) -> str | None:
        return validate_trusted_source_sha(value)


class GatewayUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    rest_url: str | None = None
    ws_url: str | None = None
    api_url: str | None = None
    connection_mode: str | None = Field(default=None, pattern="^(public|private|tunnel)$")
    enabled: bool | None = None
    dashboard_token: str | None = Field(default=None, max_length=4096)
    api_key: str | None = Field(default=None, max_length=4096)
    trusted_source_sha: str | None = None

    @field_validator("trusted_source_sha")
    @classmethod
    def valid_trusted_source_sha(cls, value: str | None) -> str | None:
        return validate_trusted_source_sha(value)


class GatewayView(ApiModel):
    id: str
    name: str
    connection_mode: str
    enabled: bool
    env_managed: bool
    health_status: str
    last_health_at: datetime | None = None
    version: str | None = None
    source_sha: str | None = None
    has_dashboard_token: bool = False
    has_api_key: bool = False
    has_trusted_source_sha: bool = False
    capability_set: dict[str, Any] = Field(default_factory=dict)


class ProfileView(ApiModel):
    gateway_id: str
    profile_name: str
    display_name: str
    status: str
    model: str | None = None
    avatar_url: str | None = None
    mutable: bool = False
    capabilities: dict[str, Any] = Field(default_factory=dict)
    capability_set: dict[str, Any] = Field(default_factory=dict)


class ProfileCreate(ApiModel):
    gateway_id: str = Field(min_length=1, max_length=36)
    technical_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )
    display_name: str = Field(min_length=2, max_length=80)
    # This is an operator-authored setup brief, not a routing label or SOUL
    # document. Keep a generous application boundary while still fitting
    # safely beneath the global one-megabyte request-body limit in UTF-8.
    description: str = Field(min_length=10, max_length=200_000)

    @field_validator("technical_name")
    @classmethod
    def reject_reserved_profile_name(cls, value: str) -> str:
        if value in _RESERVED_PROFILE_NAMES:
            raise ValueError("Technical profile name is reserved")
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(char) < 32 for char in normalized):
            raise ValueError("Display name must be a single printable line")
        return normalized

    @field_validator("description")
    @classmethod
    def trim_profile_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Profile description cannot be blank")
        return normalized


class ProfileCreateView(ApiModel):
    id: str
    gateway_id: str
    technical_name: str
    display_name: str
    avatar_url: str | None = None
    model: str
    status: Literal["ready", "busy", "offline"]
    mutable: bool
    capabilities: dict[str, bool] = Field(default_factory=dict)
    capability_set: dict[str, Any] = Field(default_factory=dict)


class ProfileAvatarView(ApiModel):
    avatar_url: str | None = None


class WorkspaceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class WorkspaceUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    archived: bool | None = None


class WorkspaceView(ApiModel):
    id: str
    name: str
    description: str | None
    color: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SessionCreate(ApiModel):
    gateway_id: str | None = None
    profile_name: str | None = Field(default=None, min_length=1, max_length=120)
    profile_id: str | None = None
    workspace_id: str | None = None
    title: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def route_or_profile_reference(self) -> "SessionCreate":
        if self.profile_id:
            return self
        if self.gateway_id and self.profile_name:
            return self
        raise ValueError("profileId or gatewayId/profileName is required")


class SessionSyncRequest(ApiModel):
    gateway_id: str
    profile_name: str = Field(min_length=1, max_length=120)
    workspace_id: str | None = None


class SessionUpdate(ApiModel):
    workspace_id: str | None = None
    archived: bool | None = None
    pinned: bool | None = None
    display_title: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("display_title")
    @classmethod
    def normalize_display_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Display title cannot be empty")
        return normalized


class SessionView(ApiModel):
    id: str
    gateway_id: str
    workspace_id: str | None
    profile_name: str
    profile_id: str | None = None
    automation_generated: bool = False
    stored_session_id: str
    runtime_session_id: str | None
    title: str | None
    status: str
    replay_epoch: str | None
    last_sequence: int
    pinned_at: datetime | None
    unread: bool
    archived_at: datetime | None
    updated_at: datetime


class PushNotificationConfigView(ApiModel):
    available: bool = True
    application_server_key: str


class PushSubscriptionKeys(ApiModel):
    p256dh: str = Field(min_length=40, max_length=180, pattern=r"^[A-Za-z0-9_-]+$")
    auth: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class PushSubscriptionCreate(ApiModel):
    endpoint: str = Field(min_length=20, max_length=2_048)
    keys: PushSubscriptionKeys
    locale: Literal["de", "en", "es", "fr", "pt"] = "en"


class PushSubscriptionDelete(ApiModel):
    endpoint: str = Field(min_length=20, max_length=2_048)


class PushSubscriptionView(ApiModel):
    id: str
    enabled: bool = True


class SearchItemView(ApiModel):
    id: str
    target_id: str
    kind: Literal["session", "message", "automation", "workspace"]
    title: str
    excerpt: str
    meta: str


class SearchResponse(ApiModel):
    items: list[SearchItemView] = Field(default_factory=list, max_length=100)
    partial: bool = False


class PromptRequest(ApiModel):
    # Setup prompts may wrap a 200k operator brief with bounded instructions.
    content: str = Field(min_length=1, max_length=205_000)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt cannot be blank")
        return value


class ApprovalResponseRequest(ApiModel):
    choice: Literal["once", "session", "always", "deny"]


class ApprovalResponseView(ApiModel):
    request_id: str
    resolved: int = Field(ge=1, le=1_000)
    status: Literal["resolved"] = "resolved"


class ClarificationResponseRequest(ApiModel):
    answer: str | list[str]
    question_id: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("answer", mode="before")
    @classmethod
    def valid_answer(cls, value: Any) -> str | list[str]:
        if isinstance(value, str):
            if not value.strip() or len(value) > 10_000:
                raise ValueError("Clarification answer must be non-empty and at most 10000 characters")
            return value
        if isinstance(value, list):
            if not 1 <= len(value) <= 100:
                raise ValueError("Clarification answer must contain between 1 and 100 choices")
            if any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > 1_000
                for item in value
            ):
                raise ValueError("Clarification choices must be non-empty strings of at most 1000 characters")
            return value
        raise ValueError("Clarification answer must be a string or a list of strings")


class ClarificationResponseView(ApiModel):
    request_id: str
    status: Literal["ok", "expired"]
    remaining: list[str] = Field(default_factory=list, max_length=50)


class OperationView(ApiModel):
    operation_id: str
    status: str
    accepted_at: datetime | None = None


class AutomationCreate(ApiModel):
    gateway_id: str
    profile_name: str
    workspace_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    schedule: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="Hermes local", min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=200_000)
    enabled: bool = True

    @field_validator("schedule")
    @classmethod
    def valid_schedule(cls, value: str) -> str:
        return validate_cron_5(value)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return validate_automation_timezone(value)


class AutomationUpdate(ApiModel):
    workspace_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    prompt: str | None = Field(default=None, min_length=1, max_length=200_000)
    enabled: bool | None = None

    @field_validator("schedule")
    @classmethod
    def valid_schedule(cls, value: str | None) -> str | None:
        return validate_cron_5(value) if value is not None else None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        return validate_automation_timezone(value) if value is not None else None


class AutomationView(ApiModel):
    id: str
    gateway_id: str
    workspace_id: str | None
    profile_name: str
    hermes_automation_id: str | None
    name: str
    schedule: str
    timezone: str
    prompt: str
    enabled: bool
    next_runs: list[str]
    updated_at: datetime


class AutomationRunView(ApiModel):
    id: str
    automation_id: str
    hermes_run_id: str | None
    session_link_id: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error_summary: str | None
    read_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TicketView(ApiModel):
    ticket: str
    expires_at: datetime


class CapabilitiesView(ApiModel):
    gateway_id: str
    profile_name: str
    protocol: str
    version: str | None
    source_sha: str | None
    methods: list[str]
    features: list[str]


class AuditView(ApiModel):
    id: str
    actor_user_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    outcome: str
    details: dict[str, Any]
    request_id: str | None
    created_at: datetime
