"""Opt-in integration checks against disposable schemas in a real PostgreSQL DB.

Set AGENT_CONTROL_TEST_POSTGRES_URL to a dedicated validation database. Each
case creates and removes only its own random schema; public is never modified.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
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
from hermes_control_api.models import BetaInvitation, ExternalIdentity, Gateway, ProfileRef, SessionLink, User

REPO = Path(__file__).resolve().parents[2]
POSTGRES = os.environ.get("AGENT_CONTROL_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES, reason="Set AGENT_CONTROL_TEST_POSTGRES_URL for real PostgreSQL checks")


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
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0023_connectors"
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
