from __future__ import annotations

from typing import Any, Literal

from hermes_client import contains_secret_fields
from hermes_client.limits import validate_json_shape
from pydantic import Field, SecretStr, field_validator, model_validator

from .schemas import ApiModel


_RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,199}$"
_ENV_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{0,199}$"


class AdminResourceView(ApiModel):
    gateway_id: str
    profile_name: str
    resource: Literal[
        "models",
        "config",
        "soul",
        "memory",
        "skills",
        "toolsets",
        "mcp",
        "channels",
        "usage",
        "secrets",
    ]
    data: dict[str, Any]


class ModelSelection(ApiModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=300)
    confirm_expensive_model: bool = False


class ConfigMutation(ApiModel):
    config: dict[str, Any]

    @field_validator("config")
    @classmethod
    def safe_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_json_shape(value, max_depth=16, max_nodes=10_000)
        if contains_secret_fields(value):
            raise ValueError(
                "Secret-shaped values must use the write-only secrets endpoint"
            )
        return value


class SoulMutation(ApiModel):
    content: str = Field(max_length=256_000)


class MemoryProviderMutation(ApiModel):
    provider: str = Field(default="", max_length=120)


class MemoryResetMutation(ApiModel):
    target: Literal["all", "memory", "user"] = "all"


class ToggleMutation(ApiModel):
    enabled: bool


class McpServerCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120, pattern=_RESOURCE_ID_PATTERN)
    url: str | None = Field(default=None, max_length=2_048)
    command: str | None = Field(default=None, max_length=2_048)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, SecretStr] = Field(default_factory=dict)
    auth: str | None = Field(default=None, max_length=120)
    bearer_token: SecretStr | None = None
    enabled: bool = True

    @field_validator("args")
    @classmethod
    def bounded_args(cls, value: list[str]) -> list[str]:
        if any(len(item) > 2_048 for item in value):
            raise ValueError("MCP argument is too long")
        return value

    @field_validator("env")
    @classmethod
    def valid_env(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        if len(value) > 100:
            raise ValueError("Too many MCP environment variables")
        for key, secret in value.items():
            if not key or len(key) > 200 or len(secret.get_secret_value()) > 16_384:
                raise ValueError("Invalid MCP environment value")
        return value

    @model_validator(mode="after")
    def one_transport(self) -> "McpServerCreate":
        if bool(self.url) == bool(self.command):
            raise ValueError("Exactly one of url or command is required")
        if self.url and self.args:
            raise ValueError("Arguments are supported only for stdio MCP servers")
        if self.url and self.env:
            raise ValueError("Environment variables are supported only for stdio MCP servers")
        auth = (self.auth or "none").strip().lower()
        if auth not in {"none", "header", "oauth"}:
            raise ValueError("Unsupported MCP authentication mode")
        if self.bearer_token is not None:
            if not self.url:
                raise ValueError("Bearer authentication requires an HTTP MCP URL")
            if auth not in {"none", "header"}:
                raise ValueError("Bearer token is incompatible with this MCP authentication mode")
            # The mobile form intentionally presents a single write-only token
            # field. Translate that safe shorthand to Hermes' explicit
            # dashboard contract instead of sending an invalid auth='none'.
            self.auth = "header"
        elif auth == "header":
            raise ValueError("Header authentication requires a bearer token")
        elif self.command and auth != "none":
            raise ValueError("stdio MCP servers do not support HTTP authentication")
        else:
            self.auth = auth
        return self

    def upstream(self) -> dict[str, Any]:
        payload = self.model_dump(
            by_alias=False,
            exclude={"env", "bearer_token"},
            exclude_none=True,
        )
        payload["env"] = {
            key: value.get_secret_value() for key, value in self.env.items()
        }
        if self.bearer_token is not None:
            payload["bearer_token"] = self.bearer_token.get_secret_value()
        return payload


class ChannelMutation(ApiModel):
    enabled: bool | None = None
    env: dict[str, SecretStr] = Field(default_factory=dict)
    clear_env: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("env")
    @classmethod
    def valid_env(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        if len(value) > 100:
            raise ValueError("Too many channel environment variables")
        for key, secret in value.items():
            if (
                not __import__("re").fullmatch(_ENV_NAME_PATTERN, key)
                or len(secret.get_secret_value()) > 16_384
            ):
                raise ValueError("Invalid channel environment value")
        return value

    @field_validator("clear_env")
    @classmethod
    def valid_clear_env(cls, value: list[str]) -> list[str]:
        if any(not __import__("re").fullmatch(_ENV_NAME_PATTERN, item) for item in value):
            raise ValueError("Invalid channel environment name")
        return value

    def upstream(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "env": {key: value.get_secret_value() for key, value in self.env.items()},
            "clear_env": list(self.clear_env),
        }


class SecretMutation(ApiModel):
    value: SecretStr

    @field_validator("value")
    @classmethod
    def valid_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or len(raw) > 16_384:
            raise ValueError("Secret value is missing or too large")
        return value
