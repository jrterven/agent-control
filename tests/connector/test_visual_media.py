import ast
import asyncio
from contextlib import closing
import io
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest
import yaml

from agent_control_connector import hermes_media_plugin as plugin
from agent_control_connector.media_install import install_profile, media_profiles, probe_profile
from agent_control_connector.visual_media import (
    MediaError, acknowledge, fetch_image, next_publication, normalize_image, profile_home, public_addresses,
)
from hermes_client.compatibility import HERMES_0212_SHA


def image_bytes(format="PNG", size=(12, 20), **kwargs):
    out = io.BytesIO()
    Image.new("RGB", size, color="red").save(out, format=format, **kwargs)
    return out.getvalue()


def start_turn(home, session="session", turn="turn"):
    home.mkdir(parents=True, exist_ok=True)
    with closing(plugin.open_queue(home)) as db, db:
        db.execute("INSERT OR REPLACE INTO turns(session_id,turn_id,updated_at) VALUES(?,?,?)", (session, turn, time.time()))


def enqueue(home, *, session="session", path=None, url=None, count=1):
    if path is None and url is None:
        path = home / "chart.png"
        path.write_bytes(image_bytes())
    image = {"alt": "Chart", "provenance": "web" if url else "generated", **({"url": url} if url else {"path": str(path)})}
    return plugin.enqueue_images(home, session, [image.copy() for _ in range(count)])


@pytest.mark.parametrize("version", [(3, 10), (3, 11)])
def test_native_plugin_supports_existing_hermes_python_versions(version):
    path = Path(plugin.__file__)
    source = path.read_text(encoding="utf-8")
    syntax = ast.parse(source, filename=str(path), feature_version=version)
    # feature_version is best-effort: Python 3.12+ still accepts PEP 701
    # backslashes inside f-string expressions when parsing an older grammar.
    for node in ast.walk(syntax):
        if isinstance(node, ast.FormattedValue):
            assert "\\" not in ast.get_source_segment(source, node.value)
    compile(syntax, str(path), "exec")


@pytest.mark.parametrize("format,media_type", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/png")])
def test_normalization_has_real_mime_and_thumbnail(format, media_type):
    content, thumbnail, metadata = normalize_image(image_bytes(format))
    assert metadata == {"width": 12, "height": 20, "mediaType": media_type, "thumbnailMediaType": "image/webp"}
    assert Image.open(io.BytesIO(content)).size == (12, 20)
    assert Image.open(io.BytesIO(thumbnail)).format == "WEBP"


def test_orientation_is_applied_and_metadata_removed():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "Private location"
    content, _, metadata = normalize_image(image_bytes("JPEG", exif=exif))
    assert (metadata["width"], metadata["height"]) == (20, 12)
    assert not Image.open(io.BytesIO(content)).getexif()
    assert b"Private location" not in content


@pytest.mark.parametrize("content", [b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"<html>photo</html>", b"not a photo", b"", image_bytes("GIF")])
def test_only_raster_content_accepted(content):
    with pytest.raises(MediaError):
        normalize_image(content)


def test_pixel_and_byte_limits(monkeypatch):
    monkeypatch.setattr("agent_control_connector.visual_media.MAX_PIXELS", 100)
    with pytest.raises(MediaError, match="MEDIA_PIXEL_LIMIT"):
        normalize_image(image_bytes(size=(11, 10)))
    monkeypatch.setattr("agent_control_connector.visual_media.MAX_BYTES", 10)
    with pytest.raises(MediaError, match="MEDIA_FILE_LIMIT"):
        normalize_image(image_bytes())


def test_animation_rejected():
    out = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(out, format="PNG", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")])
    with pytest.raises(MediaError, match="MEDIA_PIXEL_LIMIT"):
        normalize_image(out.getvalue())


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1", "0.0.0.0", "100.64.0.1", "224.0.0.1", "ff02::1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::"])
def test_dns_rejects_nonpublic_addresses(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))])
    with pytest.raises(MediaError, match="MEDIA_URL_NOT_PUBLIC"):
        public_addresses("images.example", 443)


def test_dns_rejects_mixed_public_private_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (ip, 443)) for ip in ["8.8.8.8", "127.0.0.1"]])
    with pytest.raises(MediaError):
        public_addresses("images.example", 443)


@pytest.mark.parametrize("url", ["http://example.com/x.png", "https://user:password@example.com/x.png", "https://example.com:8443/x.png", "file:///x.png"])
def test_fetch_rejects_unsafe_url(url):
    with pytest.raises(MediaError):
        fetch_image(url)


