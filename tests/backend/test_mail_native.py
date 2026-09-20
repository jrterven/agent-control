"""Opt-in test against the pinned, unmodified Hermes source and its MCP SDK.

HERMES_MAIL_TEST_RUNTIME=/absolute/agent-control-runtime
HERMES_MAIL_TEST_SDK=/optional/directory/containing/the/pinned/mcp/extra
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from .test_mail_plugins import mail, connect, grant


@pytest.mark.skipif(not os.environ.get("HERMES_MAIL_TEST_RUNTIME"), reason="Requires pinned Hermes runtime")
def test_pinned_native_hermes_discovery_profile_isolation_and_existing_mcp(mail, app, tmp_path):
    runtime = Path(os.environ["HERMES_MAIL_TEST_RUNTIME"]).resolve()
    provenance = json.loads((runtime / "build-provenance.json").read_text())
    assert "939e45c91d751fadd94dcd1b873ac3cb44846213" in json.dumps(provenance)
    first = connect(mail)
    second = connect(mail, "two@example.com")
    tokens = [grant(mail, app, first), grant(mail, app, second, 1)]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.02)
    assert server.started
    environment = {**os.environ, "HERMES_HOME": str(tmp_path / "hermes"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(filter(None, [os.environ.get("HERMES_MAIL_TEST_SDK"), str(runtime / "hermes"), str(runtime / "connector")]))}
    script = r'''
import asyncio, json, os, sys
from pathlib import Path
from hermes_cli.profiles import get_profile_dir
from hermes_cli.web_routers.mcp import add_mcp_server, list_mcp_servers, remove_mcp_server, test_mcp_server
from hermes_cli.web_models import MCPServerCreate
from hermes_cli.web_server_profiles import _config_profile_scope
from hermes_cli.mcp_config import _get_mcp_servers, _resolve_mcp_server_config
from tools.mcp_tool_discovery import _connect_server

payload = json.load(sys.stdin)
async def exercise():
    for index, profile in enumerate(('mail-one', 'mail-two')):
        home = get_profile_dir(profile)
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('mcp_servers: {}\n')
        # A pre-existing MCP, skill and unrelated secret must survive every operation.
        (home / 'skills').mkdir()
        (home / 'skills/existing.md').write_text('existing skill')
        (home / '.env').write_text('EXISTING_SECRET=untouched\n')
        await add_mcp_server(MCPServerCreate(name='existing', url='https://existing.example/mcp'), profile=profile)
        await add_mcp_server(MCPServerCreate(name='agent_control_mail_test', url=payload['url'], auth='header', bearer_token=payload['tokens'][index]), profile=profile)
        result = await test_mcp_server('agent_control_mail_test', profile=profile)
        assert result['ok'], result
        assert {t['name'] for t in result['tools']} == {'mail_accounts','mail_search','mail_read','mail_send'}
        with _config_profile_scope(profile):
            config = _resolve_mcp_server_config(_get_mcp_servers()['agent_control_mail_test'])
            server = await _connect_server('agent_control_mail_test', config)
            try:
                result = await server.session.call_tool('mail_accounts', arguments={})
                rows = json.loads(result.content[0].text)
                assert [r['accountId'] for r in rows] == [payload['ids'][index]], rows
            finally:
                await server.shutdown()
        await remove_mcp_server('agent_control_mail_test', profile=profile)
        assert [s['name'] for s in (await list_mcp_servers(profile=profile))['servers']] == ['existing']
        assert (home / 'skills/existing.md').read_text() == 'existing skill'
        assert 'EXISTING_SECRET=untouched' in (home / '.env').read_text()
    print('Native Hermes 0.21.2: discovery, tool call, profile isolation, preservation passed')
asyncio.run(exercise())
'''
    try:
        result = subprocess.run([str(runtime / "python/bin/python3"), "-B", "-c", script], input=json.dumps({
            "url": f"http://127.0.0.1:{port}/api/v1/mail/mcp", "tokens": tokens, "ids": [first["id"], second["id"]]}),
            env=environment, cwd=tmp_path, text=True, capture_output=True, timeout=90)
        output = result.stdout + result.stderr
        for token in tokens:
            output = output.replace(token, "[REDACTED]")
        assert result.returncode == 0, output
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
