from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from hermes_control_api.config import Settings
from hermes_control_api.database import Base, build_engine, build_session_factory
from hermes_control_api.models import (
    AttachmentReference,
    Draft,
    Gateway,
    SessionLink,
    SessionTag,
    Tag,
    User,
    UserIntegration,
)
from hermes_control_api.security import SecretVault


REPO = Path(__file__).resolve().parents[2]
INITIAL_MIGRATION = REPO / "apps" / "api" / "alembic" / "versions" / "0001_initial.py"
APPLICATION_TABLES = {
    "attachment_references",
    "audit_events",
    "auth_sessions",
    "automation_runs",
    "automations",
    "drafts",
    "gateway_credentials",
    "gateways",
    "idempotency_operations",
    "profile_refs",
    "realtime_tickets",
    "session_links",
    "session_tags",
    "tags",
    "users",
    "user_integrations",
    "workspaces",
}


def test_initial_alembic_schema_is_explicit_and_reversible(tmp_path):
    migration_source = INITIAL_MIGRATION.read_text(encoding="utf-8")
    assert "create_all" not in migration_source
    assert "drop_all" not in migration_source

    database_path = tmp_path / "migration.db"
    environment = os.environ.copy()
    environment.update(
        {
            "HERMES_CONTROL_ENVIRONMENT": "test",
            "HERMES_CONTROL_DATABASE_URL": f"sqlite:///{database_path}",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(REPO / "apps" / "api"),
                    str(REPO / "packages" / "hermes-client"),
                    environment.get("PYTHONPATH", ""),
                ]
            ),
        }
    )
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(REPO / "apps" / "api" / "alembic.ini"),
        "upgrade",
        "head",
    ]
    result = subprocess.run(
        command,
        cwd=REPO / "apps" / "api",
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    # ``upgrade head`` is the development and production startup contract. It
    # must be safe to execute again against an already-current database.
    idempotent_upgrade = subprocess.run(
        command,
        cwd=REPO / "apps" / "api",
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert idempotent_upgrade.returncode == 0, idempotent_upgrade.stderr

    engine = create_engine(f"sqlite:///{database_path}")
    schema = inspect(engine)
    table_names = set(schema.get_table_names())
    assert table_names - {"alembic_version"} == APPLICATION_TABLES
    assert {"tags", "session_tags", "attachment_references", "drafts"} <= table_names
    assert not any("message" in table_name for table_name in table_names)

    draft_columns = {column["name"] for column in schema.get_columns("drafts")}
    assert "content_ciphertext" in draft_columns
    assert "content" not in draft_columns
    attachment_columns = {
        column["name"] for column in schema.get_columns("attachment_references")
    }
    assert not ({"blob", "content", "url", "path", "secret"} & attachment_columns)
    credential_columns = {
        column["name"] for column in schema.get_columns("gateway_credentials")
    }
    assert "trusted_source_sha_ciphertext" in credential_columns
    assert "trusted_source_sha" not in credential_columns
    integration_columns = {
        column["name"] for column in schema.get_columns("user_integrations")
    }
    assert "api_key_ciphertext" in integration_columns
    assert "api_key" not in integration_columns

    session_tag_fks = {
        fk["name"]: (tuple(fk["constrained_columns"]), tuple(fk["referred_columns"]))
        for fk in schema.get_foreign_keys("session_tags")
    }
    assert session_tag_fks["fk_session_tags_session_owner"] == (
        ("session_link_id", "owner_id"),
        ("id", "owner_id"),
    )
    assert session_tag_fks["fk_session_tags_tag_owner"] == (
        ("tag_id", "owner_id"),
        ("id", "owner_id"),
    )

    unique_names = {
        constraint["name"]
        for table in (
            "tags",
            "attachment_references",
            "drafts",
            "session_links",
            "automations",
        )
        for constraint in schema.get_unique_constraints(table)
    }
    assert {
        "uq_tags_owner_normalized_name",
        "uq_attachment_refs_session_upstream",
        "uq_drafts_owner_session",
        "uq_session_links_runtime_route",
        "uq_automations_upstream_route",
    } <= unique_names
    assert "runtime_generation" in {
        column["name"] for column in schema.get_columns("session_links")
    }
    assert "initial_history_pending" in {
        column["name"] for column in schema.get_columns("session_links")
    }
    assert "capabilities_checked_at" in {
        column["name"] for column in schema.get_columns("profile_refs")
    }
    assert {"description", "managed_by_control"} <= {
        column["name"] for column in schema.get_columns("profile_refs")
    }
    assert "read_at" in {
        column["name"] for column in schema.get_columns("automation_runs")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == "0008_user_integrations"
    engine.dispose()

    downgrade = subprocess.run(
        [*command[:-2], "downgrade", "base"],
        cwd=REPO / "apps" / "api",
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert downgrade.returncode == 0, downgrade.stderr
    downgraded_engine = create_engine(f"sqlite:///{database_path}")
    downgraded = inspect(downgraded_engine)
    assert not (APPLICATION_TABLES & set(downgraded.get_table_names()))
    downgraded_engine.dispose()

    second_upgrade = subprocess.run(
        command,
        cwd=REPO / "apps" / "api",
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second_upgrade.returncode == 0, second_upgrade.stderr
    upgraded_again_engine = create_engine(f"sqlite:///{database_path}")
    upgraded_again = inspect(upgraded_again_engine)
    assert set(upgraded_again.get_table_names()) - {"alembic_version"} == APPLICATION_TABLES
    upgraded_again_engine.dispose()


def _expect_integrity_error(db, row) -> None:
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_metadata_constraints_enforce_owner_isolation_encryption_and_cascade():
    settings = Settings(environment="test", database_url="sqlite://")
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as db:
        owner = User(username="owner", password_hash="unused")
        other = User(username="other", password_hash="unused")
        gateway = Gateway(
            name="metadata-gateway",
            rest_url="http://127.0.0.1:19119",
            ws_url="ws://127.0.0.1:19119/api/ws",
        )
        db.add_all([owner, other, gateway])
        db.flush()
        integration_envelope = SecretVault(b"d" * 32).encrypt(
            "owner-only-key",
            aad=f"user-integration:{owner.id}:elevenlabs:api-key",
        )
        db.add(
            UserIntegration(
                owner_id=owner.id,
                provider="elevenlabs",
                api_key_ciphertext=integration_envelope,
            )
        )
        db.commit()
        _expect_integrity_error(
            db,
            UserIntegration(
                owner_id=other.id,
                provider="elevenlabs",
                api_key_ciphertext="plaintext-key",
            ),
        )
        session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="default",
            stored_session_id="stored-owner",
        )
        db.add(session)
        db.flush()

        tag = Tag(owner_id=owner.id, name="Research Papers", color="#7C5CFC")
        other_tag = Tag(owner_id=other.id, name="Research Papers", color="#7C5CFC")
        db.add_all([tag, other_tag])
        db.flush()
        db.add(SessionTag(session_link_id=session.id, tag_id=tag.id, owner_id=owner.id))
        db.commit()

        _expect_integrity_error(
            db,
            Tag(owner_id=owner.id, name="  research   papers ", color="#7C5CFC"),
        )
        _expect_integrity_error(
            db,
            SessionTag(session_link_id=session.id, tag_id=other_tag.id, owner_id=owner.id),
        )
        _expect_integrity_error(
            db,
            AttachmentReference(
                owner_id=other.id,
                session_link_id=session.id,
                upstream_attachment_id="wrong-owner",
                display_name="paper.pdf",
                size_bytes=10,
            ),
        )
        _expect_integrity_error(
            db,
            AttachmentReference(
                owner_id=owner.id,
                session_link_id=session.id,
                upstream_attachment_id="unsafe-path",
                display_name="/home/hermes/secret.txt",
                size_bytes=10,
            ),
        )
        _expect_integrity_error(
            db,
            Draft(
                owner_id=owner.id,
                session_link_id=session.id,
                content_ciphertext="plaintext draft",
                content_size=15,
            ),
        )

        attachment = AttachmentReference(
            owner_id=owner.id,
            session_link_id=session.id,
            upstream_attachment_id="hermes-attachment-1",
            display_name="paper.pdf",
            media_type="application/pdf",
            size_bytes=1024,
            sha256="a" * 64,
        )
        envelope = SecretVault(b"d" * 32).encrypt(
            "unfinished prompt", aad=f"draft:{owner.id}:{session.id}"
        )
        draft = Draft(
            owner_id=owner.id,
            session_link_id=session.id,
            content_ciphertext=envelope,
            content_size=len("unfinished prompt".encode()),
        )
        db.add_all([attachment, draft])
        db.commit()

        tag.name = "  Updated   Research  "
        tag.normalized_name = "attempted-override"
        db.commit()
        assert tag.name == "Updated Research"
        assert tag.normalized_name == "updated research"

        _expect_integrity_error(
            db,
            Draft(
                owner_id=owner.id,
                session_link_id=session.id,
                content_ciphertext=envelope,
                content_size=1,
            ),
        )

        db.delete(session)
        db.commit()
        assert db.scalars(select(SessionTag)).all() == []
        assert db.scalars(select(AttachmentReference)).all() == []
        assert db.scalars(select(Draft)).all() == []
        assert db.get(Tag, tag.id) is not None
    engine.dispose()
