"""Opt-in integration checks against disposable schemas in a real PostgreSQL DB.

Set AGENT_CONTROL_TEST_POSTGRES_URL to a dedicated validation database. Each
case creates and removes only its own random schema; public is never modified.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from hermes_control_api.auth import issue_session
from hermes_control_api.cloud_auth import enroll_google_identity, invite_email
from hermes_control_api.config import Settings
from hermes_control_api.database import build_engine, build_session_factory
from hermes_control_api.main import create_app
from hermes_control_api.models import BetaInvitation, ExternalIdentity, Gateway, LiveTranscript, ProfileRef, SessionLink, User

REPO = Path(__file__).resolve().parents[2]
POSTGRES = os.environ.get("AGENT_CONTROL_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES, reason="Set AGENT_CONTROL_TEST_POSTGRES_URL for real PostgreSQL checks")


def test_visual_media_quota_reservations_are_serialized(pg_url):
    import io
    from PIL import Image
    from hermes_control_api.connector_models import Connector
    from hermes_control_api.models import VisualMedia
    from hermes_control_api.visual_media import VisualMediaService, normalize_image
    migrate(pg_url)
    cfg = settings(pg_url)
    output = io.BytesIO()
    Image.new("RGB", (80, 60), "red").save(output, format="PNG")
    content = output.getvalue()
    full, thumb, _, _, _ = normalize_image(content, cfg)
    cfg = cfg.model_copy(update={"visual_media_quota_bytes": len(full) + len(thumb)})
    factory = build_session_factory(build_engine(cfg))
    with factory() as db:
        owner = User(username="media-quota", password_hash="none")
        db.add(owner)
        db.flush()
        gateway = Gateway(name="media-host", owner_id=owner.id, transport_kind="connector",
                          rest_url="http://unused.invalid", ws_url="ws://unused.invalid")
        db.add(gateway)
        db.flush()
        connector = Connector(owner_id=owner.id, gateway_id=gateway.id, name="Host", profiles=["jarvis"], token_hash="media-token")
        db.add(connector)
        db.commit()
        connector_id = connector.id
    class Store:
        def put(self, key, data, media_type):
            pass
    service = VisualMediaService(cfg, Store())
    ready = Barrier(2)
    def submit(identifier):
        with factory() as db:
            connector = db.get(Connector, connector_id)
            ready.wait(timeout=10)
            return service.ingest(db, connector, media_id=identifier, profile_name="jarvis", stored_session_id="cron",
                metadata=dict(alt="Figure", provenance="local", mediaType="image/png", width=80, height=60), content=content)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["a" * 32, "b" * 32]))
    assert sorted(result["status"] for result in results) == ["failed", "ready"]
    assert next(result for result in results if result["status"] == "failed")["errorCode"] == "quota_exceeded"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(VisualMedia)) == 1


@pytest.fixture
def pg_url():
    url = make_url(POSTGRES).set(drivername="postgresql+psycopg")
    admin = create_engine(url, pool_pre_ping=True)
    schema = "agent_control_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = url.update_query_dict({"options":f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    try:
        yield isolated
    finally:
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def migrate(url, revision="head", *, action="upgrade"):
    environment = {**os.environ,
        "HERMES_CONTROL_ENVIRONMENT":"test", "HERMES_CONTROL_DEPLOYMENT_MODE":"private",
        "HERMES_CONTROL_DATABASE_URL":url,
        "PYTHONPATH":os.pathsep.join([str(REPO / "apps/api"), str(REPO / "packages/hermes-client")]),
    }
    result = subprocess.run([sys.executable,"-m","alembic","-c",str(REPO / "apps/api/alembic.ini"), action, revision],
        cwd=REPO, env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def settings(url):
    return Settings(environment="test", deployment_mode="cloud", public_base_url="https://control.test",
        database_url=url, vault_key_b64=base64.urlsafe_b64encode(b"p" * 32).decode(),
        allowed_origins=["http://testserver"], create_schema_on_start=False)


def test_postgresql_upgrade_preserves_private_resources_and_assigns_owner(pg_url):
    migrate(pg_url, "0021_live_transcripts")
    engine = create_engine(pg_url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id, username, password_hash, is_admin, is_active, created_at, updated_at) VALUES ('admin','legacy-admin','unused',true,true,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        db.execute(text("INSERT INTO gateways(id,name,rest_url,ws_url,connection_mode,enabled,env_managed,health_status,created_at,updated_at) VALUES ('gateway','legacy-host','http://127.0.0.1:9119','ws://127.0.0.1:9119/api/ws','private',true,true,'offline',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        db.execute(text("INSERT INTO profile_refs(id,gateway_id,profile_name,display_name,status,capabilities,created_at,updated_at) VALUES ('profile','gateway','personal','Personal','offline','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        db.execute(text("INSERT INTO session_links(id,owner_id,gateway_id,profile_name,stored_session_id,title,status,last_sequence,created_at,updated_at,last_activity_at) VALUES ('session','admin','gateway','personal','hermes-history','Keep this title','ready',91,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    migrate(pg_url)
    migrate(pg_url)
    with engine.connect() as db:
        assert db.execute(text("SELECT owner_id,transport_kind FROM gateways WHERE id='gateway'")).one() == ("admin", "direct")
        assert db.execute(text("SELECT id,title,last_sequence FROM session_links")).one() == ("session","Keep this title",91)
        assert db.execute(text("SELECT profile_name FROM profile_refs")).scalar_one() == "personal"
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0030_semantic_search"
        assert db.execute(text("SELECT chat_mode FROM session_links")).scalar_one() == "memory_read_write"
    migrate(pg_url, "0021_live_transcripts", action="downgrade")
    migrate(pg_url)
    with engine.connect() as db:
        assert db.execute(text("SELECT owner_id FROM gateways")).scalar_one() == "admin"
        assert db.execute(text("SELECT title FROM session_links")).scalar_one() == "Keep this title"
    engine.dispose()


def test_postgresql_concurrent_invites_stop_at_twenty_and_identity_is_stable(pg_url):
    migrate(pg_url)
    configuration = settings(pg_url)
    engine = build_engine(configuration)
    factory = build_session_factory(engine)
    def invite(index):
        with factory() as db:
            try:
                return invite_email(db, configuration, f"person{index}@example.com").email
            except ValueError:
                return None
    with ThreadPoolExecutor(max_workers=21) as pool:
        accepted = [email for email in pool.map(invite, range(21)) if email]
    assert len(accepted) == 20
    def enroll(email):
        with factory() as db:
            return enroll_google_identity(db, configuration, {"sub":f"google:{email}", "email":email, "email_verified":True}).id
    with ThreadPoolExecutor(max_workers=20) as pool:
        users = list(pool.map(enroll, accepted))
    assert len(set(users)) == 20
    with ThreadPoolExecutor(max_workers=10) as pool:
        repeated = list(pool.map(enroll, [accepted[0]] * 10))
    assert set(repeated) == {users[0]}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ExternalIdentity)) == 20
        assert db.scalar(select(func.count()).select_from(BetaInvitation).where(BetaInvitation.accepted_at.is_not(None))) == 20
        assert db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) == 0
        with pytest.raises(ValueError, match="limit"):
            invite_email(db, configuration, "person21@example.com")
    engine.dispose()


def test_postgresql_open_signup_serializes_the_last_slot_and_keeps_existing_logins(pg_url):
    migrate(pg_url)
    configuration = settings(pg_url)
    configuration.cloud_registration_mode = "open"
    engine = build_engine(configuration)
    factory = build_session_factory(engine)
    with factory() as db:
        for index in range(19):
            user = User(username=f"existing{index}@example.com", password_hash="unused", is_admin=False)
            db.add(user)
            db.flush()
            db.add(ExternalIdentity(user_id=user.id, issuer="https://accounts.google.com",
                subject=f"existing{index}", email=user.username))
        db.commit()
    barrier = Barrier(8)

    def enroll(index):
        with factory() as db:
            barrier.wait(timeout=15)
            try:
                user = enroll_google_identity(db, configuration, {
                    "sub": f"new{index}", "email": f"new{index}@example.com", "email_verified": True,
                })
                return index, user.id
            except ValueError as exc:
                assert str(exc) == "beta_full"
                return index, None

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(enroll, range(8)))
        winners = [(index, user_id) for index, user_id in results if user_id]
        assert len(winners) == 1
        index, user_id = winners[0]
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(ExternalIdentity)) == 20
            assert db.scalar(select(func.count()).select_from(User)) == 20
            assert db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) == 0
            assert db.scalar(select(func.count()).select_from(BetaInvitation)) == 0
            returning = enroll_google_identity(db, configuration, {
                "sub": f"new{index}", "email": "renamed@example.com", "email_verified": True,
            })
            assert returning.id == user_id
    finally:
        engine.dispose()


def test_postgresql_api_never_exposes_other_tenants_gateway_profile_or_session(pg_url):
    migrate(pg_url)
    app = create_app(settings(pg_url))
    with TestClient(app) as client:
        owners = []
        with app.state.session_factory() as db:
            for name in ("alice", "bob"):
                user = User(username=name, password_hash="unused", is_admin=False)
                db.add(user)
                db.flush()
                gateway = Gateway(owner_id=user.id, name=name, transport_kind="connector", rest_url=f"connector://{name}",ws_url=f"connector://{name}")
                db.add(gateway)
                db.flush()
                profile = ProfileRef(gateway_id=gateway.id,profile_name="personal",display_name=name)
                session = SessionLink(owner_id=user.id,gateway_id=gateway.id,profile_name="personal",stored_session_id=name)
                db.add_all([profile,session])
                db.commit()
                token,csrf,_ = issue_session(db,user,ttl_hours=1)
                owners.append((gateway.id,profile.id,session.id,token,csrf))
        for own, other in ((owners[0],owners[1]),(owners[1],owners[0])):
            client.cookies.set("hc_session",own[3])
            response = client.get("/api/v1/bootstrap")
            assert response.status_code == 200, response.text
            assert [row["id"] for row in response.json()["gateways"]] == [own[0]]
            assert all(identifier not in response.text for identifier in other[:3])
            assert client.get(f"/api/v1/profiles/{other[1]}/avatar").status_code == 404
            assert client.get(f"/api/v1/sessions/{other[2]}/messages").status_code == 404
            response = client.patch(f"/api/v1/gateways/{other[0]}",json={"name":"stolen"},
                headers={"X-CSRF-Token":own[4],"Idempotency-Key":uuid4().hex})
            assert response.status_code == 404


def test_postgresql_voice_transcripts_append_retry_stay_encrypted_and_owner_scoped(pg_url):
    migrate(pg_url)
    app = create_app(settings(pg_url))
    with TestClient(app) as client:
        owners = []
        with app.state.session_factory() as db:
            for name in ("voice-alice", "voice-bob"):
                user = User(username=name, password_hash="unused", is_admin=False)
                db.add(user)
                db.flush()
                gateway = Gateway(owner_id=user.id, name=name, transport_kind="connector",
                    rest_url=f"connector://{name}", ws_url=f"connector://{name}")
                db.add(gateway)
                db.flush()
                session = SessionLink(owner_id=user.id, gateway_id=gateway.id,
                    profile_name="personal", stored_session_id=name)
                db.add(session)
                db.commit()
                token, csrf, _ = issue_session(db, user, ttl_hours=1)
                owners.append((user.id, session.id, token, csrf))
        alice, bob = owners
        client.cookies.set("hc_session", alice[2])
        headers = {"X-CSRF-Token": alice[3]}
        path = f"/api/v1/sessions/{alice[1]}/live-transcripts"
        call_id = str(uuid4())
        first = {"role": "user", "text": "Private PostgreSQL voice message", "order": 0, "start": 0, "end": 1}
        second = {"role": "assistant", "text": "Private voice response", "order": 1, "start": 1, "end": 2}
        initial = {"fragments": [first]}
        append = {"offset": 1, "fragments": [second]}
        for payload in (initial, initial, append, append, initial):
            response = client.put(f"{path}/{call_id}", headers=headers, json=payload)
            assert response.status_code == 204, response.text
        page = client.get(path)
        assert page.status_code == 200, page.text
        assert page.json()["items"][0]["fragments"] == [first, second]
        assert len(page.json()["items"]) == 1
        with app.state.session_factory() as db:
            row = db.get(LiveTranscript, call_id)
            assert row.revision == 2
            assert row.owner_id == alice[0]
            assert row.payload_ciphertext.startswith("v1.")
            assert all(part["text"] not in row.payload_ciphertext for part in (first, second))
            with pytest.raises(ValueError):
                app.state.services.vault.decrypt(row.payload_ciphertext,
                    aad=f"live-transcript:{bob[0]}:{alice[1]}:{call_id}")
        client.cookies.set("hc_session", bob[2])
        headers = {"X-CSRF-Token": bob[3]}
        assert client.get(path).status_code == 404
        assert client.put(f"{path}/{call_id}", headers=headers, json=initial).status_code == 404
        own_path = f"/api/v1/sessions/{bob[1]}/live-transcripts"
        assert client.put(f"{own_path}/{call_id}", headers=headers, json=initial).status_code == 404
        assert client.get(own_path).json()["items"] == []


def test_semantic_migration_leases_encryption_and_revocation(pg_url):
    from types import SimpleNamespace
    import hashlib
    import numpy as np
    from hermes_control_api.connector_models import Connector
    from hermes_control_api.models import SemanticFragment, SemanticIndexState
    from hermes_control_api.openai_live import OpenAIIntegrationService
    from hermes_control_api.security import SecretVault
    from hermes_control_api.semantic_search import DIMENSIONS, SemanticSearch

    migrate(pg_url, "0029_chat_modes")
    cfg = settings(pg_url)
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    vault = SecretVault(b"s" * 32)
    with factory() as db:
        owner = User(username="semantic-owner", password_hash="unused")
        db.add(owner); db.flush()
        gateway = Gateway(name="semantic-host", owner_id=owner.id, transport_kind="connector",
                          rest_url="http://unused.invalid", ws_url="ws://unused.invalid")
        db.add(gateway); db.flush()
        connector = Connector(owner_id=owner.id, gateway_id=gateway.id, name="Host", profiles=["default"], token_hash="semantic-token")
        db.add(connector)
        row = SessionLink(owner_id=owner.id, gateway_id=gateway.id, profile_name="default", stored_session_id="durable", title="Keep me")
        db.add(row)
        OpenAIIntegrationService(vault).set_api_key(db, owner, "sk-unit-semantic-postgresql")
        db.commit()
        owner_id, session_id, connector_id = owner.id, row.id, connector.id
    migrate(pg_url)
    service = SemanticSearch(SimpleNamespace(settings=cfg, vault=vault, session_factory=factory))
    service.settings(owner_id, True)
    service.reconcile()
    ready = Barrier(2)
    def claim(_):
        ready.wait(timeout=10)
        return service.claim()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(claim, range(2)))
    assert sum(job is not None for job in jobs) == 1
    job = next(job for job in jobs if job)
    vector = np.zeros(DIMENSIONS, dtype=np.float32); vector[0] = 1
    content = "bicicleta privada"
    assert service.save_fragments(job, "text", [(content, hashlib.sha256(content.encode()).hexdigest(), None, vector)])
    service.finish_page(job, publish=True)
    assert service.rank(owner_id, vector, 20)["items"][0]["targetId"] == session_id
    with factory() as db:
        assert db.get(SessionLink, session_id).title == "Keep me"
        fragment = db.scalar(select(SemanticFragment))
        assert content not in fragment.payload_ciphertext
        db.get(Connector, connector_id).revoked_at = datetime.now(timezone.utc)
        db.commit()
    assert service.rank(owner_id, vector, 20)["items"] == []
    with factory() as db:
        db.delete(db.get(SessionLink, session_id)); db.commit()
        assert db.scalar(select(func.count()).select_from(SemanticFragment)) == 0
        assert db.get(SemanticIndexState, session_id) is None
    engine.dispose()
