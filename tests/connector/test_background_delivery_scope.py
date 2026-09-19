from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import hashlib
from pathlib import Path
import sqlite3
import sys
import threading
import time
import types

import pytest

from agent_control_connector import hermes_background_plugin as plugin


@pytest.fixture
def native(tmp_path, monkeypatch):
    """Native dispatch body + real profile databases, without model inference."""
    root = tmp_path / "runtime"
    (root / "tui_gateway").mkdir(parents=True)
    (root / "tools").mkdir()
    source = (Path(__file__).parents[1] / "fixtures/hermes_939e_delivery.py").read_text()
    native_path = root / "tui_gateway/session_notifications.py"
    native_path.write_text(source)
    (root / "tools/async_delegation.py").write_text("# fake native database helpers\n")
    (root / "hermes_constants.py").write_text("# fake native context variable\n")
    monkeypatch.setattr(plugin, "DELIVERY_SOURCE_HASHES", {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in plugin.DELIVERY_SOURCE_HASHES})
    homes = {name: tmp_path / name for name in ("default", "jarvis", "other")}
    for home in homes.values():
        home.mkdir()
        with sqlite3.connect(home / "state.db") as db:
            db.execute("CREATE TABLE deliveries(id TEXT PRIMARY KEY, state TEXT, claim TEXT, attempts INTEGER)")
            db.execute("INSERT INTO deliveries VALUES('same-id','pending',NULL,0)")
    current = ContextVar("delivery-profile", default=homes["default"])
    constants = types.ModuleType("hermes_constants")
    constants.set_hermes_home_override = current.set
    constants.reset_hermes_home_override = current.reset
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    helpers = types.ModuleType("tools.async_delegation")
    calls = []

    def claim(evt, consumer):
        calls.append(("claim", current.get()))
        with sqlite3.connect(current.get() / "state.db") as db:
            row = db.execute("SELECT state FROM deliveries WHERE id=?", (evt["delegation_id"],)).fetchone()
            if row is None:
                return "legacy"  # Audited native compatibility for an absent ledger row.
            changed = db.execute("UPDATE deliveries SET claim='held',attempts=attempts+1 WHERE id=? AND state='pending' AND claim IS NULL",
                                 (evt["delegation_id"],)).rowcount
        return "held" if changed else None

    def complete(evt, claim):
        calls.append(("complete", current.get()))
        with sqlite3.connect(current.get() / "state.db") as db:
            db.execute("UPDATE deliveries SET state='delivered',claim=NULL WHERE id=? AND claim=?",
                       (evt["delegation_id"], claim))

    def release(evt, claim):
        calls.append(("release", current.get()))
        with sqlite3.connect(current.get() / "state.db") as db:
            db.execute("UPDATE deliveries SET claim=NULL WHERE id=? AND claim=?", (evt["delegation_id"], claim))

    helpers.claim_event_delivery = claim
    helpers.complete_event_delivery = complete
    helpers.release_event_delivery = release
    monkeypatch.setitem(sys.modules, "tools.async_delegation", helpers)
    server = types.ModuleType("tui_gateway.server")
    server.__file__ = str(root / "tui_gateway/server.py")
    server._hermes_home = homes["default"]
    server.time = time
    server._async_delegation_display_metadata = lambda evt: {"delegation_id": evt["delegation_id"]}
    server._notif_submit = lambda *args, **kwargs: calls.append(("submit", current.get()))
    # Exec binds the audited function to the server namespace exactly as Hermes'
    # FunctionType(fn.__code__, vars(server), ...) split-module binding does.
    exec(compile(source, str(native_path), "exec"), vars(server))
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    return types.SimpleNamespace(server=server, helpers=helpers, homes=homes, current=current, calls=calls, path=native_path)


def state(home):
    with sqlite3.connect(home / "state.db") as db:
        return db.execute("SELECT state,claim,attempts FROM deliveries WHERE id='same-id'").fetchone()


def event():
    return {"type": "async_delegation", "delegation_id": "same-id"}


