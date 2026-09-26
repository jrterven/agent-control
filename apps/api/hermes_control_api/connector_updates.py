"""Bounded release telemetry and owner-controlled update intents.

The cloud never supplies executable commands, paths, or download URLs. The
local updater resolves and verifies its own signed publication and idle gate.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class UpdateStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1]
    supported: bool
    release: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    availableRelease: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    state: Literal["current", "checking", "available", "waiting", "downloading", "installing", "failed", "manual", "paused"]
    reason: Literal["busy", "temporary", "offline", "recovery", "verification", "service", "rollout", "unsupported", "externalHermes"] | None = None
    checkedAt: int | None = Field(default=None, ge=0, le=100_000_000_000)
    requestId: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def update_control(row):
    return {"automatic": True, "requestId": None, "pausedUntil": 0, **(row.update_settings or {})}


def update_view(row):
    status = row.update_status or {"protocol": 1, "supported": False, "state": "manual", "reason": "unsupported"}
    return {**status, "automatic": update_control(row)["automatic"],
            "pausedUntil": update_control(row)["pausedUntil"]}
