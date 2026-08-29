from __future__ import annotations

from unittest.mock import AsyncMock

from hermes_control_api.models import User
from hermes_control_api.security import hash_password

from .conftest import mutation_headers


def test_session_and_automation_sync_reject_non_admin_before_upstream(
    authenticated, app
):
    client, _ = authenticated
    gateway_id = client.get("/api/v1/gateways").json()[0]["id"]
    with app.state.session_factory() as db:
        db.add(
            User(
                username="sync-non-admin",
                password_hash=hash_password("non admin password is long enough"),
                is_admin=False,
            )
        )
        db.commit()

    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={
            "username": "sync-non-admin",
            "password": "non admin password is long enough",
        },
    )
    assert login.status_code == 200, login.text
    csrf = login.json()["csrfToken"]
    app.state.services.provider_pool.get = AsyncMock(
        side_effect=AssertionError("non-admin sync reached Hermes")
    )

    session_sync = client.post(
        "/api/v1/sessions/sync",
        headers=mutation_headers(csrf, "non-admin-session-sync"),
        json={
            "gatewayId": gateway_id,
            "profileName": "control-dev",
        },
    )
    automation_sync = client.post(
        "/api/v1/automations/sync",
        params={"gatewayId": gateway_id, "profileName": "control-dev"},
        headers=mutation_headers(csrf, "non-admin-automation-sync"),
    )

    assert session_sync.status_code == 403
    assert automation_sync.status_code == 403
    assert session_sync.json()["detail"] == "Administrator access required"
    assert automation_sync.json()["detail"] == "Administrator access required"
