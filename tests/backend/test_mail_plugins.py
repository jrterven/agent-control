from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select

from hermes_control_api.mail_models import MailAccount, MailAgent, MailGrant, MailSendOperation
from hermes_control_api.mail_providers import MailError, public_socket
from hermes_control_api.models import ProfileRef, User
from hermes_control_api.security import hash_password
from .conftest import mutation_headers


def test_publisher_association_exposes_only_configured_public_id(client, app):
    path = "/.well-known/microsoft-identity-association.json"
    assert client.get(path).status_code == 404
    app.state.settings.mail_outlook_enabled = True
    app.state.settings.mail_outlook_client_id = "misconfigured-not-a-public-id"
    assert client.get(path).status_code == 404
    app.state.settings.mail_outlook_client_id = "edc26d1e-418e-44f0-af5d-486e24d5f525"
    app.state.settings.mail_outlook_client_secret = "SECRET-NEVER-PUBLISH"
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"associatedApplications": [{"applicationId": app.state.settings.mail_outlook_client_id}]}
    assert "SECRET" not in response.text


@pytest.fixture
def mail(authenticated, app, monkeypatch):
    client, csrf = authenticated
    app.state.settings.public_base_url = "https://control.example"
    monkeypatch.setattr("hermes_control_api.api.mail_routes.imap_work", lambda *args: {"ok": True})
    profiles = client.get("/api/v1/bootstrap").json()["profiles"]
    return client, csrf, profiles


def connect(mail, address="one@example.com", **changes):
    client, csrf, _ = mail
    response = client.post("/api/v1/mail/accounts", headers=mutation_headers(csrf), json={
        "provider": "hostinger", "service": "hostinger", "address": address, "username": address,
        "password": "SECRET-PASSWORD-NEVER-ECHO", "label": address, **changes,
    })
    assert response.status_code == 200, response.text
    assert "SECRET-PASSWORD" not in response.text
    return response.json()


def grant(mail, app, account, index=0):
    client, csrf, profiles = mail
    response = client.patch(f"/api/v1/mail/accounts/{account['id']}", headers=mutation_headers(csrf), json={"label": account["label"], "profileIds": [profiles[index]["id"]]})
    assert response.status_code == 200, response.text
    with app.state.session_factory() as db:
        agent = db.scalar(select(MailAgent).where(MailAgent.profile_id == profiles[index]["id"]))
        token = app.state.services.vault.decrypt(agent.token_ciphertext, aad=f"mail-agent:{agent.owner_id}:{agent.id}")
    return token


def rpc(client, token, name, arguments=None):
    response = client.post("/api/v1/mail/mcp", headers={"Authorization": "Bearer " + token}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}},
    })
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_multiaccount_reconnect_keeps_grants_and_encrypts_secrets(mail, app):
    first = connect(mail)
    grant(mail, app, first)
    second = connect(mail, "two@example.com")
    again = connect(mail, accountId=first["id"])
    assert again["id"] == first["id"] and again["agents"]
    assert first["id"] != second["id"]
    assert len(mail[0].get("/api/v1/mail/accounts").json()) == 2
    with app.state.session_factory() as db:
        row = db.get(MailAccount, first["id"])
        assert row.credential_ciphertext.startswith("v1.")
        assert "SECRET-PASSWORD" not in row.credential_ciphertext


def test_profile_permissions_and_revocation_are_enforced_on_every_call(mail, app):
    first, second = connect(mail), connect(mail, "two@example.com")
    token = grant(mail, app, first)
    other = grant(mail, app, second, 1)
    rows, error = rpc(mail[0], token, "mail_accounts")
    assert not error and [r["accountId"] for r in rows] == [first["id"]]
    denied, error = rpc(mail[0], other, "mail_read", {"accountId": first["id"], "messageId": "1:1"})
    assert error and denied["code"] == "MAIL_ACCOUNT_FORBIDDEN"
    removed = mail[0].delete(f"/api/v1/mail/accounts/{first['id']}", headers=mutation_headers(mail[1]))
    assert removed.status_code == 204
    assert rpc(mail[0], token, "mail_accounts")[0] == []


