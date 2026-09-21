"""Native per-session policy primitives (stdlib only; Python 3.10+).

Policies are attached to agent instances, never to process-global memory settings.
This module does not advertise a capability until all native storage adapters are
installed and verified by the runtime integration.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import json
import inspect
import logging
import sys
import contextlib
import base64
import tempfile
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

MODES = ("memory_read_write", "memory_read_only", "temporary")
LEASE_SECONDS = 300
POLICY_VERSION = 1
PLUGIN_NAME = "agent-control-chat-modes"
PLUGIN_VERSION = "1.0.0"
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
        if ancestry and identity and hasattr(self, "original_acquire"):
            db = self.original_acquire()
            try:
                parent = identity
                for _ in range(100):
                    row = db.get_session(parent)
                    parent = (row or {}).get("parent_session_id")
                    if not parent or parent == identity:
                        break
                    if parent in self.policies or parent.startswith(("ac_ro_", "ac_tmp_")):
                        value = self.policy(parent)
                        self.policies[identity] = value
                        return value
            finally:
                self.original_release(db)
        return None

    def active_policy(self):
        current = current_policy()
        if current is not None:
            return current
        from gateway.session_context import get_session_env
        for key in ("HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
            if value := self.policy(get_session_env(key, "")):
                return value
        return None

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
            policy = runtime.policy(arguments.get("session_id"), ancestry=True) or runtime.policy(arguments.get("parent_session_id")) or runtime.active_policy()
            if policy:
                policy.require_active()
                from hermes_cli.config import load_config
                if ((load_config() or {}).get("memory") or {}).get("provider"):
                    raise RuntimeError("CHAT_MEMORY_PROVIDER_UNSUPPORTED")
                arguments["skip_background_review"] = True
                if policy.mode == "temporary":
                    arguments.update(session_db=runtime.databases[policy.session_id], save_trajectories=False,
                                     verbose_logging=False, checkpoints_enabled=False)
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
                from tools.async_delegation import _initialize_schema
                _initialize_schema(db)
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

    def install_storage(self):
        import hermes_state_registry as registry
        runtime = self
        acquire = registry.acquire
        self.original_acquire = acquire
        self.original_release = registry.release_or_close

        @wraps(acquire)
        def scoped_acquire(*args, **kwargs):
            return runtime.private_database() or acquire(*args, **kwargs)
        registry.acquire = scoped_acquire
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
                        directory = tempfile.TemporaryDirectory(prefix="agent-control-private-upload-")
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

    def install_background(self):
        from tools import async_delegation as tasks
        connect = tasks._connect
        runtime = self
        def scoped_connect():
            policy = runtime.active_policy()
            if policy and policy.mode == "temporary":
                connection = runtime.storage[policy.session_id].connect()
                connection.row_factory = sqlite3.Row
                tasks._initialize_schema(connection)
                return connection
            return connect()
        tasks._connect = scoped_connect
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


def register(ctx):
    """Load only in the audited Serve process, before accepting RPC traffic."""
    import hashlib
    import importlib
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        frame = sys._getframe(1)
        try:
            while frame is not None:
                if frame.f_code.co_name == "_dashboard_prepare_runtime" and frame.f_globals.get("__name__") in {"hermes_cli.main", "__main__"}:
                    root = Path(frame.f_globals["__file__"]).resolve().parent.parent
                    if hashlib.sha256((root / "tui_gateway/server.py").read_bytes()).hexdigest() != AUDITED_HASHES["tui_gateway/server.py"]:
                        raise ValueError("Unsupported chat policy runtime")
                    server = importlib.import_module("tui_gateway.server")
                    break
                frame = frame.f_back
        finally:
            del frame
    if server is None:
        return  # CLI/cron do not use Control's chat modes.
    root = Path(server.__file__).resolve().parent.parent
    for relative, digest in AUDITED_HASHES.items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("Unsupported chat policy runtime")
    runtime = getattr(server, "_agent_control_chat_policy", None)
    if runtime is None:
        runtime = NativePolicyRuntime(server)
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
