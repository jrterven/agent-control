from __future__ import annotations

import base64
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from hermes_control_api.auth import issue_session
from hermes_control_api.cloud_auth import enroll_google_identity, invite_email, safe_return_to, router
from hermes_control_api.config import Settings
from hermes_control_api.main import create_app
from hermes_control_api.models import (
    Automation, BetaInvitation, ExternalIdentity, Gateway, OIDCFlow, ProfileRef, SessionLink, User, utc_now,
)
from hermes_control_api.api.routes import bind_owned_realtime_event
from hermes_control_api.security import hash_password


@pytest.fixture
def cloud():
    settings = Settings(environment="test", deployment_mode="cloud", public_base_url="https://control.test",
        google_client_id="client", google_client_secret="secret", database_url="sqlite://",
        vault_key_b64=base64.urlsafe_b64encode(b"c" * 32).decode(), create_schema_on_start=True,
        provider_mode="mock", mock_fallback_enabled=True, allowed_origins=["http://testserver"])
    app = create_app(settings)
    if not any(getattr(route, "path", None) == "/api/v1/auth/methods" for route in app.routes):
        app.include_router(router)
    with TestClient(app) as client:
        yield app, client, settings


def seed_tenants(app):
    with app.state.session_factory() as db:
        owners = []
        for label in ("alice", "bob"):
            user = User(username=label, password_hash=hash_password("some unusable password"), is_admin=False)
            db.add(user)
            db.flush()
            gateway = Gateway(name=label, owner_id=user.id, transport_kind="connector", rest_url=f"connector://{label}", ws_url=f"connector://{label}")
            db.add(gateway)
            db.flush()
            profile = ProfileRef(gateway_id=gateway.id, profile_name="personal", display_name=label)
            session = SessionLink(owner_id=user.id, gateway_id=gateway.id, profile_name="personal", stored_session_id=label)
            db.add_all([profile, session])
            db.commit()
            token, csrf, _ = issue_session(db, user, ttl_hours=1)
            owners.append((user.id, gateway.id, profile.id, session.id, token, csrf))
        return owners


def test_cloud_methods_and_password_login_disabled(cloud):
    app, client, _ = cloud
    assert client.get("/api/v1/auth/methods").json() == {
        "mode": "cloud", "googleEnabled": True, "registrationMode": "invite_only", "betaMaxUsers": 20,
    }
    assert client.post("/api/v1/auth/login", json={"username":"x","password":"123456789012"}).status_code == 403
    with app.state.session_factory() as db:
        assert db.scalar(select(Gateway)) is None
    assert client.get("/api/v1/ready").json()["status"] == "ready"


def test_cloud_owner_scope_all_resource_projections(cloud):
    app, client, _ = cloud
    alice, bob = seed_tenants(app)
    client.cookies.set("hc_session", alice[4])
    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200, bootstrap.text
    body = bootstrap.json()
    assert body["userId"] == alice[0]
    assert [row["id"] for row in body["gateways"]] == [alice[1]]
    assert bob[1] not in bootstrap.text and bob[2] not in bootstrap.text and bob[3] not in bootstrap.text
    assert len(client.get("/api/v1/gateways").json()) == 1
    assert client.get(f"/api/v1/profiles/{bob[2]}/avatar").status_code == 404
    assert client.get(f"/api/v1/sessions/{bob[3]}/messages").status_code == 404
    assert client.get(f"/api/v1/integrations/openai/profiles/{bob[2]}/voice").status_code == 404
    assert client.get(f"/api/v1/admin/config?gatewayId={bob[1]}&profileName=personal").status_code in {404, 409}
    assert client.patch(f"/api/v1/gateways/{bob[1]}", json={"name":"stolen"}, headers={"X-CSRF-Token":alice[5],"Idempotency-Key":"foreign"}).status_code == 404
    for owner in (alice, bob):
        payload = {"type":"gateway.health", "gatewayId":owner[1], "profileName":"personal", "_routeIdentity":"gateway"}
        event = bind_owned_realtime_event(app.state.session_factory, user_id=alice[0], payload=payload, cloud_mode=True)
        assert (event is not None) == (owner == alice)


def test_google_enrollment_requires_invite_and_stable_subject(cloud):
    app, _, settings = cloud
    with app.state.session_factory() as db:
        claims = {"sub":"google-123", "email":"person@example.com", "email_verified":True}
        with pytest.raises(ValueError, match="invite_required"):
            enroll_google_identity(db, settings, claims)
        invitation = invite_email(db, settings, "PERSON@example.com")
        user = enroll_google_identity(db, settings, claims)
        assert not user.is_admin
        assert invitation.accepted_at is not None
        again = enroll_google_identity(db, settings, {**claims, "email":"renamed@example.com"})
        assert again.id == user.id
        with pytest.raises(ValueError, match="invite_required"):
            enroll_google_identity(db, settings, {**claims, "sub":"different-google-user"})
        assert len(db.scalars(select(ExternalIdentity)).all()) == 1