def test_another_user_cannot_read_update_or_reconnect_account(mail, app):
    account = connect(mail)
    with app.state.session_factory() as db:
        db.add(User(username="other", password_hash=hash_password("another secure password")))
        db.commit()
    login = mail[0].post("/api/v1/auth/login", json={"username": "other", "password": "another secure password"}).json()
    assert mail[0].get("/api/v1/mail/accounts").json() == []
    response = mail[0].patch(f"/api/v1/mail/accounts/{account['id']}", headers=mutation_headers(login["csrfToken"]), json={"label": "Stolen", "profileIds": []})
    assert response.status_code == 404


def test_browser_mutations_require_csrf_and_mcp_requires_bearer(mail):
    assert mail[0].post("/api/v1/mail/accounts", json={}).status_code == 403
    assert mail[0].post("/api/v1/mail/mcp", json={}).status_code == 401
    assert mail[0].post("/api/v1/mail/mcp", headers={"Authorization": "Bearer invalid"}, json={}).status_code == 401


def test_mcp_discovery_and_bad_parameters(mail, app):
    token = grant(mail, app, connect(mail))
    headers = {"Authorization": "Bearer " + token}
    initialized = mail[0].post("/api/v1/mail/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}).json()
    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    listing = mail[0].post("/api/v1/mail/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    tools = {tool["name"]: tool for tool in listing["result"]["tools"]}
    assert tools["mail_read"]["annotations"]["readOnlyHint"]
    assert not tools["mail_send"]["annotations"]["readOnlyHint"]
    assert "userRequestedSend" in tools["mail_send"]["inputSchema"]["required"]
    invalid = mail[0].post("/api/v1/mail/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": {}}})
    assert invalid.json()["error"]["code"] == -32602


def test_send_deduplicates_and_never_retries_an_uncertain_delivery(mail, app, monkeypatch):
    account = connect(mail)
    token = grant(mail, app, account)
    sender = AsyncMock(return_value={"status": "accepted"})
    monkeypatch.setattr("hermes_control_api.mail_service.send_message", sender)
    args = {"accountId": account["id"], "operationId": str(uuid4()), "to": ["friend@example.com"], "subject": "Hello", "body": "A draft", "userRequestedSend": True}
    assert rpc(mail[0], token, "mail_send", args)[0]["status"] == "accepted"
    assert rpc(mail[0], token, "mail_send", args)[0]["status"] == "accepted"
    assert sender.await_count == 1
    conflict, error = rpc(mail[0], token, "mail_send", {**args, "body": "Different"})
    assert error and conflict["code"] == "MAIL_SEND_CONFLICT"
    sender.side_effect = TimeoutError()
    args["operationId"] = str(uuid4())
    assert rpc(mail[0], token, "mail_send", args)[1]
    assert rpc(mail[0], token, "mail_send", args)[0]["status"] == "delivery_unknown"
    assert sender.await_count == 2
    with app.state.session_factory() as db:
        assert len(list(db.scalars(select(MailSendOperation)))) == 2


