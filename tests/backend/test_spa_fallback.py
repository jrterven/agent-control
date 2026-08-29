from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from hermes_control_api.config import Settings
from hermes_control_api.main import create_app


def test_configured_static_bundle_serves_history_routes_but_never_unknown_api(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<main>Hermes Control</main>", encoding="utf-8")
    (tmp_path / "assets" / "app-deadbeef.js").write_text("export {};", encoding="utf-8")
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
        missing_api = client.get("/api/v1/does-not-exist")

    assert history.status_code == 200
    assert history.headers["content-type"].startswith("text/html")
    assert history.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")
