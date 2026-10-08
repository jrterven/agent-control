"""Opt-in contract checks against real Hermes code/dependencies, entirely offline.

HERMES_NATIVE_TEST_ROOT=/source HERMES_NATIVE_TEST_PYTHON=/runtime/bin/python
"""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(not os.environ.get("HERMES_NATIVE_TEST_ROOT"), reason="Requires isolated native Hermes runtime")
def test_native_profile_delivery_without_legacy_shim(tmp_path):
    source = Path(os.environ["HERMES_NATIVE_TEST_ROOT"]).resolve()
    python = os.environ["HERMES_NATIVE_TEST_PYTHON"]
    repo = Path(__file__).resolve().parents[2]
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
    environment.update(HOME=str(tmp_path), HERMES_HOME=str(tmp_path / "hermes"),
        HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    script = r'''
import concurrent.futures, os, sys, threading, time
from pathlib import Path
sys.path[:0] = sys.argv[1:]
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from hermes_state import SessionDB
from tools import async_delegation as tasks
home = Path(os.environ['HERMES_HOME'])
homes = {'default': home, 'one': home / 'profiles/one', 'two': home / 'profiles/two'}
for name, directory in homes.items():
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.yaml').write_text('plugins:\n  enabled: []\n')
    token = set_hermes_home_override(directory)
    try:
        db = SessionDB(directory / 'state.db')
        db.create_session(session_id='parent_' + name, source='cli')
        db.close()
        record = {'delegation_id': 'same-id', 'parent_session_id': 'parent_' + name,
                  'session_key': 'parent_' + name, 'dispatched_at': time.time()}
        tasks._persist_dispatch(record)
        tasks._persist_completion({**record, 'type': 'async_delegation', 'status': 'completed'}, {'summary': 'fixture'})
    finally:
        reset_hermes_home_override(token)
from agent_control_connector import hermes_background_plugin as plugin
assert plugin.install_delivery_profile_scope(plugin.NATIVE_SCOPED_SOURCE_SHA) is False
assert 'tui_gateway.server' not in sys.modules
sys.argv.append('serve')  # argv alone never authorizes an early native import
assert plugin.install_delivery_profile_scope(plugin.NATIVE_SCOPED_SOURCE_SHA) is False
sys.argv.pop()
from tui_gateway import server
original = server._notif_dispatch_event
poller = server._notification_poller_loop
assert plugin.install_delivery_profile_scope(plugin.NATIVE_SCOPED_SOURCE_SHA)
assert server._notif_dispatch_event is original  # no wrapper is installed on 0.21.6
assert server._notification_poller_loop is poller
assert plugin.delivery_runtime_mode(True, plugin.NATIVE_SCOPED_SOURCE_SHA) == 'native-tui'
server._notification_poller_loop = lambda *args: None
try:
    plugin.install_delivery_profile_scope(plugin.NATIVE_SCOPED_SOURCE_SHA)
except ValueError:
    pass
else:
    raise AssertionError('A foreign poller replacement must fail closed')
server._notification_poller_loop = poller
submissions = []
lock = threading.Lock()
barrier = threading.Barrier(2)
def submit(*args, **kwargs):
    with lock:
        submissions.append(str(get_hermes_home().resolve()))
    barrier.wait(timeout=10)
server._notif_submit = submit
# Keep the native poller's actual entry/scope and native claim/dispatch/ack;
# replace only its never-ending event loop and model-call boundary.
def one_poll(stop, sid, session):
    event = {'type': 'async_delegation', 'delegation_id': 'same-id', 'parent_session_id': session['session_key']}
    server._notif_dispatch_event(sid, session, event, 'fixture')
    server._notif_dispatch_event(sid, session, event, 'duplicate fixture')
server._notification_poller_scoped_loop = one_poll
def deliver(name):
    session = {'profile_home': str(homes[name]), 'session_key': 'parent_' + name,
               'history_lock': threading.RLock(), 'running': True}
    server._notification_poller_loop(threading.Event(), name, session)
    assert get_hermes_home().resolve() == home.resolve()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    list(executor.map(deliver, ['one', 'two']))
assert sorted(submissions) == sorted(str(homes[name].resolve()) for name in ['one', 'two'])
for name, directory in homes.items():
    token = set_hermes_home_override(directory)
    try:
        row = tasks.get_durable_delegation('same-id')
        assert row['delivery_state'] == ('pending' if name == 'default' else 'delivered'), (name, row)
        assert row['delivery_attempts'] == (0 if name == 'default' else 1), (name, row)
    finally:
        reset_hermes_home_override(token)
# An admission failure releases the same native claim; no completion is acknowledged.
server._notif_submit = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('fixture admission failure'))
def failed_poll(stop, sid, session):
    server._notif_dispatch_event(sid, session, {'type': 'async_delegation', 'delegation_id': 'same-id'}, 'fixture')
server._notification_poller_scoped_loop = failed_poll
deliver('default')
row = tasks.get_durable_delegation('same-id')
assert row['delivery_state'] == 'pending' and row['delivery_attempts'] == 1, row
print('Native 0.21.6 poller verified: profile scope, parallel claims, single acknowledgement, failure release.')
'''
    result = subprocess.run([python, "-I", "-B", "-c", script, str(source), str(repo / "packages/connector")],
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
