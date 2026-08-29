from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps" / "api"))
sys.path.insert(0, str(REPO / "packages" / "hermes-client"))

from hermes_control_api.config import Settings  # noqa: E402
from hermes_control_api.main import create_app  # noqa: E402
from hermes_control_api.models import User  # noqa: E402
from hermes_control_api.security import hash_password  # noqa: E402


@pytest.fixture
def app():
    key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    settings = Settings(
        environment="test",
        database_url="sqlite://",
        vault_key_b64=key,
        provider_mode="mock",
        mock_fallback_enabled=True,
        hermes_source_sha="791e2ae3257e211d14ca77e654dfe10ee1976a1c",
        allowed_origins=["http://testserver"],
        secure_cookies=False,
        create_schema_on_start=True,
    )
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            db.add(
                User(
                    username="admin",
                    password_hash=hash_password("correct horse battery staple"),
                    is_admin=True,
                )
            )
            db.commit()
        yield client


@pytest.fixture
def authenticated(client: TestClient):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text
    return client, response.json()["csrfToken"]


def mutation_headers(csrf: str, key: str = "test-operation") -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}