def test_invite_limit_expiry_and_unverified_email(cloud):
    app, _, settings = cloud
    settings.beta_max_users = 1
    with app.state.session_factory() as db:
        invitation = invite_email(db, settings, "a@example.com")
        with pytest.raises(ValueError, match="limit"):
            invite_email(db, settings, "b@example.com")
        with pytest.raises(ValueError, match="invite_required"):
            enroll_google_identity(db, settings, {"sub":"1", "email":"a@example.com", "email_verified":False})
        invitation.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
        with pytest.raises(ValueError, match="invite_required"):
            enroll_google_identity(db, settings, {"sub":"1", "email":"a@example.com", "email_verified":True})


def test_registration_configuration_defaults_closed_and_rejects_invalid_modes_or_capacity():
    assert Settings(_env_file=None).cloud_registration_mode == "invite_only"
    for values in ({"cloud_registration_mode": "public"}, {"beta_max_users": 21}, {"beta_max_users": 0}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **values)


def test_open_google_signup_does_not_require_invitation_or_merge_local_admin(cloud):
    app, client, settings = cloud
    settings.cloud_registration_mode = "open"
    assert client.get("/api/v1/auth/methods").json() == {
        "mode": "cloud", "googleEnabled": True, "registrationMode": "open", "betaMaxUsers": 20,
    }
    with app.state.session_factory() as db:
        admin = User(username="person@example.com", password_hash="unused", is_admin=True)
        db.add(admin)
        db.commit()
        claims = {"sub": "new-google-user", "email": "PERSON@example.com", "email_verified": True}
        user = enroll_google_identity(db, settings, claims)
        assert user.id != admin.id
        assert user.is_active and not user.is_admin
        assert db.scalar(select(BetaInvitation)) is None
        identity = db.scalar(select(ExternalIdentity))
        assert identity.user_id == user.id and identity.email == "person@example.com"
        assert identity.issuer == "https://accounts.google.com"
        # Subject is authoritative even when another verified account has the
        # same email. It must never inherit the existing account's resources.
        other = enroll_google_identity(db, settings, {**claims, "sub": "other-google-user"})
        assert other.id != user.id and other.id != admin.id
        assert not other.is_admin


def test_open_capacity_blocks_new_accounts_but_existing_accounts_keep_access(cloud):
    app, _, settings = cloud
    settings.cloud_registration_mode = "open"
    settings.beta_max_users = 1
    claims = {"sub": "first", "email": "first@example.com", "email_verified": True}
    with app.state.session_factory() as db:
        user = enroll_google_identity(db, settings, claims)
        with pytest.raises(ValueError, match="beta_full"):
            enroll_google_identity(db, settings, {**claims, "sub": "extra", "email": "extra@example.com"})
        db.rollback()
        again = enroll_google_identity(db, settings, {**claims, "email": "renamed@example.com"})
        assert again.id == user.id
        assert db.scalar(select(ExternalIdentity)).email == "renamed@example.com"
        assert len(db.scalars(select(ExternalIdentity)).all()) == 1
        user.is_active = False
        db.commit()
        with pytest.raises(ValueError, match="google_login_failed"):
            enroll_google_identity(db, settings, claims)
        db.rollback()
        # Disabling a user cannot be bypassed with a fresh identity, nor does
        # it silently release a slot while that user's account still exists.
        with pytest.raises(ValueError, match="beta_full"):
            enroll_google_identity(db, settings, {**claims, "sub": "extra"})


@pytest.mark.parametrize("verified", [False, None, "true", 1])
def test_open_signup_requires_a_verified_google_email(cloud, verified):
    app, _, settings = cloud
    settings.cloud_registration_mode = "open"
    with app.state.session_factory() as db:
        with pytest.raises(ValueError, match="google_login_failed"):
            enroll_google_identity(db, settings, {
                "sub": "unverified", "email": "person@example.com", "email_verified": verified,
            })
        assert db.scalar(select(ExternalIdentity)) is None
        assert db.scalar(select(User)) is None


@pytest.mark.parametrize("state", ["pending", "expired", "revoked"])
def test_open_signup_ignores_invitation_restrictions_and_consumes_only_valid_invites(cloud, state):
    app, _, settings = cloud
    settings.cloud_registration_mode = "open"
    with app.state.session_factory() as db:
        invitation = invite_email(db, settings, "person@example.com")
        if state == "expired":
            invitation.expires_at = utc_now() - timedelta(seconds=1)
        elif state == "revoked":
            invitation.revoked_at = utc_now()
        db.commit()
        user = enroll_google_identity(db, settings, {
            "sub": "open-user", "email": "person@example.com", "email_verified": True,
        })
        assert user.is_active and not user.is_admin
        assert (invitation.accepted_at is not None) == (state == "pending")
        assert invitation.user_id == (user.id if state == "pending" else None)