def test_oauth_is_separate_single_use_and_bound_to_browser(mail, app, monkeypatch):
    settings = app.state.settings
    settings.mail_gmail_enabled, settings.mail_gmail_client_id, settings.mail_gmail_client_secret = True, "mail-client", "SECRET-OAUTH"
    token = {"access_token": "SECRET-ACCESS", "refresh_token": "SECRET-REFRESH", "expires_in": 3600, "scope": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"}
    exchange = AsyncMock(return_value=token)
    monkeypatch.setattr("hermes_control_api.api.mail_routes.http_json", exchange)
    monkeypatch.setattr("hermes_control_api.api.mail_routes.identity", AsyncMock(return_value=("gmail-sub", "mailbox@example.com")))
    start = mail[0].post("/api/v1/mail/oauth/gmail/start", headers=mutation_headers(mail[1]), json={})
    assert start.status_code == 200
    assert "httponly" in start.headers["set-cookie"].lower()
    parameters = parse_qs(urlsplit(start.json()["authorizationUrl"]).query)
    assert parameters["client_id"] == ["mail-client"]
    assert parameters["code_challenge_method"] == ["S256"]
    path = "/api/v1/mail/oauth/gmail/callback?code=test&state=" + parameters["state"][0]
    result = mail[0].get(path, follow_redirects=False)
    assert result.headers["location"] == "/settings?mailResult=connected#plugins"
    assert mail[0].get("/api/v1/auth/me").json()["username"] == "admin"
    assert mail[0].get(path, follow_redirects=False).headers["location"].endswith("failed#plugins")
    assert exchange.await_count == 1
    listing = mail[0].get("/api/v1/mail/accounts")
    assert "SECRET" not in listing.text and listing.json()[0]["address"] == "mailbox@example.com"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1", "64:ff9b::7f00:1"])
def test_imap_smtp_reject_non_public_destinations(monkeypatch, ip):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", (ip, 993))])
    with pytest.raises(MailError, match="MAIL_INVALID_HOST"):
        public_socket("mail.example.com", 993)


def test_reconnect_different_account_does_not_overwrite(mail):
    account = connect(mail)
    response = mail[0].post("/api/v1/mail/accounts", headers=mutation_headers(mail[1]), json={"provider": "hostinger", "service": "hostinger", "address": "other@example.com", "username": "other@example.com", "password": "secret", "accountId": account["id"]})
    assert response.status_code == 409
    assert mail[0].get("/api/v1/mail/accounts").json()[0]["address"] == account["address"]


def test_provision_uses_native_mcp_and_preserves_existing_servers(mail, app):
    account = connect(mail)
    grant(mail, app, account)
    async def exercise():
        with app.state.session_factory() as db:
            agent = db.scalar(select(MailAgent))
            profile = db.get(ProfileRef, agent.profile_id)
            from hermes_control_api.services import GatewayService
            provider = await app.state.services.provider_pool.get(await GatewayService(app.state.services).connection(db, profile.gateway_id, profile.profile_name))
            await provider.create_mcp_server({"name": "existing", "url": "https://existing.example/mcp"})
            await app.state.mail_service.provision(db, agent)
            listing = await provider.list_mcp_servers()
            assert len(listing.data["servers"]) == 2
            assert any(row["name"] == "existing" for row in listing.data["servers"])
            assert agent.state == "ready"
    asyncio.run(exercise())


def test_oauth_refresh_is_rotated_encrypted_and_failures_require_reconnect(mail, app, monkeypatch):
    app.state.settings.mail_outlook_enabled = True
    app.state.settings.mail_outlook_client_id = "client"
    app.state.settings.mail_outlook_client_secret = "SECRET-CLIENT"
    refresh = AsyncMock(return_value={"access_token": "SECRET-NEW-ACCESS", "refresh_token": "SECRET-ROTATED", "expires_in": 3600})
    monkeypatch.setattr("hermes_control_api.mail_service.http_json", refresh)
    async def exercise():
        with app.state.session_factory() as db:
            owner = db.scalar(select(User).where(User.username == "admin"))
            account = app.state.mail_service.save(db, owner.id, "outlook", "external", "one@example.com", {"refresh_token": "SECRET-OLD", "expires_at": 0})
            first = await app.state.mail_service.credentials(db, account)
            second = await app.state.mail_service.credentials(db, account)
            assert first == second and first["refresh_token"] == "SECRET-ROTATED"
            assert refresh.await_count == 1
            assert "SECRET" not in account.credential_ciphertext
            app.state.mail_service.seal(account, {**first, "expires_at": 0})
            db.commit()
            refresh.side_effect = MailError()
            with pytest.raises(MailError, match="MAIL_RECONNECT_REQUIRED"):
                await app.state.mail_service.credentials(db, account)
            assert account.status == "reconnect_required"
    asyncio.run(exercise())


def test_revocation_during_token_refresh_blocks_provider_read(mail, app, monkeypatch):
    account = connect(mail)
    token = grant(mail, app, account)
    reader = AsyncMock(return_value={})
    monkeypatch.setattr("hermes_control_api.mail_service.read_message", reader)
    async def refresh(db, row):
        from sqlalchemy import delete
        db.execute(delete(MailGrant).where(MailGrant.account_id == row.id))
        db.commit()
        return {"password": "secret"}
    monkeypatch.setattr(app.state.mail_service, "credentials", refresh)
    result, error = rpc(mail[0], token, "mail_read", {"accountId": account["id"], "messageId": "1:2"})
    assert error and result["code"] == "MAIL_ACCOUNT_FORBIDDEN"
    reader.assert_not_awaited()


def test_invalid_credentials_are_not_echoed_and_drafts_cannot_send(mail, app, monkeypatch):
    response = mail[0].post("/api/v1/mail/accounts", headers=mutation_headers(mail[1]), json={"provider": "imap", "address": "invalid", "username": "SECRET-USERNAME", "password": "SECRET-PASSWORD"})
    assert response.status_code == 422 and "SECRET" not in response.text
    account = connect(mail)
    token = grant(mail, app, account)
    sender = AsyncMock()
    monkeypatch.setattr("hermes_control_api.mail_service.send_message", sender)
    response = mail[0].post("/api/v1/mail/mcp", headers={"Authorization": "Bearer " + token}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mail_send", "arguments": {
            "accountId": account["id"], "operationId": str(uuid4()), "to": ["one@example.com"], "subject": "Draft", "body": "Draft", "userRequestedSend": False}}})
    assert response.json()["error"]["code"] == -32602
    sender.assert_not_awaited()