def test_fetch_pins_ip_does_not_send_cookies_and_rechecks_redirect(monkeypatch):
    requests = []
    resolutions = []
    class Connection:
        sock = None
        def __init__(self, host, address, timeout):
            assert address == "8.8.8.8"
            self.host = host
        def request(self, method, path, headers):
            assert "Cookie" not in headers and "Authorization" not in headers
            requests.append((self.host, path))
        def getresponse(self):
            return SimpleNamespace(status=302, getheader=lambda key, default=None: "https://private.example/secret")
        def close(self): pass
    def resolve(host, port):
        resolutions.append(host)
        if host == "private.example":
            raise MediaError("MEDIA_URL_NOT_PUBLIC")
        return ["8.8.8.8"]
    monkeypatch.setattr("agent_control_connector.visual_media.public_addresses", resolve)
    monkeypatch.setattr("agent_control_connector.visual_media.PinnedHTTPSConnection", Connection)
    with pytest.raises(MediaError, match="MEDIA_URL_NOT_PUBLIC"):
        fetch_image("https://images.example/photo.png")
    assert requests == [("images.example", "/photo.png")]
    assert resolutions == ["images.example", "private.example"]


def test_tls_connect_uses_numeric_ip_but_verifies_original_hostname(monkeypatch):
    from agent_control_connector.visual_media import PinnedHTTPSConnection
    calls = []
    raw = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: calls.append((address, timeout)) or raw)
    connection = PinnedHTTPSConnection("images.example", "8.8.8.8", 3)
    connection._context = SimpleNamespace(wrap_socket=lambda sock, server_hostname: calls.append(server_hostname) or sock)
    connection.connect()
    assert calls == [(("8.8.8.8", 443), 3), "images.example"]


def test_download_enforces_bytes_even_without_content_length(monkeypatch):
    class Response:
        status = 200
        def getheader(self, key, default=None): return default
        def read(self, size): return b"x" * size
    class Connection:
        sock = None
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr("agent_control_connector.visual_media.public_addresses", lambda *a: ["8.8.8.8"])
    monkeypatch.setattr("agent_control_connector.visual_media.PinnedHTTPSConnection", Connection)
    monkeypatch.setattr("agent_control_connector.visual_media.MAX_BYTES", 20)
    with pytest.raises(MediaError, match="MEDIA_FILE_LIMIT"):
        fetch_image("https://images.example/photo.png")


def test_source_snapshot_and_durable_replay(tmp_path):
    start_turn(tmp_path)
    path = tmp_path / "secret-local-name.png"
    path.write_bytes(image_bytes())
    rows = enqueue(tmp_path, path=path)
    assert rows[0]["status"] == "pending"
    path.write_bytes(b"file was modified")
    first = next_publication(tmp_path, "profile")
    assert first["id"] == rows[0]["id"]
    assert first["sessionId"] == "session"
    assert "secret-local-name" not in json.dumps(first["metadata"])
    assert Image.open(io.BytesIO(first["content"])).size == (12, 20)
    with closing(plugin.open_queue(tmp_path)) as db, db:
        db.execute("UPDATE images SET next_attempt=0")
    assert next_publication(tmp_path, "profile") == first
    acknowledge(tmp_path, first["id"], "ready")
    assert next_publication(tmp_path, "profile") is None
    with closing(plugin.open_queue(tmp_path)) as db:
        row = db.execute("SELECT status,content,thumbnail FROM images").fetchone()
        assert tuple(row) == ("ready", None, None)


def test_failed_retryable_ack_keeps_same_outbox(tmp_path):
    start_turn(tmp_path)
    identifier = enqueue(tmp_path)[0]["id"]
    next_publication(tmp_path, "profile")
    acknowledge(tmp_path, identifier, "failed", "storage_unavailable")
    with closing(plugin.open_queue(tmp_path)) as db:
        assert db.execute("SELECT status FROM images").fetchone()[0] == "pending"
    acknowledge(tmp_path, identifier, "failed", "quota_exceeded")
    with closing(plugin.open_queue(tmp_path)) as db:
        assert tuple(db.execute("SELECT status,error_code,content FROM images").fetchone()) == ("failed", "quota_exceeded", None)