def test_open_callback_creates_session_and_full_beta_redirects_without_session(cloud, monkeypatch):
    app, client, settings = cloud
    settings.cloud_registration_mode = "open"
    settings.beta_max_users = 1

    async def exchange(settings, code, verifier, nonce):
        return {"sub": code, "email": f"{code}@example.com", "email_verified": True}

    monkeypatch.setattr("hermes_control_api.cloud_auth.exchange_google_code", exchange)
    for subject, location in (("first", "/connect"), ("extra", "/login?error=beta_full"), ("first", "/connect")):
        client.cookies.clear()
        start = client.get("/api/v1/auth/google/start?returnTo=%2Fconnect", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        response = client.get(f"/api/v1/auth/google/callback?state={state}&code={subject}", follow_redirects=False)
        assert response.headers["location"] == location
        if subject == "first":
            assert client.get("/api/v1/auth/me").json()["isAdmin"] is False
        else:
            assert "hc_session" not in client.cookies
            assert client.get("/api/v1/auth/me").status_code == 401
    with app.state.session_factory() as db:
        assert len(db.scalars(select(User)).all()) == 1
        assert db.scalar(select(BetaInvitation)) is None


def test_oidc_pkce_browser_binding_and_single_use_callback(cloud, monkeypatch):
    app, client, settings = cloud
    with app.state.session_factory() as db:
        invite_email(db, settings, "a@example.com")
    start = client.get("/api/v1/auth/google/start?returnTo=%2Fconnect%3Fcode%3DABCD", follow_redirects=False)
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]
    async def exchange(settings, code, verifier, nonce):
        assert code == "authorization" and len(verifier) >= 43 and nonce == query["nonce"][0]
        return {"sub":"verified-sub", "email":"a@example.com", "email_verified":True}
    monkeypatch.setattr("hermes_control_api.cloud_auth.exchange_google_code", exchange)
    state = query["state"][0]
    callback = f"/api/v1/auth/google/callback?state={state}&code=authorization"
    response = client.get(callback, follow_redirects=False)
    assert response.headers["location"] == "/connect?code=ABCD"
    assert client.get("/api/v1/auth/me").json()["isAdmin"] is False
    assert client.get(callback, follow_redirects=False).headers["location"] == "/login?error=google_login_failed"
    with app.state.session_factory() as db:
        assert db.scalar(select(OIDCFlow)).consumed_at is not None


def test_oidc_rejects_stolen_state_without_browser_cookie(cloud):
    _, client, _ = cloud
    start = client.get("/api/v1/auth/google/start", follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    client.cookies.clear()
    result = client.get(f"/api/v1/auth/google/callback?state={state}&code=stolen", follow_redirects=False)
    assert result.headers["location"] == "/login?error=google_login_failed"


@pytest.mark.parametrize("target", ["https://evil.test", "//evil.test", "/%2fevil.test", "/\\evil.test", "/\r\nevil", "/api/v1/auth/logout"])
def test_oidc_return_to_rejects_external_redirects(target):
    assert safe_return_to(target) == "/chats"


def test_google_id_token_verifies_signature_audience_nonce_and_expiry(cloud):
    from authlib.jose import JsonWebKey, JsonWebToken
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from hermes_control_api.cloud_auth import verify_google_id_token
    _, _, settings = cloud
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    jwks = {"keys":[JsonWebKey.import_key(public, {"kid":"test"}).as_dict()]}
    now = int(utc_now().timestamp())
    claims = {"iss":"https://accounts.google.com", "sub":"subject", "aud":"client", "nonce":"nonce", "iat":now, "exp":now + 300}
    jwt = JsonWebToken(["RS256"])
    def encode(payload):
        return jwt.encode({"alg":"RS256","kid":"test"}, payload, private).decode()
    assert verify_google_id_token(settings, encode(claims), jwks, "nonce")["sub"] == "subject"
    for override in ({"aud":"attacker"}, {"nonce":"other"}, {"exp":now-120}, {"iss":"https://attacker.test"}, {"azp":"attacker"}):
        with pytest.raises(Exception):
            verify_google_id_token(settings, encode({**claims, **override}), jwks, "nonce")
    bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad_public = bad_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(Exception):
        verify_google_id_token(settings, encode(claims), {"keys":[JsonWebKey.import_key(bad_public, {"kid":"test"}).as_dict()]}, "nonce")