def test_offline_agent_is_pending_and_retried_without_losing_grants(mail, app, monkeypatch):
    from hermes_control_api.services import GatewayService
    account = connect(mail)
    grant(mail, app, account)
    monkeypatch.setattr(GatewayService, "connection", AsyncMock(side_effect=ConnectionError("offline")))
    async def exercise():
        with app.state.session_factory() as db:
            agent = db.scalar(select(MailAgent))
            await app.state.mail_service.provision(db, agent)
            assert agent.state == "pending" and db.get(MailGrant, (account["id"], agent.id))
    asyncio.run(exercise())


def test_tls_and_readonly_imap_contract(monkeypatch):
    from hermes_control_api import mail_providers as providers
    from unittest.mock import MagicMock
    imap, smtp = MagicMock(), MagicMock()
    imap.__enter__.return_value = imap
    smtp.__enter__.return_value = smtp
    imap.select.return_value = ("OK", [])
    imap.response.return_value = ("UIDVALIDITY", [b"12"])
    imap.uid.side_effect = [("OK", [b"4"]), ("OK", [(b"1 (UID 4", b"Subject: Test\r\nFrom: Name <one@example.com>\r\nMessage-ID: <test@example.com>\r\n\r\n")])]
    factory = MagicMock(return_value=imap)
    monkeypatch.setattr(providers, "PublicIMAP", factory)
    monkeypatch.setattr(providers, "smtp_client", lambda *args: smtp)
    cfg = {"imapHost": "imap.example.com", "smtpHost": "smtp.example.com", "smtpPort": 465, "username": "one@example.com", "address": "one@example.com"}
    providers.imap_work(cfg, {"password": "secret"}, "test", {})
    smtp.send_message.assert_not_called()
    rows = providers.imap_work(cfg, {"password": "secret"}, "search", {})
    assert rows[0]["uid"] == "4" and rows[0]["mailbox"] == "INBOX"
    assert rows[0]["senderAddress"] == "one@example.com"
    imap.select.assert_called_once_with("INBOX", readonly=True)
    assert "BODY.PEEK" in imap.uid.call_args.args[2]
    assert factory.call_args.args[1] == 993
    assert factory.call_args.kwargs["ssl_context"].check_hostname


