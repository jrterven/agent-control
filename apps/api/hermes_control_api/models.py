from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class User(Base, Timestamped):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    user: Mapped[User] = relationship()


class Gateway(Base, Timestamped):
    __tablename__ = "gateways"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    rest_url: Mapped[str] = mapped_column(Text)
    ws_url: Mapped[str] = mapped_column(Text)
    api_url: Mapped[str | None] = mapped_column(Text)
    connection_mode: Mapped[str] = mapped_column(String(20), default="private")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    env_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str | None] = mapped_column(String(80))
    source_sha: Mapped[str | None] = mapped_column(String(80))


class GatewayCredential(Base, Timestamped):
    __tablename__ = "gateway_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    gateway_id: Mapped[str] = mapped_column(
        ForeignKey("gateways.id", ondelete="CASCADE"), unique=True, index=True
    )
    dashboard_token_ciphertext: Mapped[str | None] = mapped_column(Text)
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    # Operator trust anchor. It is encrypted and write-only at the API
    # boundary; Gateway.source_sha remains untrusted diagnostic metadata.
    trusted_source_sha_ciphertext: Mapped[str | None] = mapped_column(Text)


class ProfileRef(Base, Timestamped):
    __tablename__ = "profile_refs"
    __table_args__ = (UniqueConstraint("gateway_id", "profile_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    gateway_id: Mapped[str] = mapped_column(ForeignKey("gateways.id", ondelete="CASCADE"))
    profile_name: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    managed_by_control: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    model: Mapped[str | None] = mapped_column(String(200))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Connectivity observations and capability verification have different
    # trust lifetimes. A heartbeat may refresh last_seen_at, but it must never
    # extend an old capability assertion.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capabilities_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class Workspace(Base, Timestamped):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(String(16))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionLink(Base, Timestamped):
    __tablename__ = "session_links"
    __table_args__ = (
        UniqueConstraint(
            "gateway_id",
            "profile_name",
            "stored_session_id",
            name="uq_session_links_upstream_route",
        ),
        UniqueConstraint(
            "gateway_id",
            "profile_name",
            "runtime_session_id",
            name="uq_session_links_runtime_route",
        ),
        UniqueConstraint("id", "owner_id", name="uq_session_links_id_owner"),
        Index("ix_session_route", "gateway_id", "profile_name", "stored_session_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    gateway_id: Mapped[str] = mapped_column(ForeignKey("gateways.id", ondelete="RESTRICT"))
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), index=True
    )
    profile_name: Mapped[str] = mapped_column(String(120))
    stored_session_id: Mapped[str] = mapped_column(String(255))
    runtime_session_id: Mapped[str | None] = mapped_column(String(255))
    runtime_generation: Mapped[str | None] = mapped_column(String(96))
    title: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30), default="idle")
    # Hermes 0.20.5/0.20.6 does not persist an RPC-created session until its
    # first prompt. This marker is the sole authority for treating that one
    # expected history 404 as an empty pre-dispatch boundary.
    initial_history_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    replay_epoch: Mapped[str | None] = mapped_column(String(100))
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Tag(Base, Timestamped):
    """Owner-scoped label; normalized_name provides deterministic uniqueness."""

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("owner_id", "normalized_name", name="uq_tags_owner_normalized_name"),
        UniqueConstraint("id", "owner_id", name="uq_tags_id_owner"),
        CheckConstraint("length(trim(name)) BETWEEN 1 AND 120", name="ck_tags_name_length"),
        CheckConstraint(
            "length(normalized_name) BETWEEN 1 AND 120",
            name="ck_tags_normalized_name_length",
        ),
        CheckConstraint("color IS NULL OR length(color) = 7", name="ck_tags_color_length"),
        Index("ix_tags_owner_name", "owner_id", "normalized_name"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    normalized_name: Mapped[str] = mapped_column(String(120))
    color: Mapped[str | None] = mapped_column(String(7))

    @validates("name")
    def normalize_name(self, key: str, value: str) -> str:
        clean = " ".join(value.strip().split())
        self.normalized_name = unicodedata.normalize("NFKC", clean).casefold()
        return clean



@event.listens_for(Tag, "before_insert")
@event.listens_for(Tag, "before_update")
def enforce_tag_normalization(mapper, connection, target: Tag) -> None:
    clean = " ".join(target.name.strip().split())
    target.name = clean
    target.normalized_name = unicodedata.normalize("NFKC", clean).casefold()


class SessionTag(Base):
    """Many-to-many link that enforces a shared owner at the database level."""

    __tablename__ = "session_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_link_id", "owner_id"],
            ["session_links.id", "session_links.owner_id"],
            ondelete="CASCADE",
            name="fk_session_tags_session_owner",
        ),
        ForeignKeyConstraint(
            ["tag_id", "owner_id"],
            ["tags.id", "tags.owner_id"],
            ondelete="CASCADE",
            name="fk_session_tags_tag_owner",
        ),
        Index("ix_session_tags_owner_tag", "owner_id", "tag_id"),
    )
    session_link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tag_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AttachmentReference(Base, Timestamped):
    """Metadata-only pointer to an attachment owned and stored by Hermes."""

    __tablename__ = "attachment_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_link_id", "owner_id"],
            ["session_links.id", "session_links.owner_id"],
            ondelete="CASCADE",
            name="fk_attachment_refs_session_owner",
        ),
        UniqueConstraint(
            "session_link_id",
            "upstream_attachment_id",
            name="uq_attachment_refs_session_upstream",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_attachment_refs_nonnegative_size",
        ),
        CheckConstraint(
            "sha256 IS NULL OR length(sha256) = 64",
            name="ck_attachment_refs_sha256_length",
        ),
        CheckConstraint(
            "source IN ('hermes', 'user-reference')",
            name="ck_attachment_refs_source",
        ),
        CheckConstraint(
            "length(trim(display_name)) BETWEEN 1 AND 255 "
            "AND display_name NOT LIKE '%/%' "
            "AND display_name NOT LIKE '%\\%'",
            name="ck_attachment_refs_safe_display_name",
        ),
        Index("ix_attachment_refs_owner_session", "owner_id", "session_link_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_link_id: Mapped[str] = mapped_column(String(36), nullable=False)
    upstream_attachment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(30), default="hermes")


