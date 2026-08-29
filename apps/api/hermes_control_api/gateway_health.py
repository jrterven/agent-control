from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Protocol


class ProfileHealthRecord(Protocol):
    status: str
    last_seen_at: datetime | None


class GatewayHealthRecord(Protocol):
    health_status: str
    last_health_at: datetime | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def profile_health_state(
    profile: ProfileHealthRecord,
    *,
    at: datetime,
    ttl_seconds: int,
) -> str:
    observed_at = profile.last_seen_at
    if observed_at is None:
        return "unknown"
    if (_as_utc(at) - _as_utc(observed_at)).total_seconds() > ttl_seconds:
        return "stale"
    return (
        profile.status
        if profile.status in {"online", "offline", "degraded"}
        else "unknown"
    )


def aggregate_profile_health(
    profiles: Iterable[ProfileHealthRecord],
    *,
    at: datetime,
    ttl_seconds: int,
) -> str:
    """Fail-closed aggregate for every configured/discovered profile route."""

    states = [
        profile_health_state(profile, at=at, ttl_seconds=ttl_seconds)
        for profile in profiles
    ]
    if states and all(state == "online" for state in states):
        return "online"
    if states and all(state == "offline" for state in states):
        return "offline"
    if states and all(state in {"unknown", "stale"} for state in states):
        return "unknown"
    return "degraded" if states else "unknown"


def gateway_health_state(
    gateway: GatewayHealthRecord,
    *,
    at: datetime,
    ttl_seconds: int,
) -> str:
    """Project cached gateway health only while its observation is fresh."""

    observed_at = gateway.last_health_at
    if observed_at is None:
        return "unknown"
    if (_as_utc(at) - _as_utc(observed_at)).total_seconds() > ttl_seconds:
        return "stale"
    return (
        gateway.health_status
        if gateway.health_status in {"online", "offline", "degraded"}
        else "unknown"
    )