def test_gallery_and_response_limits_are_durable(tmp_path):
    start_turn(tmp_path)
    for _ in range(4):
        enqueue(tmp_path, count=6)
    with pytest.raises(ValueError, match="MEDIA_RESPONSE_LIMIT"):
        enqueue(tmp_path)
    with pytest.raises(ValueError, match="MEDIA_GALLERY_LIMIT"):
        enqueue(tmp_path, count=7)
    start_turn(tmp_path, turn="next-turn")
    assert enqueue(tmp_path)


def test_response_limit_does_not_reset_after_acknowledged_receipts_are_pruned(tmp_path):
    start_turn(tmp_path)
    for _ in range(4):
        enqueue(tmp_path, count=6)
    with closing(plugin.open_queue(tmp_path)) as db, db:
        db.execute("DELETE FROM images")
    with pytest.raises(ValueError, match="MEDIA_RESPONSE_LIMIT"):
        enqueue(tmp_path)


def test_session_is_runtime_only_and_outbox_bounded(tmp_path, monkeypatch):
    start_turn(tmp_path)
    with pytest.raises(ValueError, match="MEDIA_TURN_UNAVAILABLE"):
        enqueue(tmp_path, session="foreign")
    monkeypatch.setattr(plugin, "MAX_QUEUE_BYTES", 1)
    with pytest.raises(ValueError, match="MEDIA_OUTBOX_FULL"):
        enqueue(tmp_path)


def test_normalization_expansion_cannot_exceed_total_outbox_quota(tmp_path, monkeypatch):
    start_turn(tmp_path)
    monkeypatch.setattr(plugin, "MAX_QUEUE_BYTES", 1200)
    enqueue(tmp_path, count=2)
    monkeypatch.setattr("agent_control_connector.visual_media.normalize_image", lambda *args: (
        b"x" * 700, b"y" * 100, {"width": 12, "height": 20, "mediaType": "image/png"}))
    assert next_publication(tmp_path, "profile")
    assert next_publication(tmp_path, "profile") is None
    with closing(plugin.open_queue(tmp_path)) as db:
        assert db.execute("SELECT sum(coalesce(length(content),0)+coalesce(length(thumbnail),0)) FROM images").fetchone()[0] == 800
        assert db.execute("SELECT error_code FROM images WHERE status='failed'").fetchone()[0] == "MEDIA_OUTBOX_FULL"


@pytest.mark.parametrize("policy", [{"maxBytes": plugin.MAX_BYTES + 1}, {"maxPixels": 25_000_001},
    {"maxImagesPerGallery": 7}, {"maxImagesPerResponse": 25}, {"maxBytes": True}, {"owner": "someone"}])
def test_cloud_policy_cannot_raise_safety_caps(policy):
    with pytest.raises(ValueError, match="MEDIA_POLICY_UNAVAILABLE"):
        plugin.validate_policy(policy)