def test_native_unscoped_dispatch_reproduces_wrong_profile_ack(native):
    native.server._notif_dispatch_event("sid", {"profile_home": native.homes["jarvis"]}, event(), "result")
    assert state(native.homes["default"]) == ("delivered", None, 1)
    assert state(native.homes["jarvis"]) == ("pending", None, 0)


def test_shim_preserves_native_delivery_in_correct_profile_and_binds_default(native):
    assert plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    dispatch = native.server._notif_dispatch_event
    assert plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    assert native.server._notif_dispatch_event is dispatch
    token = native.current.set(native.homes["other"])
    try:
        dispatch("sid", {"profile_home": native.homes["jarvis"]}, event(), "result")
        assert native.current.get() == native.homes["other"]
        dispatch("default-sid", {"profile_home": None}, event(), "default result")
        assert native.current.get() == native.homes["other"]
    finally:
        native.current.reset(token)
    assert state(native.homes["jarvis"]) == state(native.homes["default"]) == ("delivered", None, 1)
    assert state(native.homes["other"]) == ("pending", None, 0)
    assert [phase for phase, _ in native.calls] == ["claim", "submit", "complete"] * 2


def test_parallel_profiles_do_not_cross_claims_or_acknowledgements(native):
    assert plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    barrier = threading.Barrier(3)
    native.server._notif_submit = lambda *args, **kwargs: barrier.wait(timeout=3)
    def deliver(name):
        native.server._notif_dispatch_event(name, {"profile_home": native.homes[name]}, event(), "result")
        assert native.current.get() == native.homes["default"]
    with ThreadPoolExecutor(max_workers=3) as workers:
        list(workers.map(deliver, native.homes))
    assert all(state(home) == ("delivered", None, 1) for home in native.homes.values())


def test_failed_native_admission_releases_only_correct_claim_and_restores_scope(native):
    plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    def fail(*args, **kwargs):
        raise RuntimeError("admission failed")
    native.server._notif_submit = fail
    native.server._notif_dispatch_event("sid", {"profile_home": native.homes["jarvis"]}, event(), "result")
    assert native.current.get() == native.homes["default"]
    assert state(native.homes["jarvis"]) == ("pending", None, 1)
    assert state(native.homes["default"]) == ("pending", None, 0)
    assert native.calls == [(phase, native.homes["jarvis"]) for phase in ("claim", "release")]


def test_claim_failure_restores_scope_and_does_not_submit(native):
    plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    native.helpers.claim_event_delivery = lambda *args: (_ for _ in ()).throw(RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError, match="database unavailable"):
        native.server._notif_dispatch_event("sid", {"profile_home": native.homes["jarvis"]}, event(), "result")
    assert native.current.get() == native.homes["default"]
    assert native.calls == []


def test_already_delivered_result_is_not_admitted_again(native):
    plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    for _ in range(2):
        native.server._notif_dispatch_event("sid", {"profile_home": native.homes["jarvis"]}, event(), "result")
    assert state(native.homes["jarvis"]) == ("delivered", None, 1)
    assert [phase for phase, _ in native.calls] == ["claim", "submit", "complete", "claim"]


def test_revision_source_and_third_party_wrappers_fail_closed(native):
    original = native.server._notif_dispatch_event
    with pytest.raises(ValueError, match="revision"):
        plugin.install_delivery_profile_scope("unreviewed")
    native.path.write_text(native.path.read_text() + "# source changed\n")
    with pytest.raises(ValueError, match="source"):
        plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    assert native.server._notif_dispatch_event is original
    native.server._notif_dispatch_event = lambda *args: None
    with pytest.raises(ValueError, match="handler"):
        plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)


def test_without_loaded_tui_server_no_activation_is_claimed(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    assert plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA) is False
    assert plugin.delivery_runtime_mode(False) is None