def test_smtp_requires_starttls_before_authentication(monkeypatch):
    from hermes_control_api import mail_providers as providers
    from unittest.mock import MagicMock
    client = MagicMock()
    client.starttls.side_effect = providers.smtplib.SMTPNotSupportedError()
    monkeypatch.setattr(providers, "PublicSMTP", MagicMock(return_value=client))
    with pytest.raises(providers.smtplib.SMTPNotSupportedError):
        providers.smtp_client({"smtpHost": "smtp.example.com", "smtpPort": 587, "username": "one"}, {"password": "secret"})
    client.login.assert_not_called()
    client.close.assert_called_once()


def test_missing_native_mcp_setup_does_not_report_ready(mail, app, monkeypatch):
    from hermes_client.provider import InMemoryHermesProvider
    from hermes_client.admin import admin_snapshot
    account = connect(mail)
    grant(mail, app, account)
    monkeypatch.setattr(InMemoryHermesProvider, "test_mcp_server", AsyncMock(return_value=admin_snapshot("mcp", {"ok": False, "tools": []})))
    async def exercise():
        with app.state.session_factory() as db:
            agent = db.scalar(select(MailAgent))
            await app.state.mail_service.provision(db, agent)
            assert agent.state == "setup_required"
    asyncio.run(exercise())


def test_cloud_connector_ownership_and_revocation_block_mail(mail, app):
    from hermes_control_api.connector_models import Connector
    from hermes_control_api.models import Gateway, utc_now
    account = connect(mail)
    token = grant(mail, app, account)
    with app.state.session_factory() as db:
        agent = db.scalar(select(MailAgent))
        profile = db.get(ProfileRef, agent.profile_id)
        gateway = db.get(Gateway, profile.gateway_id)
        gateway.owner_id = agent.owner_id
        connector = Connector(owner_id=agent.owner_id, gateway_id=gateway.id, name="Test", token_hash="fake-connector-hash", profiles=[profile.profile_name])
        db.add(connector)
        db.commit()
        connector_id = connector.id
    app.state.settings.deployment_mode = "cloud"
    assert rpc(mail[0], token, "mail_accounts")[0][0]["accountId"] == account["id"]
    with app.state.session_factory() as db:
        db.get(Connector, connector_id).revoked_at = utc_now()
        db.commit()
    assert mail[0].post("/api/v1/mail/mcp", headers={"Authorization": "Bearer " + token}, json={}).status_code == 403


@pytest.mark.parametrize("failure", ["cookie", "expiry", "session", "scope"])
def test_oauth_rejects_unbound_expired_revoked_or_incomplete_consent(mail, app, monkeypatch, failure):
    from datetime import timedelta
    from hermes_control_api.mail_models import MailOAuthFlow
    from hermes_control_api.models import AuthSession, utc_now
    cfg = app.state.settings
    cfg.mail_gmail_enabled, cfg.mail_gmail_client_id, cfg.mail_gmail_client_secret = True, "mail", "secret"
    exchange = AsyncMock(return_value={"access_token": "secret", "refresh_token": "refresh", "scope": "gmail.send"})
    monkeypatch.setattr("hermes_control_api.api.mail_routes.http_json", exchange)
    start = mail[0].post("/api/v1/mail/oauth/gmail/start", headers=mutation_headers(mail[1]), json={}).json()
    state = parse_qs(urlsplit(start["authorizationUrl"]).query)["state"][0]
    if failure == "cookie":
        mail[0].cookies.clear()
    with app.state.session_factory() as db:
        flow = db.scalar(select(MailOAuthFlow))
        if failure == "expiry":
            flow.expires_at = utc_now() - timedelta(seconds=1)
        if failure == "session":
            db.get(AuthSession, flow.session_id).revoked_at = utc_now()
        db.commit()
    response = mail[0].get("/api/v1/mail/oauth/gmail/callback?code=example&state=" + state, follow_redirects=False)
    assert response.headers["location"].endswith("failed#plugins")
    assert exchange.await_count == (1 if failure == "scope" else 0)
    with app.state.session_factory() as db:
        assert not list(db.scalars(select(MailAccount)))
