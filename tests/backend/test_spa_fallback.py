from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from hermes_control_api.config import Settings
from hermes_control_api.main import create_app


def test_configured_static_bundle_serves_history_routes_but_never_unknown_api(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<main>Hermes Control</main>", encoding="utf-8")
    (tmp_path / "assets" / "app-deadbeef.js").write_text("export {};", encoding="utf-8")
    (tmp_path / "boot-recovery.js").write_text("window.__bootRecovery = true;", encoding="utf-8")
    settings = Settings(
        environment="test",
        database_url="sqlite://",
        static_dir=str(tmp_path),
        vault_key_b64=base64.urlsafe_b64encode(b"s" * 32).decode("ascii"),
        provider_mode="mock",
        allowed_origins=["http://testserver"],
        secure_cookies=False,
        create_schema_on_start=True,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        history = client.get("/chats/session-example")
        asset = client.get("/assets/app-deadbeef.js")
        missing_asset = client.get("/assets/app-oldbeef.js")
        missing_extensionless_asset = client.get("/assets/app-oldbeef")
        missing_static_file = client.get("/missing-icon.svg")
        boot_recovery = client.get("/boot-recovery.js")
        missing_api = client.get("/api/v1/does-not-exist")

    assert history.status_code == 200
    assert history.headers["content-type"].startswith("text/html")
    assert history.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    for missing in (missing_asset, missing_extensionless_asset):
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")
        assert missing.headers["cache-control"] == "no-store"
        assert missing.headers["x-content-type-options"] == "nosniff"
        assert missing.json()["code"] == "NOT_FOUND"
    assert missing_static_file.status_code == 404
    assert missing_static_file.headers["content-type"].startswith("application/json")
    assert missing_static_file.headers["cache-control"] == "no-store"
    assert boot_recovery.status_code == 200
    assert boot_recovery.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert boot_recovery.headers["cache-control"] == "no-cache"
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")


def test_missing_asset_can_fall_back_to_a_safe_sibling_release(tmp_path):
    releases_root = tmp_path / "releases"
    previous_static = releases_root / "revision-1" / "apps" / "api" / "static"
    current_static = releases_root / "revision-2" / "apps" / "api" / "static"
    (previous_static / "assets").mkdir(parents=True)
    current_static.mkdir(parents=True)
    (previous_static / "assets" / "app-previous.js").write_text(
        "window.__previousRelease = true;",
        encoding="utf-8",
    )
    (previous_static / "boot-recovery.js").write_text(
        "window.__staleRecovery = true;",
        encoding="utf-8",
    )
    outside_asset = tmp_path / "outside-release.js"
    outside_asset.write_text("window.__private = true;", encoding="utf-8")
    (previous_static / "assets" / "escape.js").symlink_to(outside_asset)
    (current_static / "index.html").write_text("<main>Current release</main>", encoding="utf-8")
    settings = Settings(
        environment="test",
        database_url="sqlite://",
        static_dir=str(current_static),
        vault_key_b64=base64.urlsafe_b64encode(b"s" * 32).decode("ascii"),
        provider_mode="mock",
        allowed_origins=["http://testserver"],
        secure_cookies=False,
        create_schema_on_start=True,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        previous_asset = client.get("/assets/app-previous.js")
        escaped_asset = client.get("/assets/escape.js")
        missing_root_file = client.get("/boot-recovery.js")

    assert previous_asset.status_code == 200
    assert previous_asset.text == "window.__previousRelease = true;"
    assert previous_asset.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert previous_asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert escaped_asset.status_code == 404
    assert escaped_asset.headers["content-type"].startswith("application/json")
    assert escaped_asset.headers["cache-control"] == "no-store"
    assert missing_root_file.status_code == 404
    assert missing_root_file.headers["content-type"].startswith("application/json")
    assert missing_root_file.headers["cache-control"] == "no-store"