class Draft(Base, Timestamped):
    """Single encrypted composer draft per owner/session; never a message copy."""

    __tablename__ = "drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_link_id", "owner_id"],
            ["session_links.id", "session_links.owner_id"],
            ondelete="CASCADE",
            name="fk_drafts_session_owner",
        ),
        UniqueConstraint("owner_id", "session_link_id", name="uq_drafts_owner_session"),
        CheckConstraint(
            "content_ciphertext LIKE 'v1.%'",
            name="ck_drafts_encrypted_envelope",
        ),
        CheckConstraint(
            "content_size BETWEEN 0 AND 200000",
            name="ck_drafts_content_size",
        ),
        CheckConstraint("version >= 1", name="ck_drafts_version"),
        Index("ix_drafts_owner_updated", "owner_id", "updated_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_link_id: Mapped[str] = mapped_column(String(36), nullable=False)
    content_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    content_size: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Automation(Base, Timestamped):
    __tablename__ = "automations"
    __table_args__ = (
        UniqueConstraint(
            "gateway_id",
            "profile_name",
            "hermes_automation_id",
            name="uq_automations_upstream_route",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    gateway_id: Mapped[str] = mapped_column(ForeignKey("gateways.id", ondelete="RESTRICT"))
    profile_name: Mapped[str] = mapped_column(String(120))
    hermes_automation_id: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(200))
    schedule: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_runs: Mapped[list[str]] = mapped_column(JSON, default=list)


class AutomationRun(Base, Timestamped):
    __tablename__ = "automation_runs"
    __table_args__ = (
        UniqueConstraint(
            "automation_id",
            "hermes_run_id",
            name="uq_automation_runs_automation_hermes_run",
        ),
        Index(
            "ix_automation_runs_automation_created",
            "automation_id",
            "created_at",
        ),
        Index("ix_automation_runs_session_link", "session_link_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    automation_id: Mapped[str] = mapped_column(
        ForeignKey("automations.id", ondelete="CASCADE"), index=True
    )
    hermes_run_id: Mapped[str | None] = mapped_column(String(255))
    session_link_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_links.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyOperation(Base):
    __tablename__ = "idempotency_operations"
    __table_args__ = (UniqueConstraint("user_id", "scope", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RealtimeTicket(Base):
    __tablename__ = "realtime_tickets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    auth_session_id: Mapped[str] = mapped_column(ForeignKey("auth_sessions.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(160), index=True)
    target_type: Mapped[str | None] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(30), default="success")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