def test_gateway_delivery_is_distinct_from_uninitialized_serve(monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    monkeypatch.delitem(sys.modules, "tui_gateway.entry", raising=False)
    monkeypatch.setitem(sys.modules, "gateway.run", types.ModuleType("gateway.run"))
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    assert plugin.delivery_runtime_mode(False) == "native-gateway"
    monkeypatch.setitem(sys.modules, "tui_gateway.entry", types.ModuleType("tui_gateway.entry"))
    assert plugin.delivery_runtime_mode(False) is None


def canonical_startup(native, monkeypatch, *, module_name="hermes_cli.main"):
    """Canonical discovery order: the plugin runs before the server import."""
    root = Path(native.server.__file__).parent.parent
    main_path = root / "hermes_cli/main.py"
    main_path.parent.mkdir()
    source = "def _dashboard_prepare_runtime(args=None, headless_backend=True):\n    return discover_plugins()\n"
    main_path.write_text(source)
    Path(native.server.__file__).write_text("# native server imported after discovery\n")
    monkeypatch.setattr(plugin, "DELIVERY_BOOTSTRAP_HASHES", {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in plugin.DELIVERY_BOOTSTRAP_HASHES})
    main = types.ModuleType(module_name)
    main.__file__ = str(main_path)
    if module_name == "__main__":
        main.__spec__ = types.SimpleNamespace(name="hermes_cli.main")
    main.discover_plugins = lambda: plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    exec(compile(source, str(main_path), "exec"), vars(main))
    monkeypatch.delitem(sys.modules, "tui_gateway.server")
    imports = []
    def import_server(name):
        assert name == "tui_gateway.server"
        imports.append(name)
        monkeypatch.setitem(sys.modules, name, native.server)
        return native.server
    monkeypatch.setattr(plugin.importlib, "import_module", import_server)
    return main, imports


@pytest.mark.parametrize("module_name", ["hermes_cli.main", "__main__"])
def test_canonical_serve_discovery_imports_server_before_registration_finishes(native, monkeypatch, module_name):
    main, imports = canonical_startup(native, monkeypatch, module_name=module_name)
    assert main._dashboard_prepare_runtime() is True
    assert imports == ["tui_gateway.server"]
    assert plugin.delivery_runtime_mode(True) == "tui-scoped"
    dispatch = native.server._notif_dispatch_event
    # A later profile registration reuses the same installed native dispatcher.
    assert plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA) is True
    assert native.server._notif_dispatch_event is dispatch and len(imports) == 1
    dispatch("later-profile", {"profile_home": native.homes["jarvis"]}, event(), "result")
    assert state(native.homes["jarvis"]) == ("delivered", None, 1)
    assert state(native.homes["default"]) == ("pending", None, 0)


@pytest.mark.parametrize("relative", ["hermes_cli/main.py", "tui_gateway/server.py"])
def test_changed_canonical_startup_source_is_rejected_before_import(native, monkeypatch, relative):
    main, imports = canonical_startup(native, monkeypatch)
    path = Path(native.server.__file__).parent.parent / relative
    path.write_text(path.read_text() + "# changed source\n")
    with pytest.raises(ValueError, match="source"):
        main._dashboard_prepare_runtime()
    assert imports == [] and "tui_gateway.server" not in sys.modules


def test_unrelated_cli_with_serve_argument_does_not_import_native_server(native, monkeypatch):
    main, imports = canonical_startup(native, monkeypatch)
    monkeypatch.setitem(sys.modules, "hermes_cli.main", main)
    monkeypatch.setattr(sys, "argv", ["hermes", "serve"])
    assert plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA) is False
    assert imports == [] and plugin.delivery_runtime_mode(False) is None


def test_partial_server_after_canonical_import_cannot_claim_activation(native, monkeypatch):
    main, imports = canonical_startup(native, monkeypatch)
    del native.server._notif_dispatch_event
    with pytest.raises(ValueError, match="handler"):
        main._dashboard_prepare_runtime()
    assert imports == ["tui_gateway.server"]
    monkeypatch.setitem(sys.modules, "tui_gateway.server", types.ModuleType("tui_gateway.server"))
    with pytest.raises(ValueError, match="handler"):
        plugin.install_delivery_profile_scope(plugin.AUDITED_SOURCE_SHA)
    assert plugin.delivery_runtime_mode(False) is None
