"""Native per-session policy primitives (stdlib only; Python 3.10+).

Policies are attached to agent instances, never to process-global memory settings.
This module does not advertise a capability until all native storage adapters are
installed and verified by the runtime integration.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
import json
import inspect
import logging
import os
import sys
import contextlib
import base64
import tempfile
from pathlib import Path
import sqlite3
import threading
import time
import types
from uuid import uuid4

MODES = ("memory_read_write", "memory_read_only", "temporary")
LEASE_SECONDS = 300
POLICY_VERSION = 1
PLUGIN_NAME = "agent-control-chat-modes"
PLUGIN_VERSION = "1.2.0"
_current = ContextVar("agent_control_chat_policy", default=None)


@dataclass
class ChatPolicy:
    mode: str
    session_id: str
    deadline: float = field(default_factory=lambda: time.monotonic() + LEASE_SECONDS)
    closed: bool = False
    profile: str = "default"
    children: set = field(default_factory=set)
    cleanup: list = field(default_factory=list)
    lock: object = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError("CHAT_MODE_UNAVAILABLE")

    def require_active(self):
        if self.closed or (self.mode == "temporary" and time.monotonic() >= self.deadline):
            raise RuntimeError("TEMPORARY_CHAT_ENDED")

    def renew(self):
        with self.lock:
            self.require_active()
            self.deadline = time.monotonic() + LEASE_SECONDS

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            cleanup, self.cleanup = self.cleanup, []
        for stop in cleanup:
            try:
                stop()
            except Exception:
                # One failed cancellation cannot retain the other resources.
                pass


@contextmanager
def policy_scope(policy):
    if policy is not None:
        policy.require_active()
    token = _current.set(policy)
    try:
        yield
    finally:
        _current.reset(token)


def current_policy():
    return _current.get()


def _private_upload_directory(*retained_homes):
    """Keep explicit private uploads outside every retained Hermes home.

    Hermes 0.21.6 redirects tempfile's environment and cached default into
    HERMES_HOME/cache/scratch. An explicit, resolved parent is required here;
    resetting the process default would also affect unrelated conversations.
    """
    homes = [Path(home).resolve() for home in retained_homes]
    candidates = []
    with contextlib.suppress(OSError):
        candidates.append(tempfile.gettempdir())
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, ValueError):
            candidates.append(os.confstr("CS_DARWIN_USER_TEMP_DIR"))
    candidates.extend(("/tmp", "/var/tmp"))
    for candidate in candidates:
        if not candidate:
            continue
        parent = Path(candidate).resolve()
        if any(parent.is_relative_to(home) for home in homes):
            continue
        try:
            directory = tempfile.TemporaryDirectory(prefix="agent-control-private-upload-", dir=parent)
        except OSError:
            continue
        if any(Path(directory.name).resolve().is_relative_to(home) for home in homes):
            directory.cleanup()
            continue
        return directory
    raise RuntimeError("TEMPORARY_CHAT_UPLOAD_UNAVAILABLE")


class ReadOnlyMemoryStore:
    """Expose only the native store's reading surface; refuse every mutation."""
    READ_METHODS = frozenset({"load_from_disk", "get_memory", "get_user", "get_all", "format_for_system_prompt", "get_status", "is_enabled", "get_entries", "get_content", "target_enabled", "reset_consolidation_failures"})
    READ_PROPERTIES = frozenset({"memory_enabled", "user_profile_enabled", "memory_char_limit", "user_char_limit", "memory", "user", "memory_content", "user_content", "memory_entries", "user_entries"})

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        if name in self.READ_METHODS:
            return getattr(self._store, name)
        if name in self.READ_PROPERTIES:
            value = getattr(self._store, name)
            # Do not hand out a mutable reference to the live store.
            return json.loads(json.dumps(value)) if isinstance(value, (dict, list)) else value
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: {"success": False, "error": "CHAT_MEMORY_READ_ONLY"}


class VolatileSqlite:
    """Shared SQLite connections whose entire database lives in RAM.

    An anchor keeps the store alive while workers open/close their own handles.
    Closing revokes future connections before releasing the anchor.
    """
    def __init__(self, policy):
        self.policy = policy
        self.uri = "file:ac-chat-" + uuid4().hex + "?mode=memory&cache=shared"
        self._lock = threading.RLock()
        self._connections = set()
        self.closed = False
        self._anchor = self.connect()
        policy.cleanup.append(self.close)

    def connect(self):
        with self._lock:
            self.policy.require_active()
            if self.closed:
                raise RuntimeError("TEMPORARY_CHAT_ENDED")
            owner = self
            class Connection(sqlite3.Connection):
                def close(self):
                    with owner._lock:
                        owner._connections.discard(self)
                    super().close()
            conn = sqlite3.connect(self.uri, uri=True, check_same_thread=False, timeout=10, factory=Connection)
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA max_page_count=32768")
            # Prevent an accidental ATTACH/VACUUM INTO from writing a transcript
            # into a file through a borrowed private connection.
            conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK)
            self._connections.add(conn)
            return conn

    def close(self):
        with self._lock:
            self.closed = True
            for conn in list(self._connections):
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()


def constrain_agent(agent, policy):
    """Apply the policy before a first turn or any child receives user content."""
    policy.require_active()
    agent._agent_control_chat_policy = policy
    if policy.mode == "memory_read_write":
        return
    store = getattr(agent, "_memory_store", None)
    if store is not None and not isinstance(store, ReadOnlyMemoryStore):
        agent._memory_store = ReadOnlyMemoryStore(store)
    # Unreviewed external providers can ingest during their read callbacks. They
    # need an audited read-only adapter; silently disabling recall is not valid.
    if getattr(agent, "_memory_manager", None) is not None:
        raise RuntimeError("CHAT_MEMORY_PROVIDER_UNSUPPORTED")
    agent.skip_background_review = True
    agent._skip_background_review = True
    for name in ("_spawn_background_review", "_spawn_background_review_now", "commit_memory_session", "_sync_external_memory_for_turn"):
        setattr(agent, name, lambda *args, **kwargs: None)
    agent.tools = [tool for tool in getattr(agent, "tools", []) if tool.get("function", {}).get("name") != "memory"]
    if policy.mode == "temporary":
        agent.save_trajectories = False
        agent.verbose_logging = False
        agent._persist_disabled = True
        for name in ("_save_trajectory", "_dump_api_request_debug"):
            setattr(agent, name, lambda *args, **kwargs: None)


def policy_call(policy, fn):
    @wraps(fn)
    def invoke(*args, **kwargs):
        with policy_scope(policy):
            return fn(*args, **kwargs)
    return invoke


