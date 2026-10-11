from __future__ import annotations
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import Field, field_validator, model_validator
from .schemas import ApiModel

PanelKind = Literal["pending", "prepared", "background", "automations", "pinned", "mail", "reports", "custom"]


class HomeSources(ApiModel):
    session_ids: list[str] = Field(default_factory=list, max_length=40)
    workspace_ids: list[str] = Field(default_factory=list, max_length=20)
    automation_ids: list[str] = Field(default_factory=list, max_length=20)
    mail_account_ids: list[str] = Field(default_factory=list, max_length=10)
    urls: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("session_ids", "workspace_ids", "automation_ids", "mail_account_ids")
    @classmethod
    def identifiers(cls, values):
        if any(not value or len(value) > 80 for value in values):
            raise ValueError("Invalid source identifier")
        return list(dict.fromkeys(values))

    @field_validator("urls")
    @classmethod
    def public_urls(cls, values):
        from urllib.parse import urlsplit
        import ipaddress
        for value in values:
            url = urlsplit(value)
            if len(value) > 2048 or url.scheme != "https" or not url.hostname or url.username or url.password or url.port not in (None, 443):
                raise ValueError("Use a public HTTPS source")
            if "." not in url.hostname or url.hostname.endswith((".local", ".internal", ".localhost")):
                raise ValueError("Use a public HTTPS source")
            try:
                address = ipaddress.ip_address(url.hostname)
            except ValueError:
                continue
            if not address.is_global:
                raise ValueError("Use a public HTTPS source")
        return list(dict.fromkeys(values))


class PanelDefinition(ApiModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    kind: PanelKind
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    size: Literal["compact", "wide"] = "compact"
    pinned: bool = False
    sources: HomeSources = Field(default_factory=HomeSources)
    profile_id: str | None = Field(default=None, max_length=80)
    refresh_minutes: Literal[60, 1440, 10080] = 1440
    display: Literal["summary", "list", "metrics", "table", "chart"] = "summary"


class ProactivePreference(ApiModel):
    enabled: bool = False
    profile_id: str | None = Field(default=None, max_length=80)
    sources: HomeSources = Field(default_factory=HomeSources)
    timezone: str = "America/Mexico_City"
    language: Literal["en", "es", "fr", "de", "pt"] = "es"
    quiet_start: str | None = None
    quiet_end: str | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, KeyError):
            raise ValueError("Unknown timezone") from None
        return value

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def clock_time(cls, value):
        import re
        if value is not None and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Use HH:MM")
        return value

    @model_validator(mode="after")
    def complete(self):
        if bool(self.quiet_start) != bool(self.quiet_end):
            raise ValueError("Both quiet times are required")
        if self.enabled and not self.profile_id:
            raise ValueError("Choose an agent")
        return self


class HomeLayout(ApiModel):
    revision: int = Field(default=0, ge=0)
    background: Literal["mist", "dusk", "plain"] = "mist"
    reduce_transparency: bool = False
    panels: list[PanelDefinition] = Field(default_factory=lambda: [
        PanelDefinition(id=kind, kind=kind) for kind in ("pending", "prepared", "background", "automations")
    ], max_length=20)
    proactive: ProactivePreference = Field(default_factory=ProactivePreference)

    @model_validator(mode="after")
    def unique_panels(self):
        if len({panel.id for panel in self.panels}) != len(self.panels):
            raise ValueError("Panel identifiers must be unique")
        native = [panel.kind for panel in self.panels if panel.kind != "custom"]
        if len(set(native)) != len(native):
            raise ValueError("Catalog panels cannot be duplicated")
        return self
