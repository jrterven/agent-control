from __future__ import annotations

import hashlib
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
    ProfileRef,
    ProfileVoicePreference,
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
    "profile_voice_preferences",
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
    assert {"tts_voice_id", "tts_voice_name", "tts_model_id"} <= integration_columns
    profile_voice_columns = {
        column["name"]
        for column in schema.get_columns("profile_voice_preferences")
    }
    assert {
        "integration_id",
        "profile_id",
        "api_key_fingerprint",
        "tts_voice_id",
        "tts_voice_name",
        "tts_model_id",
        "created_at",
        "updated_at",
    } <= profile_voice_columns
    profile_voice_fks = {
        tuple(foreign_key["constrained_columns"]): foreign_key
        for foreign_key in schema.get_foreign_keys("profile_voice_preferences")
    }
    assert profile_voice_fks[("integration_id",)]["referred_table"] == "user_integrations"
    assert profile_voice_fks[("integration_id",)]["options"].get("ondelete") == "CASCADE"
    assert profile_voice_fks[("profile_id",)]["referred_table"] == "profile_refs"
    assert profile_voice_fks[("profile_id",)]["options"].get("ondelete") == "CASCADE"
    assert "uq_profile_voice_preferences_integration_profile" in {
        constraint["name"]
        for constraint in schema.get_unique_constraints("profile_voice_preferences")
    }
    assert "ck_profile_voice_preferences_key_fingerprint_length" in {
        constraint["name"]
        for constraint in schema.get_check_constraints(
            "profile_voice_preferences"
        )
    }

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
    assert "display_title" in {
        column["name"] for column in schema.get_columns("session_links")
    }
    assert "pinned_at" in {
        column["name"] for column in schema.get_columns("session_links")
    }
    assert "capabilities_checked_at" in {
        column["name"] for column in schema.get_columns("profile_refs")
    }
    assert {"description", "managed_by_control"} <= {
        column["name"] for column in schema.get_columns("profile_refs")
    }
    assert {"avatar_mime_type", "avatar_data"} <= {
        column["name"] for column in schema.get_columns("profile_refs")
    }
    assert "read_at" in {
        column["name"] for column in schema.get_columns("automation_runs")
    }
    assert "workspace_id" in {
        column["name"] for column in schema.get_columns("automations")
    }
    automation_workspace_fk = next(
        foreign_key
        for foreign_key in schema.get_foreign_keys("automations")
        if foreign_key["constrained_columns"] == ["workspace_id"]
    )
    assert automation_workspace_fk["referred_table"] == "workspaces"
    assert automation_workspace_fk["options"].get("ondelete") == "SET NULL"
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == "0015_session_pinning"
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
        profile = ProfileRef(
            gateway_id=gateway.id,
            profile_name="default",
            display_name="Newton",
        )
        secondary_profile = ProfileRef(
            gateway_id=gateway.id,
            profile_name="jarvis",
            display_name="Jarvis",
        )
        integration_envelope = SecretVault(b"d" * 32).encrypt(
            "owner-only-key",
            aad=f"user-integration:{owner.id}:elevenlabs:api-key",
        )
        integration = UserIntegration(
            owner_id=owner.id,
            provider="elevenlabs",
            api_key_ciphertext=integration_envelope,
        )
        db.add_all([profile, secondary_profile, integration])
        db.commit()
        preference = ProfileVoicePreference(
            integration_id=integration.id,
            profile_id=profile.id,
            api_key_fingerprint=hashlib.sha256(
                integration.api_key_ciphertext.encode("utf-8")
            ).hexdigest(),
            tts_voice_id="voice_alpha",
            tts_voice_name="Aria",
            tts_model_id="eleven_flash_v2_5",
        )
        db.add(preference)
        db.commit()
        _expect_integrity_error(
            db,
            ProfileVoicePreference(
                integration_id=integration.id,
                profile_id=profile.id,
                api_key_fingerprint=hashlib.sha256(
                    integration.api_key_ciphertext.encode("utf-8")
                ).hexdigest(),
                tts_voice_id="voice_beta",
                tts_voice_name="Brian",
            ),
        )
        _expect_integrity_error(
            db,
            ProfileVoicePreference(
                integration_id=integration.id,
                profile_id=secondary_profile.id,
                api_key_fingerprint=hashlib.sha256(
                    integration.api_key_ciphertext.encode("utf-8")
                ).hexdigest(),
                tts_voice_id="voice_alpha",
                tts_voice_name="Aria",
                tts_model_id="eleven_v3",
            ),
        )
        _expect_integrity_error(
            db,
            UserIntegration(
                owner_id=other.id,
                provider="elevenlabs",
                api_key_ciphertext="plaintext-key",
            ),
        )
        _expect_integrity_error(
            db,
            UserIntegration(
                owner_id=other.id,
                provider="elevenlabs",
                api_key_ciphertext=SecretVault(b"d" * 32).encrypt(
                    "other-owner-key",
                    aad=f"user-integration:{other.id}:elevenlabs:api-key",
                ),
                tts_model_id="eleven_v3",
            ),
        )
        other_integration = UserIntegration(
            owner_id=other.id,
            provider="elevenlabs",
            api_key_ciphertext=SecretVault(b"d" * 32).encrypt(
                "other-owner-key",
                aad=f"user-integration:{other.id}:elevenlabs:api-key",
            ),
        )
        db.add(other_integration)
        db.flush()
        # The exact uniqueness key includes the owner-scoped integration, so
        # two owners may configure the same infrastructure profile independently.
        db.add(
            ProfileVoicePreference(
                integration_id=other_integration.id,
                profile_id=profile.id,
                api_key_fingerprint=hashlib.sha256(
                    other_integration.api_key_ciphertext.encode("utf-8")
                ).hexdigest(),
                tts_voice_id="voice_beta",
                tts_voice_name="Brian",
            )
        )
        db.commit()
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
        db.delete(integration)
        db.commit()
        remaining_preferences = db.scalars(select(ProfileVoicePreference)).all()
        assert len(remaining_preferences) == 1
        assert remaining_preferences[0].integration_id == other_integration.id
        db.delete(other_integration)
        db.commit()
        assert db.scalars(select(ProfileVoicePreference)).all() == []
    engine.dispose()