class NativePolicyRuntime:
    """Adapter for the pinned Serve runtime. No global memory setting is changed."""
    def __init__(self, server):
        self.server = server
        self.policies = {}
        self.databases = {}
        self.storage = {}
        self.lock = threading.RLock()
        self.ready_modes = []
        self.media_storage = {}
        self.attachment_dirs = {}

    @staticmethod
    def require_route(policy, params):
        if policy and policy.mode == "temporary" and policy.profile != (params.get("profile") or "default"):
            raise RuntimeError("CHAT_ROUTE_MISMATCH")

    def policy(self, identity=None, *, ancestry=False):
        if identity in self.policies:
            return self.policies[identity]
        if isinstance(identity, str) and identity.startswith("ac_ro_"):
            value = ChatPolicy("memory_read_only", identity)
            self.policies[identity] = value
            return value
        if isinstance(identity, str) and identity.startswith("ac_tmp_"):
            raise RuntimeError("TEMPORARY_CHAT_ENDED")
        # Compression continuations retain their native ids and parent linkage.
        # Consult ancestry only for unrecognized ids; never infer a downgrade.
        if ancestry and identity:
            with self.ancestry_reader() as parent_of:
                parent = identity
                for _ in range(100):
                    parent = parent_of(parent)
                    if not parent or parent == identity:
                        break
                    if parent in self.policies or parent.startswith(("ac_ro_", "ac_tmp_")):
                        value = self.policy(parent)
                        self.policies[identity] = value
                        return value
        return None

    @contextmanager
    def ancestry_reader(self):
        if not hasattr(self, "original_acquire"):
            yield lambda identity: None
            return
        db = self.original_acquire()
        try:
            yield lambda identity: (db.get_session(identity) or {}).get("parent_session_id")
        finally:
            self.original_release(db)

    def active_policy(self):
        current = current_policy()
        if current is not None:
            return current
        for key in ("HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
            if value := self.policy(self.session_identity(key)):
                return value
        return None

    @staticmethod
    def session_identity(key):
        from gateway.session_context import get_session_env
        return get_session_env(key, "")

    def available_modes(self, cfg):
        # A separate compute process does not share this adapter's policy or RAM
        # stores. Refuse restricted modes instead of dispatching an unsafe turn.
        if self.server._load_dashboard_process_isolation_config(cfg).get("turn_isolation"):
            return []
        plugins = cfg.get("plugins") or {}
        if ((cfg.get("memory") or {}).get("provider") or PLUGIN_NAME not in (plugins.get("enabled") or [])
                or PLUGIN_NAME in (plugins.get("disabled") or [])
                or ((plugins.get("entries") or {}).get(PLUGIN_NAME) or {}).get("enabled") is False):
            return []
        available = list(self.ready_modes)
        if self.server._effective_terminal_backend() != "local":
            # OS-temporary uploads cannot be mounted into an arbitrary remote
            # terminal backend with the same lifetime guarantees.
            available = [mode for mode in available if mode != "temporary"]
        if "agent-control-media" in (plugins.get("enabled") or []) and "agent-control-media" not in (plugins.get("disabled") or []):
            from hermes_constants import get_hermes_home
            marker = sys.modules.get("agent_control_private_media")
            if str(get_hermes_home().resolve()) not in getattr(marker, "homes", set()):
                available = [mode for mode in available if mode != "temporary"]
        return available

    def install_agents(self):
        from run_agent import AIAgent
        original_init = AIAgent.__init__
        signature = inspect.signature(original_init)
        runtime = self

        @wraps(original_init)
        def initialize(agent, *args, **kwargs):
            bound = signature.bind(agent, *args, **kwargs)
            arguments = bound.arguments
            policy = (runtime.policy(arguments.get("session_id")) or runtime.policy(arguments.get("parent_session_id"))
                      or runtime.active_policy() or runtime.policy(arguments.get("session_id"), ancestry=True))
            if policy:
                policy.require_active()
                from hermes_cli.config import load_config
                if ((load_config() or {}).get("memory") or {}).get("provider"):
                    raise RuntimeError("CHAT_MEMORY_PROVIDER_UNSUPPORTED")
                arguments["skip_background_review"] = True
                if policy.mode == "temporary":
                    arguments.update(session_db=runtime.databases[policy.session_id], save_trajectories=False,
                                     verbose_logging=False, checkpoints_enabled=False)
                    if "persistence" in signature.parameters:
                        arguments["persistence"] = arguments["session_db"]
            with policy_scope(policy):
                original_init(*bound.args, **bound.kwargs)
            if policy:
                runtime.policies[agent.session_id] = policy
                constrain_agent(agent, policy)
                if policy.mode == "temporary":
                    agent._session_db = runtime.databases[policy.session_id]
                    policy.cleanup.append(lambda: agent.interrupt("Temporary chat closed"))
        AIAgent.__init__ = initialize
        original_run = AIAgent.run_conversation

        @wraps(original_run)
        def run(agent, *args, **kwargs):
            policy = getattr(agent, "_agent_control_chat_policy", None) or runtime.policy(agent.session_id)
            with policy_scope(policy):
                return original_run(agent, *args, **kwargs)
        AIAgent.run_conversation = run

    def install_rpc(self):
        server = self.server
        original_key = server._new_session_key

        def new_key():
            policy = current_policy()
            if policy is not None:
                if any(self.policies.get(session.get("session_key")) is policy for session in server._sessions.values()):
                    raise RuntimeError("CHAT_MODE_NEW_CHAT_REQUIRED")
                return policy.session_id
            return original_key()
        server._new_session_key = new_key

        def modes(rid, params):
            # External memory providers need an explicit read-only adapter.
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            available = self.available_modes(cfg)
            return server._ok(rid, {"version": POLICY_VERSION, "modes": available, "activeTemporary": sum(key == policy.session_id and policy.mode == "temporary" and not policy.closed and policy.profile == (params.get("profile") or "default") for key, policy in self.policies.items())})

        original_create = server._methods["session.create"]

        def create(rid, params):
            mode = params.get("chat_mode")
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            if mode not in self.available_modes(cfg):
                return server._err(rid, 4095, "CHAT_MODE_UNAVAILABLE")
            # Restricted policies never accept arbitrary seed histories.
            if params.get("messages"):
                return server._err(rid, 4095, "CHAT_MODE_SEED_UNSUPPORTED")
            if mode == "temporary" and sum(p.mode == "temporary" and not p.closed and key == p.session_id for key, p in self.policies.items()) >= 16:
                return server._err(rid, 4095, "TEMPORARY_CHAT_LIMIT")
            key = ("ac_tmp_" if mode == "temporary" else "ac_ro_") + uuid4().hex
            policy = ChatPolicy(mode, key, profile=params.get("profile") or "default")
            self.policies[key] = policy
            try:
                with policy_scope(policy):
                    if mode == "temporary":
                        self.allocate_storage(policy)
                    response = original_create(rid, {**params, "close_on_disconnect": False})
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("CHAT_MODE_CREATE_FAILED")
                self.policies[result["session_id"]] = policy
                result["chat_mode"] = mode
                return response
            except BaseException:
                policy.close()
                raise

        def renew(rid, params):
            policy = self.policy(params.get("session_id"))
            self.require_route(policy, params)
            if policy is None or policy.mode != "temporary":
                return server._err(rid, 4095, "TEMPORARY_CHAT_ENDED")
            policy.renew()
            return server._ok(rid, {"ok": True})

        def private_history(rid, params):
            policy = self.policy(params.get("stored_session_id"))
            self.require_route(policy, params)
            if policy is None or policy.mode != "temporary":
                return server._err(rid, 4095, "TEMPORARY_CHAT_ENDED")
            with policy_scope(policy):
                for sid, session in list(server._sessions.items()):
                    if self.policies.get(session.get("session_key")) is policy:
                        return server._methods["session.history"](rid, {**params, "session_id": sid})
                return server._err(rid, 4095, "TEMPORARY_CHAT_ENDED")

        def close(rid, params):
            identity = params.get("session_id")
            policy = self.policies.get(identity)
            self.require_route(policy, params)
            if policy is None or policy.mode != "temporary" or policy.closed:
                return server._ok(rid, {"ok": True})
            self.close_policy(policy)
            return server._ok(rid, {"ok": True})

        def media_pending(rid, params):
            for key, storage in list(self.media_storage.items()):
                policy = self.policies[key]
                if policy.closed or time.monotonic() >= policy.deadline or policy.profile != (params.get("profile") or "default"):
                    continue
                with contextlib.closing(storage.connect()) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute("SELECT * FROM images WHERE status='pending' AND next_attempt <= ? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
                    if row:
                        with db:
                            db.execute("UPDATE images SET next_attempt=? WHERE id=?", (time.time() + 10, row["id"]))
                        return server._ok(rid, {"id": row["id"], "sessionId": policy.session_id,
                            "metadata": json.loads(row["metadata"]), "url": row["source_url"],
                            "content": base64.b64encode(row["content"]).decode() if row["content"] else None})
            return server._ok(rid, {})

        def media_ack(rid, params):
            policy = self.policy(params.get("sessionId"))
            self.require_route(policy, params)
            if policy is None or policy.mode != "temporary" or policy.closed:
                return server._ok(rid, {"ok": True})
            storage = self.media_storage.get(policy.session_id)
            if storage and params.get("status") in {"ready", "failed"}:
                with contextlib.closing(storage.connect()) as db, db:
                    db.execute("UPDATE images SET status=?,error_code=?,content=NULL,source_url=NULL WHERE id=?", (params["status"], params.get("errorCode"), params.get("id")))
            return server._ok(rid, {"ok": True})

        def background(rid, params):
            from datetime import datetime, timezone
            policy = self.policy(params.get("stored_session_id"))
            self.require_route(policy, params)
            if policy is None or policy.mode != "temporary":
                return server._err(rid, 4095, "TEMPORARY_CHAT_ENDED")
            stamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
            with policy_scope(policy), contextlib.closing(self.storage[policy.session_id].connect()) as db:
                self.prepare_delegation_store(db)
                rows = db.execute("SELECT delegation_id,state,delivery_state,dispatched_at,updated_at,completed_at FROM async_delegations").fetchall()
            tasks = []
            for identifier, state, delivery, started, updated, ended in rows:
                mapped = "running" if state in {"running", "stalling", "finalizing", "dispatched"} else "completed" if state in {"completed", "success"} else "cancelled" if state in {"cancelled", "interrupted"} else "failed"
                tasks.append({"id": identifier, "storedSessionId": policy.session_id, "state": mapped, "deliveryState": delivery,
                    "title": "Tarea en segundo plano", "createdAt": stamp(started), "updatedAt": stamp(updated),
                    **({"completedAt": stamp(ended)} if ended else {})})
            return server._ok(rid, {"available": True, "complete": True, "tasks": tasks,
                "activeCount": sum(t["state"] == "running" for t in tasks),
                "pendingDeliveryCount": sum(t["state"] != "running" and t["deliveryState"] == "pending" for t in tasks),
                "totalCount": len(tasks), "source": "hermes-native-delegation", "observedAt": stamp(time.time())})

        # Bind every existing RPC to its session before it can touch storage.
        for name, handler in list(server._methods.items()):
            @wraps(handler)
            def scoped(rid, params, _handler=handler, _name=name):
                identity = params.get("session_id") or params.get("stored_session_id") or params.get("session_key")
                policy = self.policy(identity, ancestry=True)
                self.require_route(policy, params)
                with policy_scope(policy):
                    response = _handler(rid, params)
                    if policy and isinstance(response, dict) and isinstance(response.get("result"), dict):
                        response["result"]["chat_mode"] = policy.mode
                    if _name == "session.list" and isinstance(response.get("result"), dict):
                        response["result"]["sessions"] = [row for row in response["result"].get("sessions", [])
                            if not isinstance(row, dict) or not str(row.get("stored_session_id") or row.get("id") or "").startswith("ac_tmp_")]
                        for row in response["result"].get("sessions", []):
                            if isinstance(row, dict):
                                row_policy = self.policy(row.get("stored_session_id") or row.get("id"), ancestry=True)
                                if row_policy:
                                    row["chat_mode"] = row_policy.mode
                    return response
            server._methods[name] = server._profile_scoped(scoped)
        server._methods["control.chat_modes"] = server._profile_scoped(modes)
        server._methods["control.session.create"] = server._profile_scoped(create)
        server._methods["control.session.renew"] = server._profile_scoped(renew)
        server._methods["control.session.close"] = server._profile_scoped(close)
        server._methods["control.session.history"] = server._profile_scoped(private_history)
        server._methods["control.media.pending"] = server._profile_scoped(media_pending)
        server._methods["control.media.ack"] = server._profile_scoped(media_ack)
        server._methods["control.session.background"] = server._profile_scoped(background)

    def install(self):
        self.install_storage()
        self.install_agents()
        self.install_background()
        self.install_logging()
        self.install_rpc()
        self.ready_modes = ["memory_read_only", "temporary"]
        threading.Thread(target=self.reap, daemon=True, name="chat-policy-lease").start()

    def allocate_storage(self, policy):
        from hermes_state import SessionDB
        storage = VolatileSqlite(policy)
        self.storage[policy.session_id] = storage

        class MemorySessionDB(SessionDB):
            def _open_writer(db):
                db._conn = storage.connect()
                db._conn.isolation_level = None
                db._conn.row_factory = sqlite3.Row
                db._init_schema()

            def _record_db_file_identity(db):
                pass

            def _raise_if_db_replaced(db):
                policy.require_active()

            def _halt_if_db_generation_changed(db):
                policy.require_active()

            def _reopen_after_close_locked(db, context="write"):
                raise RuntimeError("TEMPORARY_CHAT_ENDED")

            def close(db):
                # Native borrowers release handles; the lease owns the database.
                pass

        db = MemorySessionDB(Path("/nonexistent-agent-control-memory") / policy.session_id)
        self.databases[policy.session_id] = db
        policy.cleanup.insert(0, db._stop_token_writer)

    def private_database(self):
        policy = self.active_policy()
        if policy and policy.mode == "temporary":
            policy.require_active()
            return self.databases[policy.session_id]
        return None

    def private_media_queue(self):
        policy = self.active_policy()
        if policy and policy.mode == "temporary":
            policy.require_active()
            with self.lock:
                if policy.session_id not in self.media_storage:
                    self.media_storage[policy.session_id] = VolatileSqlite(policy)
                return self.media_storage[policy.session_id].connect()
        return None

    def install_persistence(self):
        import hermes_state_registry as registry
        acquire = registry.acquire
        self.original_acquire = acquire
        self.original_release = registry.release_or_close

        @wraps(acquire)
        def scoped_acquire(*args, **kwargs):
            return self.private_database() or acquire(*args, **kwargs)
        registry.acquire = scoped_acquire

    def install_storage(self):
        self.install_persistence()
        runtime = self
        # Native notification/recovery and lifecycle threads bind a profile before
        # reading their stores. Bind the conversation policy at that same boundary
        # so 0.21.6 orphan sweeps cannot route a temporary task to durable state.db.
        profile_scope = self.server._session_profile_runtime_scope
        @contextlib.contextmanager
        def scoped_profile(session, *args, **kwargs):
            policy = runtime.policy((session or {}).get("session_key"))
            with policy_scope(policy or current_policy()):
                with profile_scope(session, *args, **kwargs):
                    yield
        self.server._session_profile_runtime_scope = scoped_profile
        get_db = self.server._get_db
        self.server._get_db = lambda: runtime.private_database() or get_db()

        run_turn = self.server._run_prompt_submit
        @wraps(run_turn)
        def scoped_turn(rid, sid, session, *args, **kwargs):
            with policy_scope(runtime.policy(session.get("session_key"))):
                return run_turn(rid, sid, session, *args, **kwargs)
        self.server._run_prompt_submit = scoped_turn
        record_marker = self.server._record_turn_marker
        def marker(session, *args, **kwargs):
            policy = runtime.policy(session.get("session_key"))
            if policy and policy.mode == "temporary":
                return policy.session_id
            return record_marker(session, *args, **kwargs)
        self.server._record_turn_marker = marker

        # Native tools need real file paths for explicit user uploads. Stage
        # those files in a session-owned OS temporary directory, never in the
        # profile's retained attachments/images directories.
        home_dir = self.server._session_home_dir
        def attachment_home(session, name):
            policy = runtime.policy(session.get("session_key"))
            if policy and policy.mode == "temporary" and name in {"attachments", "images"}:
                policy.require_active()
                with runtime.lock:
                    if policy.session_id not in runtime.attachment_dirs:
                        directory = _private_upload_directory(
                            runtime.server._hermes_home, home_dir(session, "images").parent,
                        )
                        runtime.attachment_dirs[policy.session_id] = directory
                        policy.cleanup.append(directory.cleanup)
                    return Path(runtime.attachment_dirs[policy.session_id].name) / name
            return home_dir(session, name)
        self.server._session_home_dir = attachment_home
        stage_file = self.server._stage_session_file_attachment
        def private_file(session, *, raw_path, data_url, name):
            policy = runtime.policy(session.get("session_key"))
            if policy and policy.mode == "temporary" and data_url:
                raw_path = ""  # Browser uploads must not alias a host file by name.
            return stage_file(session, raw_path=raw_path, data_url=data_url, name=name)
        self.server._stage_session_file_attachment = private_file

        sync_key = self.server._sync_session_key_after_compress
        def sync_compression(sid, session, **kwargs):
            policy = runtime.policy(session.get("session_key"))
            if policy and policy.mode == "temporary":
                child = getattr(session.get("agent"), "session_id", None)
                if child:
                    runtime.policies[child] = policy
                return  # Keep a stable private route; all child rows stay in RAM.
            return sync_key(sid, session, **kwargs)
        self.server._sync_session_key_after_compress = sync_compression

        # Out-of-turn lifecycle/cwd workers receive the session dictionary rather
        # than the caller context. Establish the policy before obtaining a DB.
        for name in ("_workdir_owner_db", "_session_db", "_finalize_session"):
            original = getattr(self.server, name)
            if name == "_finalize_session":
                @wraps(original)
                def finalize(session, *args, _original=original, **kwargs):
                    policy = runtime.policy((session or {}).get("session_key"))
                    with policy_scope(policy):
                        return _original(session, *args, **kwargs)
                setattr(self.server, name, finalize)
            else:
                @contextlib.contextmanager
                def session_db(session, *args, _original=original, **kwargs):
                    policy = runtime.policy((session or {}).get("session_key"))
                    with policy_scope(policy):
                        with _original(session, *args, **kwargs) as db:
                            yield db
                setattr(self.server, name, session_db)

        # Active-session leases and lifecycle hooks are durable recovery aids.
        # Temporary sessions instead use their independent in-process lease.
        for name in ("_claim_active_session_slot", "_ensure_active_session_slot", "_notify_session_boundary"):
            original = getattr(self.server, name)
            @wraps(original)
            def skip_private(*args, _original=original, **kwargs):
                policy = runtime.active_policy()
                if policy and policy.mode == "temporary":
                    return None
                return _original(*args, **kwargs)
            setattr(self.server, name, skip_private)

        from tools.memory_tool_store import MemoryStore
        mutate = MemoryStore._mutate
        @wraps(mutate)
        def guarded_mutate(store, *args, **kwargs):
            policy = runtime.active_policy()
            if policy and policy.mode != "memory_read_write":
                return {"success": False, "error": "CHAT_MEMORY_READ_ONLY"}
            return mutate(store, *args, **kwargs)
        MemoryStore._mutate = guarded_mutate

        self.install_lineage()

    def install_lineage(self):
        runtime = self
        from hermes_state import SessionDB
        publish_child = SessionDB.publish_compression_child
        @wraps(publish_child)
        def publish(db, *args, **kwargs):
            policy = runtime.policy(kwargs.get("parent_session_id")) or runtime.active_policy()
            if policy and kwargs.get("child_session_id"):
                runtime.policies[kwargs["child_session_id"]] = policy
            with policy_scope(policy):
                return publish_child(db, *args, **kwargs)
        SessionDB.publish_compression_child = publish

    def prepare_delegation_store(self, db):
        from tools.async_delegation import _initialize_schema
        _initialize_schema(db)

    def install_delegation_storage(self, tasks):
        connect = tasks._connect
        def scoped_connect():
            policy = self.active_policy()
            if policy and policy.mode == "temporary":
                connection = self.storage[policy.session_id].connect()
                connection.row_factory = sqlite3.Row
                self.prepare_delegation_store(connection)
                return connection
            return connect()
        tasks._connect = scoped_connect

    def install_background(self):
        from tools import async_delegation as tasks
        self.install_delegation_storage(tasks)
        runtime = self
        # Delivery and monitor threads do not always inherit the dispatch context.
        # Bind by delegation identity before a late completion can reach storage.
        for name in ("_persist_dispatch", "_persist_completion", "record_unit_child", "_finalize",
                     "claim_completion_delivery", "release_completion_delivery", "defer_completion_delivery",
                     "drop_completion_delivery", "complete_completion_delivery", "get_durable_delegation"):
            original = getattr(tasks, name)
            @wraps(original)
            def scoped(*args, _original=original, **kwargs):
                identity = args[0] if args else None
                record = identity if isinstance(identity, dict) else tasks._records.get(identity, {})
                policy = runtime.policy(record.get("parent_session_id")) or runtime.policy(record.get("session_key"))
                if policy and policy.closed:
                    return False
                with policy_scope(policy or runtime.active_policy()):
                    return _original(*args, **kwargs)
            setattr(tasks, name, scoped)

    def install_logging(self):
        runtime = self
        from tools.debug_helpers import DebugSession
        for name in ("log_call", "save"):
            original_debug = getattr(DebugSession, name)
            @wraps(original_debug)
            def debug_call(session, *args, _original=original_debug, **kwargs):
                policy = runtime.active_policy()
                if policy and policy.mode == "temporary":
                    return
                return _original(session, *args, **kwargs)
            setattr(DebugSession, name, debug_call)
        original = logging.Logger.handle
        @wraps(original)
        def handle(logger, record):
            try:
                policy = runtime.active_policy()
                if policy and policy.mode == "temporary":
                    return
            except RuntimeError:
                return  # Late output from an expired private worker.
            return original(logger, record)
        logging.Logger.handle = handle

    def close_policy(self, policy):
        # Interrupt while the private store remains available for native cleanup,
        # then revoke storage and erase the runtime's references in all cases.
        from tools import async_delegation as tasks
        token = _current.set(policy)
        try:
            with contextlib.suppress(Exception):
                tasks.interrupt_for_session(parent_session_id=policy.session_id, reason="temporary_chat_closed")
            sessions = list(self.server._sessions.items())
            for sid, session in sessions:
                if self.policies.get(session.get("session_key")) is policy:
                    with contextlib.suppress(Exception):
                        self.server._close_session_by_id(sid, end_reason="temporary_chat_closed")
        finally:
            policy.close()
            _current.reset(token)
            self.databases.pop(policy.session_id, None)
            self.storage.pop(policy.session_id, None)
            self.media_storage.pop(policy.session_id, None)
            self.attachment_dirs.pop(policy.session_id, None)
            with tasks._records_lock:
                for key, record in list(tasks._records.items()):
                    if self.policies.get(record.get("parent_session_id")) is policy:
                        tasks._records.pop(key, None)
            # Preserve revoked identities (no transcript or agent references)
            # so late background results cannot fall back to a durable DB.

    def reap(self):
        while True:
            time.sleep(15)
            for policy in list({id(value): value for value in self.policies.values()}.values()):
                if policy.mode == "temporary" and not policy.closed and time.monotonic() >= policy.deadline:
                    with contextlib.suppress(Exception):
                        self.close_policy(policy)


class PersistencePolicyRuntime(NativePolicyRuntime):
    """Use Hermes 0.23.4's repositories on a lease-owned SQLite holder."""

    def install_rpc(self):
        server = self.server
        history = server._methods["session.history"]

        @wraps(history)
        def active_history(rid, params):
            policy = current_policy()
            if policy is None or policy.mode != "temporary":
                return history(rid, params)
            session = server._sessions.get(params.get("session_id"))
            if session is None or self.policy(session.get("session_key")) is not policy:
                return server._err(rid, 4095, "TEMPORARY_CHAT_ENDED")
            # Hermes' persisted projection can contain the submitted user row
            # alone because the agent has its durable flushes disabled.
            with session["history_lock"]:
                messages = list(session.get("history", []))
            return server._ok(rid, {"count": len(messages), "messages":
                server._history_to_messages(messages, profile_home=session.get("profile_home"))})
        server._methods["session.history"] = active_history
        super().install_rpc()

    def allocate_storage(self, policy):
        from persistence_assembly import Persistence, profile_scope
        from persistence.sqlite import SQLiteBackend
        from persistence.sqlite._holder import StoreHolder
        from persistence.sqlite._schema import _init_schema

        storage = VolatileSqlite(policy)
        scope = profile_scope()

        class VolatileHolder(StoreHolder):
            def _open_writer(holder):
                holder._conn = storage.connect()
                holder._conn.isolation_level = None
                holder._conn.row_factory = sqlite3.Row
                _init_schema(holder)

            def _ensure_db_file_generation(holder):
                policy.require_active()

            def _raise_if_db_replaced(holder):
                policy.require_active()

            def _halt_if_db_generation_changed(holder):
                policy.require_active()

            def _reopen_after_close_locked(holder, context="write"):
                raise RuntimeError("TEMPORARY_CHAT_ENDED")

        holder = VolatileHolder(Path(tempfile.gettempdir()) / policy.session_id / "state.db")
        backend = SQLiteBackend.lending(scope.hermes_home_key, holder)
        backend.capabilities = replace(backend.capabilities, cross_process=False, reopen_durability=False, physical_admin=False)

        def bind():
            policy.require_active()
            return backend.open(scope)

        persistence = Persistence(scope, read_only=False, bind=bind, release=lambda: None)
        policy.cleanup[:0] = [backend.close, holder.close, persistence.close]
        self.storage[policy.session_id] = storage
        self.databases[policy.session_id] = persistence

    def install_persistence(self):
        import persistence_assembly as assembly
        from gateway import session_context

        open_store = assembly.open_persistence
        self.original_reader = open_reader = assembly.open_persistence_reader
        store_exists = assembly.store_exists
        store_location = assembly.store_location
        publish_session_id = session_context.set_current_session_id

        @wraps(publish_session_id)
        def scoped_session_id(identifier):
            policy = self.policy(identifier) or current_policy()
            if policy and policy.mode == "temporary":
                session_context._SESSION_ID.set(identifier)
            else:
                publish_session_id(identifier)

        def private_handle(scope, *, read_only=False):
            root = self.private_database()
            if root is None:
                return None
            if scope.hermes_home_key != root.scope.hermes_home_key:
                raise RuntimeError("CHAT_ROUTE_MISMATCH")
            # A borrow can close independently. Only lease teardown closes the backend.
            return assembly.Persistence(root.scope, read_only=read_only, bind=root._bound_store, release=lambda: None)

        @wraps(open_store)
        def scoped_store(scope, *, eager=False):
            handle = private_handle(scope)
            if handle is None:
                return open_store(scope, eager=eager)
            if eager:
                handle.ready()
            return handle

        @contextmanager
        def scoped_reader(target, **kwargs):
            if self.private_database() is None:
                with open_reader(target, **kwargs) as db:
                    yield db
                return
            scope = (assembly.profile_scope(home=target.parent if target.name == "state.db" else target)
                     if isinstance(target, Path) else assembly.profile_scope(target))
            with private_handle(scope, read_only=True) as db:
                yield db

        def scoped_exists(scope):
            root = self.private_database()
            return scope.hermes_home_key == root.scope.hermes_home_key if root is not None else store_exists(scope)

        assembly.open_persistence = scoped_store
        assembly.open_persistence_reader = scoped_reader
        assembly.store_exists = scoped_exists
        assembly.store_location = lambda scope: None if self.private_database() is not None else store_location(scope)
        session_context.set_current_session_id = scoped_session_id

    @staticmethod
    def session_identity(key):
        from gateway.session_context import bound_session_env
        return bound_session_env(key)

    @contextmanager
    def ancestry_reader(self):
        from persistence_assembly import profile_scope

        if self.private_database() is not None:
            yield lambda identity: None
            return
        home = profile_scope().profile_dir
        if not (home / "state.db").is_file():
            yield lambda identity: None
            return
        with self.original_reader(home) as db:
            def parent_of(identity):
                row = db.sessions.get(identity)
                return row.parent_session_id if row else None
            yield parent_of

    def install_lineage(self):
        from persistence.sqlite.conversation.lineage import SQLiteLineageRepository
        publish_child = SQLiteLineageRepository.publish_compression_child

        @wraps(publish_child)
        def publish(repository, parent_session_id, child, *args, **kwargs):
            policy = self.policy(parent_session_id) or self.active_policy()
            with policy_scope(policy):
                result = publish_child(repository, parent_session_id, child, *args, **kwargs)
                if policy:
                    self.policies[child.session_id] = policy
                return result
        SQLiteLineageRepository.publish_compression_child = publish

    def prepare_delegation_store(self, db):
        # The native state schema already creates async_delegations.
        pass

    def install_delegation_storage(self, tasks):
        from tools import delegate_tool_resumable
        target_for = delegate_tool_resumable._db_path_for

        @wraps(target_for)
        def resumable_target(parent_agent=None):
            # Resume and worktree pinning deliberately outlive a native run.
            # The live parent and its async delivery remain available in RAM.
            policy = getattr(parent_agent, "_agent_control_chat_policy", None) or self.active_policy()
            if policy and policy.mode == "temporary":
                return None
            return target_for(parent_agent)
        delegate_tool_resumable._db_path_for = resumable_target


def register(ctx):
    """Load only in the audited Serve process, before accepting RPC traffic."""
    import hashlib
    import importlib
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        # Registration moved into a deadline worker in 0.21.6; verify the
        # live canonical startup frame that is waiting for that worker.
        frame = sys._current_frames().get(threading.main_thread().ident)
        try:
            while frame is not None:
                prepare = frame.f_globals.get("_dashboard_prepare_runtime")
                if (frame.f_globals.get("__name__") in {"hermes_cli.main", "__main__"}
                        and isinstance(prepare, types.FunctionType) and frame.f_code is prepare.__code__
                        and prepare.__globals__ is frame.f_globals):
                    root = Path(frame.f_globals["__file__"]).resolve().parent.parent
                    hashes = audited_source_hashes(root)
                    if (Path(frame.f_code.co_filename).resolve() != root / "hermes_cli/main.py"
                            or any(hashlib.sha256((root / name).read_bytes()).hexdigest() != hashes[name]
                                for name in ("hermes_cli/main.py", "hermes_cli/plugins.py", "hermes_cli/plugins_loader.py")
                                if name in hashes)):
                        raise ValueError("Unsupported chat policy runtime")
                    server = importlib.import_module("tui_gateway.server")
                    break
                frame = frame.f_back
        finally:
            del frame
    if server is None:
        return  # CLI/cron do not use Control's chat modes.
    root = Path(server.__file__).resolve().parent.parent
    hashes = audited_source_hashes(root)
    for relative, digest in hashes.items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("Unsupported chat policy runtime")
    runtime = getattr(server, "_agent_control_chat_policy", None)
    if runtime is None:
        adapter = PersistencePolicyRuntime if hashes is AUDITED_0234_HASHES else NativePolicyRuntime
        runtime = adapter(server)
        runtime.install()
        server._agent_control_chat_policy = runtime
        sys.modules["agent_control_chat_policy"] = sys.modules[__name__]
    global active_runtime
    active_runtime = runtime
    def instructions(**kwargs):
        policy = runtime.active_policy()
        if policy is None:
            return None
        return {"context": "This conversation can read existing memory, but MUST NOT save, modify, or consolidate memories, user profiles, or memory files through any tool. Do not schedule memory review. " + ("This is a temporary conversation; do not save its transcript or create conversation logs. Delegated workers inherit these limits and end when the conversation closes." if policy.mode == "temporary" else "Conversation history is retained normally.")}
    ctx.register_hook("pre_llm_call", instructions)


AUDITED_HASHES = {'tui_gateway/server.py': 'ba5e2ee2271acc9cd50aa7aeb0244d9b9f3d0fca8a2a71b4812c451f5d65ccb8',
 'tui_gateway/methods_session.py': '8e7f04f37b499d94360ffafb28ee06182dec09a042df985cfe8768e1a88931ed',
 'tui_gateway/session_workdir.py': '1a1a836aa3ddebe4ff4c90b7c8e2793e17e2ffc79c09ebd6366691512a9205aa',
 'tui_gateway/session_lifecycle.py': '7975bc8474d6d9257a781bf85ade044668d224dfe8ce9d012ab4edf52c3e06c6',
 'tui_gateway/session_notifications.py': 'f71b9dac8722e74cbf2fda89fd49263a1ef89df468f13ef4dbc79888b7f25cfa',
 'hermes_state.py': 'c52560398cd5d3e284724dd6bda1cb72e8cb514b937a40ee32d7a8e2ebcd287a',
 'hermes_state_sessions.py': 'b9a1695037c1d484358cc41487b375b73bbca0a7c7473d45cb229f75b027fe95',
 'hermes_state_registry.py': '070572a17c02bc345a029f4fab5af7539da352045f2ff66394586dbd686d83ee',
 'run_agent.py': '11cc45fad98fd6e345f1895c54a8ff2277b46f89ea10a5c95a47a221d117b3d6',
 'agent/agent_init.py': '67f9aef648bafc50a01c7974daf5d618f2fa53d1255c96b9973e4b185ea4cbb4',
 'agent/session_persistence.py': '83ee5c9f48a7790deb3f771e45990180e1fd714c7b1fabc24303944f17773a1b',
 'agent/conversation_compression.py': '8ff702d95611b7bb8a90e72e28bde29ba65f157faa0d0a4b16b2659a12b868c0',
 'tools/async_delegation.py': 'a837e0c5decadfb304d76cc8fe23c2c8bb56275ec38645125eaf1322f75db0a0',
 'tools/memory_tool_store.py': '5d25c0604de0c4bf4b4f07402b0582b20c33377330a0c7354e059a7e7cf52293',
 'hermes_cli/main.py': 'dc7a6eda3caebab994e8dd2c05a4a8c8b2331f4472313c675efc9e7305b5d6aa',
 'hermes_state_schema.py': '101fbdd82b7a3aff48e628f245cafe1955b61eabb54c29f6762bbebf3171ca9a',
 'tui_gateway/prompt_turn.py': 'fefc29c4bf377ec82ab337718f0eb7e3589e0b9479f3dba58da60074d19329e6',
 'tui_gateway/prompt_attachments.py': '8f972e14760f5f78b7c867fb05956ccea86596c884add2c1032804b01c634101',
 'tui_gateway/methods_prompt.py': 'a226699252cfc7888e7afb7fafdee4c861652ac765b9dc3c2927e135cffe9257',
 'tui_gateway/session_compression.py': 'ed75df639c7ac2d4c6d84b88e86419a1379bfc23b00f2646b835155fba8e553a',
 'tui_gateway/compute_host_bridge.py': 'ac6ea7195f6fb84add1ea1cf7cbae5d68a0025144b454787cda13a067d932b27',
 'tools/thread_context.py': '3d9e921fed5fedf8f6cccbd7e8ef9b12659b4c8f6872fcda27ddf81632ee1d12',
 'tools/debug_helpers.py': '63c650faca9745527dd0ca0bd89b599225401cecdd7d8e45901b075353a00f50'}


AUDITED_0216_HASHES = {'tui_gateway/server.py': '56a7b9ff796add6788f1632ca80d611e1d4253465c56984dabad1cb736943df2',
 'hermes_cli/plugins.py': 'b0f09d0004890ff0b2c07d17ee7c5688709162da4fd43d404cabbe13f1d9f59d',
 'hermes_cli/plugins_loader.py': '072fe348e2c2a5cfbda59ea09620a68b53a1a0b3c43ac74229a500a9b5c696d1',
 'tui_gateway/methods_session.py': '14fb98938c0782e003fbd2277f672d108299cac46c351258c145a5d42b121a1c',
 'tui_gateway/session_workdir.py': '8a3e8ea9382f69105d3b58d265e226544372e5b6672a0c9f105f22b77f54ebd1',
 'tui_gateway/session_lifecycle.py': '22ba865ee54b569be7285439075bdb604dffa915b827844558b1bc1261e9d776',
 'tui_gateway/session_notifications.py': '2823afc052e44072bc8f3c7e326579ae4422fa07ad382ca96544d33a131a6fe2',
 'hermes_state.py': '968cd869f077d490a59b5f624958375fe455d6024e67cbc96b4ecf49981f3828',
 'hermes_state_sessions.py': 'f1bfa82b4010b054001a8344f1b00a6b25694ae4cd439f8c8355f8a74f0fe084',
 'hermes_state_registry.py': '9b906dbb53f34aa8bfee71c929d34534fa7f6bd34ad912056858fce3244417ba',
 'run_agent.py': '73efa221f42be96a41a6b8a1ee144caf47041e06f5c7d32b3bbd898d8dc250b2',
 'agent/agent_init.py': '6e10efdbedc52a9204538a66eeacd320efb6add23a784a45f28fb70e5a5aa8f3',
 'agent/session_persistence.py': '9c6f6bb2e0e785ee53499cbb0997a0e628f91991abd4c2cfb0d51a3afe5d2f4e',
 'agent/conversation_compression.py': 'd604d8fa50044c54822812d9c124911500dff60d720582ea309d0bfacb8a5fc6',
 'tools/async_delegation.py': '5dadd04c99e474e007a7072ee8108747764b26452d417b5ccbd764d29438bc21',
 'tools/memory_tool_store.py': '24106f7191447aa140376b03393890c7b0424c815a829d8baf3ebf9471d50145',
 'hermes_cli/main.py': '1491ff17d20b54c024c052003d394798cb2e673c8b9565b4a8be4c1fd6b21dae',
 'hermes_state_schema.py': '971211b571207767d72a09a495960bb366e2d63eb51a076732239e77eeb1491a',
 'tui_gateway/prompt_turn.py': '11cdd2bd0eaae1e044582c25ef56ee74982c851c5a8cadde0d5cb3961ffa5865',
 'tui_gateway/prompt_attachments.py': '79b8dd4ee5b9460b5ca2de072e1c74fc826a6636bc26a1f27842feaf9e29cc20',
 'tui_gateway/methods_prompt.py': '97fdb44c92d793ea419119bb93ea9818d8b91813a3f2e5965966d98fb024faf9',
 'tui_gateway/session_compression.py': '90aaa5d9fde830113c07ecea42f65770fede64cba5f5f6b224c5a6236b8b6f3f',
 'tui_gateway/compute_host_bridge.py': '859d3be1744330b31b3aead6a62c2d56ebf06434952c04b974dd8c73783e337b',
 'tools/thread_context.py': '3d9e921fed5fedf8f6cccbd7e8ef9b12659b4c8f6872fcda27ddf81632ee1d12',
 'tools/debug_helpers.py': '63c650faca9745527dd0ca0bd89b599225401cecdd7d8e45901b075353a00f50',
 'tui_gateway/model_switch.py': '7f3cb0dc7afcf6b3469acb7ede218c74f8c4744cf47d1c2d2e97120eeeeeae97',
 'hermes_state_compression.py': 'd42c383abfb252b0065bc1fe861fdedb51d235db6dc72a449b20179c61674013',
 'hermes_state_messages.py': '52d4f334ac3ee649c7780274161889fabe463137f34309ee3f3ad1b4cbaf8c1e',
 'hermes_state_readpool.py': '191aed84a6bad0102b8b5f829f74a21d32027c7a26f7a9bd91ca86c359e623b6',
 'hermes_state_dbfile.py': 'd1cd1b0fcf40375599e69fdcdcd3bb1257f659d0948ca168afa3747871130a9e',
 'tools/process_registry.py': '4dacd3f6753d7b81e4ab338f7612624ec1f11c074cc969db49024a967a3b625e',
 'tools/process_registry_notifications.py': 'fa7219e7df360e95170cf1be6e132beb61b61f94a32da50d79a2bcf51e672799'}


AUDITED_0234_HASHES = {'tui_gateway/server.py': 'c2fdb2471d0428339928bb2a936ce01cab262fb6d5700e1ef4477f402a0b875a',
 'hermes_cli/plugins.py': 'd83cc89fbdfca460dba25960f52d2498488ee7e34d32b7470a63e7986ac114e6',
 'hermes_cli/plugins_loader.py': '3400cc514c8a0f0749e02b309020cc48794eae530adc26c0ad3f2381ba6935fc',
 'tui_gateway/methods_session.py': 'e68c42d49fd6bb75703e96415afc00acab1b90fe25e3c571ed03fcfb73217f03',
 'tui_gateway/session_workdir.py': '2c1e4bd3ef411aac19373fbeb3e00e9587342a14fe473710f99cb007d7154d30',
 'tui_gateway/session_lifecycle.py': '8f8f6008db9927efaf95b0f9d378618f9deb7305e57b3fff50aa5c925d4ff303',
 'tui_gateway/session_notifications.py': '9e4b2d1e8986af881fce4a6ff368ee14f49a5ce8e461942c407972cd3aa53e5c',
 'hermes_state.py': '55373fae961fd8b1d928d0e6dfe0fd5d9ad5588ae3e154b432f9ab9440c003e9',
 'run_agent.py': '21dfe95ddcd021d0ac5fc2b0306588558b441f91d2eb926aa908bf5af4b8d722',
 'agent/agent_init.py': '916d8e0c8d774e456f9cb2c3f3626893b1189c4e062041384af50a79aad5d813',
 'agent/session_persistence.py': '18440f06503b35f9f03752b1a4c5bad8536849ca1d2e24c3669260fb2f585ff7',
 'agent/conversation_compression.py': '7cbcf8345e3b51f27f8641dbaca02d44fae6c9d81f32c9f965bbdf8be11cf6e9',
 'tools/async_delegation.py': '5ae95dc34a4f264523450b3f0ad67fb2d827cf22d2d2c4f3bf418a798c044e51',
 'tools/memory_tool_store.py': '502c85f10f6d12f2f92e511670882602988c7f43476ffd74e376c0daa37a3032',
 'hermes_cli/main.py': 'a48175e492ca870ca49b997aec017970a131381fbfb43c08297d7984e4ca8058',
 'tui_gateway/prompt_turn.py': '55578951277e71db35e8743dd1020945751da64dc532daee49acac70b736c997',
 'tui_gateway/prompt_attachments.py': 'c521ffb4b0bd79e05515f00c07f70299de8aede4900e4ed4f4da6a0d87ba04ef',
 'tui_gateway/methods_prompt.py': '3e700b418a6e22f5036f936aa99989f251ab021060aaede2ba47f39df21d335c',
 'tui_gateway/session_compression.py': 'a64a89ebd56f742065912b003d5ea2ad6c3205b58a8c8c324ed33c4a672e1aa5',
 'tui_gateway/compute_host_bridge.py': '58ef6ed7f72ce731f2bfe55b7a52f41986ba694551cb38fdebb1d5b10377a514',
 'tools/thread_context.py': '308fd053f0b85f050492083cef40df942b23f0781a733da54e06cc981fc794d0',
 'tools/debug_helpers.py': '24d7ea3fc0162160d503e52f6602b2598fcfdd70a7a52fdfccfdb397cf02eb80',
 'tui_gateway/model_switch.py': 'cf12e9e5b00b76507b369495c993eb323f6d845b0bca088ca36612a5a737e300',
 'tools/process_registry.py': 'b0856e484a763a6f12f6026d63bda9fb6a41d8641b28d5665d763fff8a2228e2',
 'tools/process_registry_notifications.py': 'b3095d22b5c9914dd1d91adbfff7b97ec557bfb620bc861f6c40e0485a4fd774',
 'agent/agent_init_session.py': '610a95d281838ceb628148ca1de890c1f84a60ce69c5f36ff77ac2430548d358',
 'persistence_assembly.py': 'c8e2502953355e25f0aa8b51da02d609c60f436e8aefd204f2b343849fc7d1c1',
 'persistence/sqlite/_backend.py': 'cfd0dcad6a30703992f73f8e783fdf618f5d5df22c1ec777a2e651176fcebfb1',
 'persistence/sqlite/_holder.py': '1f6db09c4fffdc5fc04069acb480a726140941a7c02bdce878b4e8e2a0463b7d',
 'persistence/sqlite/_schema.py': '0d6425e4d260895c66b7e505cc938a690ab9eff2e106c87194257b316babec6c',
 'persistence/sqlite/conversation/lineage.py': '8615c3c4d9c6810bf13328efa4ed35a4945df0f3a10b91f01aed83973366ed1f',
 'persistence/sqlite/conversation/_store/compression.py': '2b1ed7758589443892ba59b490c126497a025deca3a08034be65240e43ce202d',
 'persistence/sqlite/authority/coordination.py': '9a8022eccf321603f44328201c862cb2ab57f7f712b0a9d0daf6dea425fe70c2',
 'persistence/sqlite/authority/subagents.py': 'ccdb51551f53deced469ff46ee97e5bb4d4988c87ffd3ffa75fb293e448643ca',
 'tui_gateway/store_handles.py': '42c29b17d90a7b9a2564288cd68d80ff9ca8cdef156b76054d9700715058cd11',
 'tools/delegate_tool.py': '8a11dcfbf92af95cdd6df259913c6607c7670ad04d8e33b332c1fc0785bd8c0e',
 'tools/delegate_tool_resumable.py': 'e1d46e800e5859d6dc51fa8a3d6ca2464733bebfb034e867698f4a020f31bebe',
 'tools/delegate_tool_auto_resume.py': 'aa020431ef3301907bcef910b14379b9b4ad5cc37e5aaacb8778df9f6ec83524',
 'gateway/session_context.py': '58e15a377a3b639ce85b9588f3e74b55888d9f36d5473a231479f33a6c03b6b2',
 'persistence/sqlite/_schema_ddl.py': 'ec75667daac0e0b962f67bc43b3a25810c6bb4eda9c7714cca86555c0b050ab3'}


def audited_source_hashes(root):
    import hashlib
    digest = hashlib.sha256((root / "tui_gateway/server.py").read_bytes()).hexdigest()
    for hashes in (AUDITED_HASHES, AUDITED_0216_HASHES, AUDITED_0234_HASHES):
        if hashes["tui_gateway/server.py"] == digest:
            return hashes
    raise ValueError("Unsupported chat policy runtime")