def test_lower_cloud_policy_applies_to_plugin_and_decoder(tmp_path):
    start_turn(tmp_path)
    policy = {**plugin.DEFAULT_POLICY, "maxImagesPerGallery": 1, "maxImagesPerResponse": 2, "maxPixels": 100}
    (plugin.queue_directory(tmp_path) / "policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="MEDIA_GALLERY_LIMIT"):
        enqueue(tmp_path, count=2)
    enqueue(tmp_path)
    enqueue(tmp_path)
    with pytest.raises(ValueError, match="MEDIA_RESPONSE_LIMIT"):
        enqueue(tmp_path)
    assert next_publication(tmp_path, "profile") is None
    with closing(plugin.open_queue(tmp_path)) as db:
        assert db.execute("SELECT error_code FROM images WHERE status='failed'").fetchone()[0] == "MEDIA_PIXEL_LIMIT"
    start_turn(tmp_path, turn="another")
    policy["maxBytes"] = 10
    (plugin.queue_directory(tmp_path) / "policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="MEDIA_FILE_LIMIT"):
        enqueue(tmp_path)


def test_symlinked_files_and_outbox_rejected(tmp_path):
    start_turn(tmp_path)
    path = tmp_path / "image.png"
    path.write_bytes(image_bytes())
    link = tmp_path / "link.png"
    link.symlink_to(path)
    with pytest.raises(OSError):
        enqueue(tmp_path, path=link)
    (tmp_path / ".agent-control/media/outbox.sqlite3").unlink()
    (tmp_path / ".agent-control/media/outbox.sqlite3").symlink_to(tmp_path / "foreign.sqlite3")
    with pytest.raises(ValueError, match="MEDIA_OUTBOX_UNSAFE"):
        plugin.open_queue(tmp_path)


def test_bad_image_terminal_failure_does_not_block_following_items(tmp_path):
    start_turn(tmp_path)
    bad = tmp_path / "bad.png"
    bad.write_text("not raster")
    enqueue(tmp_path, path=bad)
    enqueue(tmp_path)
    assert next_publication(tmp_path, "profile") is None
    assert next_publication(tmp_path, "profile")["metadata"]["width"] == 12


def test_profiles_cannot_share_symlinked_outbox(tmp_path):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles/real").mkdir()
    (tmp_path / "profiles/foreign").symlink_to(tmp_path / "profiles/real")
    with pytest.raises(MediaError):
        profile_home(tmp_path, "foreign")


def test_installer_preserves_soul_config_and_cron_overrides(tmp_path):
    config = {"model": "chosen", "plugins": {"enabled": ["other"]}, "tools": {"enabled_toolsets": ["terminal"]},
        "platform_toolsets": {"cli": ["file"], "cron": ["web"]}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    (tmp_path / "SOUL.md").write_text("Personal instructions")
    (tmp_path / "cron").mkdir()
    job = {"id": "job", "prompt": "Personal briefing", "schedule": {"expr": "0 7 * * *"}, "enabled_toolsets": ["web"], "next_run_at": "unchanged"}
    (tmp_path / "cron/jobs.json").write_text(json.dumps({"jobs": [job], "updated_at": "unchanged"}))
    assert install_profile(tmp_path)["state"] == "pendingActivation"
    after = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert after["model"] == "chosen"
    assert after["plugins"]["enabled"] == ["other", plugin.PLUGIN_NAME]
    assert after["platform_toolsets"]["cron"] == ["web", plugin.PLUGIN_NAME]
    assert (tmp_path / "SOUL.md").read_text() == "Personal instructions"
    after_job = json.loads((tmp_path / "cron/jobs.json").read_text())
    assert after_job == {"jobs": [{**job, "enabled_toolsets": ["web", plugin.PLUGIN_NAME]}], "updated_at": "unchanged"}
    first = (tmp_path / "config.yaml").read_bytes()
    assert install_profile(tmp_path)["state"] == "pendingActivation"
    assert (tmp_path / "config.yaml").read_bytes() == first


def test_default_cron_toolsets_are_not_replaced_with_media_only(tmp_path):
    (tmp_path / "cron").mkdir()
    jobs = [{"id": "empty", "enabled_toolsets": []}, {"id": "null", "enabled_toolsets": None}, {"id": "implicit"}]
    (tmp_path / "cron/jobs.json").write_text(json.dumps({"jobs": jobs}))
    install_profile(tmp_path)
    assert json.loads((tmp_path / "cron/jobs.json").read_text()) == {"jobs": jobs}


@pytest.mark.parametrize("config", [
    {"plugins": {"disabled": [plugin.PLUGIN_NAME]}},
    {"agent": {"disabled_toolsets": [plugin.PLUGIN_NAME]}},
    {"plugins": {"entries": {plugin.PLUGIN_NAME: {"enabled": False}}}},
])
def test_explicit_disable_preserved(tmp_path, config):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    assert install_profile(tmp_path)["state"] == "disabled"
    assert not (tmp_path / "plugins").exists()


def test_user_removing_plugin_from_allowlist_is_not_reenabled(tmp_path):
    install_profile(tmp_path)
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: []\n")
    assert install_profile(tmp_path)["state"] == "disabled"


def test_platform_toolset_disable_is_not_reenabled_or_added_to_cron(tmp_path):
    config = {"platform_toolsets": {"cli": ["file"], "cron": ["web"]},
        "known_plugin_toolsets": {"cron": [plugin.PLUGIN_NAME]}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron/jobs.json").write_text(json.dumps({"jobs": [{"enabled_toolsets": ["web"]}]}))
    install_profile(tmp_path)
    after = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert after["platform_toolsets"]["cli"] == ["file", plugin.PLUGIN_NAME]
    assert after["platform_toolsets"]["cron"] == ["web"]
    assert json.loads((tmp_path / "cron/jobs.json").read_text())["jobs"][0]["enabled_toolsets"] == ["web"]
    # Removing a previously managed list entry is also a deliberate disable.
    after["platform_toolsets"]["cli"].remove(plugin.PLUGIN_NAME)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(after))
    install_profile(tmp_path)
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["platform_toolsets"]["cli"] == ["file"]


def test_older_hermes_is_not_claimed_supported(tmp_path):
    assert media_profiles({"hermesHome": str(tmp_path), "profiles": ["default"], "sourceSha": "f" * 40}, install=True) == {"default": {"state": "unsupportedRuntime"}}
    assert not (tmp_path / "plugins").exists()


def test_native_plugin_registers_current_turn_and_probe(tmp_path, monkeypatch):
    install_profile(tmp_path)
    tools = {}
    hooks = {}
    sections = {}
    ctx = SimpleNamespace(register_tool=lambda name, toolset, schema, handler, **k: tools.setdefault(name, handler),
        register_system_prompt_section=lambda name, content, **k: sections.setdefault(name, content),
        register_hook=lambda name, handler: hooks.setdefault(name, handler))
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: tmp_path))
    plugin.register(ctx)
    assert probe_profile(tmp_path)["state"] == "ready"
    context = hooks["pre_llm_call"](session_id="real-session", turn_id="real-turn", is_first_turn=False)
    assert "publish_images" in context["context"]
    assert "agent-control-media" in sections
    # The model never receives session/profile/owner arguments in the schema.
    assert set(plugin.SCHEMA["parameters"]["properties"]) == {"images"}
    result = json.loads(tools["publish_images"]({"images": [], "session_id": "foreign"}, session_id="real-session"))
    assert result == {"error": "MEDIA_INVALID_ARGUMENTS"}
    assert hooks["pre_tool_call"](tool_name="cronjob", args={"enabled_toolsets": ["web"]}) == {
        "action": "modify", "args": {"enabled_toolsets": ["web", plugin.PLUGIN_NAME]}}
    with closing(plugin.open_queue(tmp_path)) as db:
        assert tuple(db.execute("SELECT session_id,turn_id FROM turns").fetchone()) == ("real-session", "real-turn")
    disabled = {"plugins": {"enabled": [plugin.PLUGIN_NAME]}, "agent": {"disabled_toolsets": [plugin.PLUGIN_NAME]}}
    monkeypatch.setitem(sys.modules, "hermes_cli.config", SimpleNamespace(load_config=lambda: disabled))
    assert hooks["pre_llm_call"](session_id="real-session", turn_id="new-turn") is None
    assert hooks["pre_tool_call"](tool_name="cronjob", args={"enabled_toolsets": ["web"]}) is None
    assert json.loads(tools["publish_images"]({"images": []}, session_id="real-session")) == {"error": "MEDIA_DISABLED"}


@pytest.mark.asyncio
async def test_connector_checks_real_profile_session_and_receives_ack(tmp_path, monkeypatch):
    from agent_control_connector.runtime import ConnectorRuntime
    home = tmp_path / "hermes"
    selected = home / "profiles/selected"
    start_turn(selected)
    identifier = enqueue(selected)[0]["id"]
    provider = SimpleNamespace(history_readonly=AsyncMock(return_value=[]))
    runtime = ConnectorRuntime(tmp_path / "connector", {"gatewayId": "g", "profiles": ["selected"],
        "restUrl": "http://127.0.0.1:9119", "wsUrl": "ws://127.0.0.1:9119/api/ws",
        "sourceSha": HERMES_0212_SHA, "hermesHome": str(home)}, {"hermesToken": "secret"},
        provider_factory=lambda *args: provider)
    socket = SimpleNamespace(send=AsyncMock())
    runtime.websocket = socket
    sent = []
    async def send(sender, lock, message):
        sent.append(message)
        runtime.websocket = None
    monkeypatch.setattr("agent_control_connector.runtime.send_message", send)
    try:
        runtime.visual_media_limits = {**plugin.DEFAULT_POLICY, "maxImagesPerGallery": 2}
        runtime._save_media_policy()
        assert plugin.load_policy(selected)["maxImagesPerGallery"] == 2
        await runtime._media_sender(socket)
        provider.history_readonly.assert_awaited_once_with("session")
        assert sent[0]["id"] == identifier and sent[0]["profile"] == "selected"
        assert sent[0]["type"] == "media.publish"
        await runtime._media_acknowledge({"type": "media.ack", "id": identifier, "status": "ready"})
        with closing(plugin.open_queue(selected)) as db:
            assert db.execute("SELECT status FROM images").fetchone()[0] == "ready"
    finally:
        runtime.ledger.close()
