from __future__ import annotations

import base64
import json
import os
import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HERMES_CONTROL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    app_name: str = "Agent Control"
    database_url: str = "sqlite:///./hermes-control.db"
    static_dir: str | None = None
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    secure_cookies: bool = False
    session_ttl_hours: int = 12
    realtime_ticket_ttl_seconds: int = 30
    transcription_token_rate_limit: int = Field(default=10, ge=1, le=120)
    transcription_token_rate_window_seconds: int = Field(default=60, ge=10, le=3_600)
    speech_rate_limit: int = Field(default=30, ge=1, le=240)
    speech_rate_window_seconds: int = Field(default=60, ge=10, le=3_600)
    capability_ttl_seconds: int = Field(default=60, ge=5, le=3_600)
    capability_refresh_seconds: int = Field(default=30, ge=5, le=1_800)
    vault_key_b64: str | None = None
    provider_mode: Literal["auto", "real", "mock"] = "real"
    mock_fallback_enabled: bool = False

    hermes_dashboard_url: str = "http://127.0.0.1:19119"
    hermes_dashboard_ws: str = "ws://127.0.0.1:19119/api/ws"
    hermes_api_url: str | None = "http://127.0.0.1:18642"
    hermes_dashboard_token: str | None = None
    hermes_api_key: str | None = None
    # Local, read-only projection of Hermes-owned media. This is deliberately
    # separate from gateway URLs: only the environment-managed gateway may
    # resolve MEDIA references through this filesystem boundary.
    hermes_media_root: str | None = "~/.hermes/profiles"
    hermes_media_max_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1,
        le=500 * 1024 * 1024,
    )
    # Hermes' official 0.20.5/0.20.6 dashboard status response doesn't expose
    # the installed commit.  This operator-supplied value is therefore the
    # only revision identity trusted for enabling audited write contracts.
    hermes_source_sha: str | None = None
    default_gateway_name: str = "Hermes remoto"
    default_profiles: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["default", "jarvis", "control-dev"]
    )
    # Operator-side allowlist. The three canonical profiles are fully usable by
    # default; a listed profile still remains read-only until its gateway has a
    # trusted full source SHA and the audited provider advertises the exact
    # mutation capability.
    mutable_profiles: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["default", "jarvis", "control-dev"]
    )
    # Conversation-only fallback allowlist for installations that deliberately
    # remove a profile from mutable_profiles.
    interactive_profiles: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["default", "jarvis", "control-dev"]
    )

    trust_private_endpoints: bool = True
    max_request_bytes: int = 1_000_000
    ws_queue_size: int = 512
    ws_max_connections: int = 64
    ws_max_connections_per_user: int = 4
    ws_max_inbound_bytes: int = Field(default=4_096, ge=256, le=65_536)
    automation_route_watch_seconds: int = Field(default=30, ge=5, le=300)
    automation_route_stale_seconds: int = Field(default=120, ge=15, le=1_800)
    upstream_health_ttl_seconds: int = Field(default=60, ge=15, le=3_600)
    # Schema creation is always an explicit Alembic step. Tests that need an
    # ephemeral database opt in to metadata creation in their fixture.
    create_schema_on_start: bool = False

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value):
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("default_profiles", "mutable_profiles", "interactive_profiles", mode="before")
    @classmethod
    def parse_profiles(cls, value):
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("default_profiles", "mutable_profiles", "interactive_profiles")
    @classmethod
    def validate_profiles(cls, value: list[str]) -> list[str]:
        if len(value) > 64:
            raise ValueError("Profile lists may contain at most 64 names")
        normalized: list[str] = []
        for item in value:
            name = item.strip() if isinstance(item, str) else ""
            if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
                raise ValueError("Profile names must contain 1 to 120 printable characters")
            if name not in normalized:
                normalized.append(name)
        return normalized

    @field_validator("hermes_source_sha")
    @classmethod
    def validate_hermes_source_sha(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", normalized):
            raise ValueError("Hermes source SHA must contain exactly 40 hexadecimal characters")
        return normalized

    @field_validator("hermes_media_root", mode="before")
    @classmethod
    def normalize_media_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        if "\x00" in normalized or "\n" in normalized or "\r" in normalized:
            raise ValueError("Hermes media root contains invalid characters")
        return normalized

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment == "production":
            if not self.secure_cookies:
                raise ValueError("Production requires secure cookies")
            if not self.vault_key_b64:
                raise ValueError("Production requires HERMES_CONTROL_VAULT_KEY_B64")
            if self.create_schema_on_start:
                raise ValueError("Production requires migrations and CREATE_SCHEMA_ON_START=false")
            if self.provider_mode != "real" or self.mock_fallback_enabled:
                raise ValueError("Production requires the real Hermes provider with mock fallback disabled")
        if self.vault_key_b64 and self.vault_key_b64 != "REPLACE_WITH_BASE64_32_BYTE_KEY":
            try:
                key = base64.urlsafe_b64decode(self.vault_key_b64 + "===")
            except Exception as exc:
                raise ValueError("Vault key must be URL-safe base64") from exc
            if len(key) != 32:
                raise ValueError("Vault key must decode to exactly 32 bytes")
        return self

    def materialize_vault_key(self) -> bytes:
        if self.vault_key_b64 and self.vault_key_b64 != "REPLACE_WITH_BASE64_32_BYTE_KEY":
            return base64.urlsafe_b64decode(self.vault_key_b64 + "===")
        if self.environment == "production":
            raise RuntimeError("Vault key is missing")
        # Ephemeral by design: local mock mode remains runnable without creating
        # a secret file. Any stored secret becomes unreadable after restart.
        return os.urandom(32)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
