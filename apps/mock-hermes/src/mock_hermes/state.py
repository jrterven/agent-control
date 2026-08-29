from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any


@dataclass(frozen=True, slots=True)
class MockProfile:
    name: str
    display_name: str
    model: str
    running: bool = True


@dataclass(slots=True)
class MockMessage:
    id: str
    role: str
    content: str
    created_at: str
    status: str = "complete"


@dataclass(slots=True)
class MockSession:
    stored_session_id: str
    runtime_session_id: str
    profile: str
    title: str
    created_at: str
    updated_at: str
    persisted: bool = False
    running: bool = False
    archived: bool = False
    messages: list[MockMessage] = field(default_factory=list)
    events: deque[dict[str, Any]] = field(default_factory=deque)
    latest_seq: int = 0
    pending_approval_id: str | None = None
    pending_approval: dict[str, Any] | None = None
    pending_clarify: dict[str, Any] | None = None
    clarify_answers: dict[str, str | list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class FaultSettings:
    disconnect_after_submit: bool = False
    unknown_event: bool = False
    force_replay_truncated: bool = False
    missing_endpoints: set[str] = field(default_factory=set)
    stream_delay_ms: int = 5


class MockHermesState:
    """In-memory deterministic state shared by both mock protocol surfaces."""

    PROFILES = (
        MockProfile("default", "Newton", "gpt-5.6-sol"),
        MockProfile("jarvis", "Jarvis", "gpt-5.6-sol"),
        MockProfile("control-dev", "Control Dev", "gpt-5.6-sol"),
    )

    def __init__(self, *, replay_buffer_size: int = 512) -> None:
        self.replay_buffer_size = replay_buffer_size
        self.lock = RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", RLock()):
            self._counter = 0
            self._clock_tick = 0
            self.replay_epoch = "mock-epoch-0001"
            self.sessions_by_stored: dict[str, MockSession] = {}
            self.runtime_to_stored: dict[str, str] = {}
            self.cron_jobs: dict[str, dict[str, Any]] = {}
            self.cron_runs: dict[str, list[dict[str, Any]]] = {}
            self.runs: dict[str, dict[str, Any]] = {}
            self.admin_profiles: dict[str, dict[str, Any]] = {
                profile.name: self._initial_admin_profile(profile)
                for profile in self.PROFILES
            }
            self.faults = FaultSettings()

    @staticmethod
    def _initial_admin_profile(profile: MockProfile) -> dict[str, Any]:
        return {
            "model": {"provider": "mock", "model": profile.model},
            "config": {
                "timezone": "America/Mexico_City",
                "model": {"provider": "mock", "default": profile.model},
                "display": {"theme": "dark"},
                "memory": {"provider": ""},
                "auxiliary": {
                    "vision": {"model": "mock-vision", "api_key": ""},
                },
            },
            "soul": f"You are the deterministic {profile.display_name} profile.",
            "memory_provider": "",
            "memory_sizes": {"memory": 128, "user": 64},
            "skills": {
                "planning": {
                    "name": "planning",
                    "description": "Deterministic planning skill",
                    "category": "core",
                    "enabled": True,
                },
                "research": {
                    "name": "research",
                    "description": "Deterministic research skill",
                    "category": "core",
                    "enabled": False,
                },
            },
            "toolsets": {
                "web": {
                    "name": "web",
                    "label": "Web",
                    "description": "Deterministic web tools",
                    "enabled": True,
                    "configured": True,
                    "tools": ["web_search", "web_extract"],
                },
                "terminal": {
                    "name": "terminal",
                    "label": "Terminal",
                    "description": "Deterministic terminal tools",
                    "enabled": False,
                    "configured": True,
                    "tools": ["terminal", "process"],
                },
            },
            "mcp": {},
            "channels": {
                "telegram": {
                    "id": "telegram",
                    "name": "Telegram",
                    "enabled": False,
                    "configured": False,
                    "state": "disabled",
                    "env_vars": [
                        {
                            "key": "TELEGRAM_BOT_TOKEN",
                            "required": True,
                            "is_set": False,
                        }
                    ],
                }
            },
            "secrets": {},
        }

    def admin_profile(self, name: str) -> dict[str, Any]:
        try:
            return self.admin_profiles[name]
        except KeyError as exc:
            raise KeyError(f"unknown profile: {name}") from exc

    def _next(self, prefix: str) -> str:
        with self.lock:
            self._counter += 1
            return f"{prefix}-{self._counter:04d}"

    def now(self) -> str:
        value = datetime(2026, 8, 28, tzinfo=UTC) + timedelta(seconds=self._clock_tick)
        self._clock_tick += 1
        return value.isoformat().replace("+00:00", "Z")

    def profile_exists(self, name: str) -> bool:
        return any(profile.name == name for profile in self.PROFILES)

    def profile_payloads(self) -> list[dict[str, Any]]:
        return [asdict(profile) for profile in self.PROFILES]

    def create_session(self, profile: str, title: str = "") -> MockSession:
        if not self.profile_exists(profile):
            raise KeyError(f"unknown profile: {profile}")
        with self.lock:
            stored = self._next("stored")
            runtime = f"{self._counter:08x}"
            timestamp = self.now()
            session = MockSession(
                stored_session_id=stored,
                runtime_session_id=runtime,
                profile=profile,
                title=title.strip() or "Untitled",
                created_at=timestamp,
                updated_at=timestamp,
                events=deque(maxlen=self.replay_buffer_size),
            )
            self.sessions_by_stored[stored] = session
            self.runtime_to_stored[runtime] = stored
            return session

    def session_for_runtime(self, runtime_id: str) -> MockSession:
        with self.lock:
            stored = self.runtime_to_stored.get(runtime_id)
            if stored is None:
                raise KeyError("session not found")
            return self.sessions_by_stored[stored]

    def session_for_stored(self, stored_id: str, profile: str | None = None) -> MockSession:
        with self.lock:
            session = self.sessions_by_stored.get(stored_id)
            if session is None or (profile is not None and session.profile != profile):
                raise KeyError("session not found")
            return session

    def resume_session(self, stored_id: str, profile: str) -> MockSession:
        with self.lock:
            session = self.session_for_stored(stored_id, profile)
            old_runtime = session.runtime_session_id
            self.runtime_to_stored.pop(old_runtime, None)
            runtime_number = int(self._next("runtime").split("-")[-1])
            session.runtime_session_id = f"{runtime_number:08x}"
            session.updated_at = self.now()
            self.runtime_to_stored[session.runtime_session_id] = stored_id
            return session

    def list_sessions(self, profile: str) -> list[MockSession]:
        with self.lock:
            return sorted(
                (
                    session
                    for session in self.sessions_by_stored.values()
                    if session.profile == profile and session.persisted and not session.archived
                ),
                key=lambda session: session.updated_at,
                reverse=True,
            )

    def add_message(self, session: MockSession, role: str, content: str) -> MockMessage:
        with self.lock:
            message = MockMessage(
                id=self._next("message"),
                role=role,
                content=content,
                created_at=self.now(),
            )
            session.messages.append(message)
            session.persisted = True
            session.updated_at = message.created_at
            if session.title == "Untitled" and role == "user":
                session.title = (content.strip() or "Untitled")[:48]
            return message

    def emit(self, session: MockSession, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            session.latest_seq += 1
            frame = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": event_type,
                    "session_id": session.runtime_session_id,
                    "payload": payload,
                    "seq": session.latest_seq,
                },
            }
            session.events.append(frame)
            return frame

    def events_since(self, session: MockSession, last_seen: int) -> dict[str, Any]:
        with self.lock:
            events = [frame for frame in session.events if frame["params"]["seq"] > last_seen]
            oldest = session.events[0]["params"]["seq"] if session.events else session.latest_seq + 1
            truncated = self.faults.force_replay_truncated or (
                session.latest_seq > 0 and last_seen < oldest - 1
            )
            return {
                "events": events,
                "latest_seq": session.latest_seq,
                "truncated": truncated,
                "count": len(events),
                "epoch": self.replay_epoch,
            }

    def bump_epoch(self) -> str:
        with self.lock:
            suffix = int(self.replay_epoch.rsplit("-", 1)[-1]) + 1
            self.replay_epoch = f"mock-epoch-{suffix:04d}"
            for session in self.sessions_by_stored.values():
                session.latest_seq = 0
                session.events.clear()
            return self.replay_epoch

    def session_payload(self, session: MockSession, *, include_messages: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session.runtime_session_id,
            "stored_session_id": session.stored_session_id,
            "title": session.title,
            "profile": session.profile,
            "message_count": len(session.messages),
            "running": session.running,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "info": {
                "model": next(p.model for p in self.PROFILES if p.name == session.profile),
                "profile_name": session.profile,
                "lazy": not session.persisted,
                "desktop_contract": 1,
            },
        }
        if include_messages:
            payload["messages"] = [asdict(message) for message in session.messages]
        if session.pending_approval is not None:
            payload["pending_approval"] = dict(session.pending_approval)
        if session.pending_clarify is not None:
            clarify = dict(session.pending_clarify)
            if session.clarify_answers:
                clarify["answers"] = dict(session.clarify_answers)
            payload["pending_clarify"] = clarify
        return payload

    def list_payload(self, session: MockSession) -> dict[str, Any]:
        preview = session.messages[-1].content[:120] if session.messages else ""
        return {
            "id": session.stored_session_id,
            "title": session.title,
            "preview": preview,
            "started_at": session.created_at,
            "updated_at": session.updated_at,
            "message_count": len(session.messages),
            "source": "mock",
            "profile": session.profile,
        }

    def create_cron_job(self, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.profile_exists(profile):
            raise KeyError(f"unknown profile: {profile}")
        with self.lock:
            job_id = self._next("cron")
            schedule_text = str(payload.get("schedule") or "0 9 * * 1")
            if "T" in schedule_text:
                schedule = {
                    "kind": "once",
                    "run_at": schedule_text,
                    "display": f"once at {schedule_text}",
                }
            else:
                schedule = {
                    "kind": "cron",
                    "expr": schedule_text,
                    "display": schedule_text,
                }
            job = {
                "id": job_id,
                "profile": profile,
                "name": str(payload.get("name") or "Mock job"),
                "schedule": schedule,
                "schedule_display": schedule["display"],
                "prompt": str(payload.get("prompt") or "Mock scheduled prompt"),
                "enabled": True,
                "state": "scheduled",
                "created_at": self.now(),
                "next_run_at": (
                    schedule_text
                    if schedule["kind"] == "once"
                    else "2026-08-31T15:00:00Z"
                ),
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
            }
            self.cron_jobs[job_id] = job
            self.cron_runs[job_id] = []
            return dict(job)

    def create_run(self, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.profile_exists(profile):
            raise KeyError(f"unknown profile: {profile}")
        with self.lock:
            run_id = self._next("run")
            raw_input = payload.get("input", payload.get("prompt", ""))
            if isinstance(raw_input, list):
                text = " ".join(
                    str(item.get("content") or "") if isinstance(item, dict) else str(item)
                    for item in raw_input
                )
            else:
                text = str(raw_input or "")
            awaiting_approval = "[approval]" in text
            events: list[dict[str, Any]] = [
                {"type": "run.started", "run_id": run_id, "profile": profile},
                {"type": "message.delta", "run_id": run_id, "delta": "Respuesta determinista"},
            ]
            run: dict[str, Any] = {
                "id": run_id,
                "object": "run",
                "profile": profile,
                "status": "requires_action" if awaiting_approval else "completed",
                "created_at": self.now(),
                "input": text,
                "output": None if awaiting_approval else f"Respuesta determinista del perfil {profile}: {text}",
                "approval_id": self._next("run-approval") if awaiting_approval else None,
                "events": events,
            }
            if awaiting_approval:
                events.append(
                    {
                        "type": "approval.request",
                        "run_id": run_id,
                        "request_id": run["approval_id"],
                        "command": "mock-safe-command",
                    }
                )
            else:
                events.append({"type": "run.completed", "run_id": run_id, "output": run["output"]})
            self.runs[run_id] = run
            return run

    def set_scenario(self, name: str, enabled: bool = True) -> None:
        aliases = {
            "disconnect": "disconnect_after_submit",
            "unknown-event": "unknown_event",
            "replay-truncated": "force_replay_truncated",
        }
        attribute = aliases.get(name)
        if attribute is None:
            raise KeyError(f"unknown scenario: {name}")
        setattr(self.faults, attribute, enabled)
