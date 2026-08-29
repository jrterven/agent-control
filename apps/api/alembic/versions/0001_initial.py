"""Initial Agent Control schema.

Revision ID: 0001_initial
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "gateways",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("rest_url", sa.Text(), nullable=False),
        sa.Column("ws_url", sa.Text(), nullable=False),
        sa.Column("api_url", sa.Text(), nullable=True),
        sa.Column("connection_mode", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("env_managed", sa.Boolean(), nullable=False),
        sa.Column("health_status", sa.String(length=20), nullable=False),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.String(length=80), nullable=True),
        sa.Column("source_sha", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_auth_sessions_user_id", "auth_sessions", ["user_id"], unique=False
    )

    op.create_table(
        "gateway_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=36), nullable=False),
        sa.Column("dashboard_token_ciphertext", sa.Text(), nullable=True),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["gateway_id"], ["gateways.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_gateway_credentials_gateway_id",
        "gateway_credentials",
        ["gateway_id"],
        unique=True,
    )

    op.create_table(
        "profile_refs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=36), nullable=False),
        sa.Column("profile_name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["gateway_id"], ["gateways.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gateway_id", "profile_name"),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=16), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspaces_owner_id", "workspaces", ["owner_id"], unique=False
    )

    op.create_table(
        "session_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("profile_name", sa.String(length=120), nullable=False),
        sa.Column("stored_session_id", sa.String(length=255), nullable=False),
        sa.Column("runtime_session_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("replay_epoch", sa.String(length=100), nullable=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["gateway_id"], ["gateways.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_id", name="uq_session_links_id_owner"),
        sa.UniqueConstraint(
            "gateway_id",
            "profile_name",
            "stored_session_id",
            name="uq_session_links_upstream_route",
        ),
    )
    op.create_index(
        "ix_session_links_owner_id", "session_links", ["owner_id"], unique=False
    )
    op.create_index(
        "ix_session_links_workspace_id",
        "session_links",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_route",
        "session_links",
        ["gateway_id", "profile_name", "stored_session_id"],
        unique=False,
    )

    op.create_table(
        "tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.String(length=120), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "color IS NULL OR length(color) = 7", name="ck_tags_color_length"
        ),
        sa.CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 120", name="ck_tags_name_length"
        ),
        sa.CheckConstraint(
            "length(normalized_name) BETWEEN 1 AND 120",
            name="ck_tags_normalized_name_length",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_id", name="uq_tags_id_owner"),
        sa.UniqueConstraint(
            "owner_id",
            "normalized_name",
            name="uq_tags_owner_normalized_name",
        ),
    )
    op.create_index(
        "ix_tags_owner_name", "tags", ["owner_id", "normalized_name"], unique=False
    )

    op.create_table(
        "session_tags",
        sa.Column("session_link_id", sa.String(length=36), nullable=False),
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_link_id", "owner_id"],
            ["session_links.id", "session_links.owner_id"],
            name="fk_session_tags_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id", "owner_id"],
            ["tags.id", "tags.owner_id"],
            name="fk_session_tags_tag_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_link_id", "tag_id"),
    )
    op.create_index(
        "ix_session_tags_owner_tag",
        "session_tags",
        ["owner_id", "tag_id"],
        unique=False,
    )

    op.create_table(
        "attachment_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("session_link_id", sa.String(length=36), nullable=False),
        sa.Column("upstream_attachment_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_attachment_refs_nonnegative_size",
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) BETWEEN 1 AND 255 "
            "AND display_name NOT LIKE '%/%' "
            "AND display_name NOT LIKE '%\\%'",
            name="ck_attachment_refs_safe_display_name",
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR length(sha256) = 64",
            name="ck_attachment_refs_sha256_length",
        ),
        sa.CheckConstraint(
            "source IN ('hermes', 'user-reference')",
            name="ck_attachment_refs_source",
        ),
        sa.ForeignKeyConstraint(
            ["session_link_id", "owner_id"],
            ["session_links.id", "session_links.owner_id"],
            name="fk_attachment_refs_session_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_link_id",
            "upstream_attachment_id",
            name="uq_attachment_refs_session_upstream",
        ),
    )
    op.create_index(
        "ix_attachment_refs_owner_session",
        "attachment_references",
        ["owner_id", "session_link_id"],
        unique=False,
    )

    op.create_table(
        "drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("session_link_id", sa.String(length=36), nullable=False),
        sa.Column("content_ciphertext", sa.Text(), nullable=False),
        sa.Column("content_size", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "content_size BETWEEN 0 AND 200000", name="ck_drafts_content_size"
        ),
        sa.CheckConstraint(
            "content_ciphertext LIKE 'v1.%'", name="ck_drafts_encrypted_envelope"
        ),
        sa.CheckConstraint("version >= 1", name="ck_drafts_version"),
        sa.ForeignKeyConstraint(
            ["session_link_id", "owner_id"],
            ["session_links.id", "session_links.owner_id"],
            name="fk_drafts_session_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "session_link_id", name="uq_drafts_owner_session"
        ),
    )
    op.create_index("ix_drafts_expires_at", "drafts", ["expires_at"], unique=False)
    op.create_index(
        "ix_drafts_owner_updated",
        "drafts",
        ["owner_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "automations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("gateway_id", sa.String(length=36), nullable=False),
        sa.Column("profile_name", sa.String(length=120), nullable=False),
        sa.Column("hermes_automation_id", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("schedule", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_runs", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["gateway_id"], ["gateways.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automations_owner_id", "automations", ["owner_id"], unique=False
    )

    op.create_table(
        "automation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("automation_id", sa.String(length=36), nullable=False),
        sa.Column("hermes_run_id", sa.String(length=255), nullable=True),
        sa.Column("session_link_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["automation_id"], ["automations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_link_id"], ["session_links.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "hermes_run_id",
            name="uq_automation_runs_automation_hermes_run",
        ),
    )
    op.create_index(
        "ix_automation_runs_automation_created",
        "automation_runs",
        ["automation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_runs_automation_id",
        "automation_runs",
        ["automation_id"],
        unique=False,
    )
    op.create_index(
        "ix_automation_runs_session_link",
        "automation_runs",
        ["session_link_id"],
        unique=False,
    )

    op.create_table(
        "idempotency_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "scope", "idempotency_key"),
    )

    op.create_table(
        "realtime_tickets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("auth_session_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_session_id"], ["auth_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_realtime_tickets_auth_session_id",
        "realtime_tickets",
        ["auth_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_realtime_tickets_token_hash",
        "realtime_tickets",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_realtime_tickets_user_id",
        "realtime_tickets",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=160), nullable=False),
        sa.Column("target_type", sa.String(length=80), nullable=True),
        sa.Column("target_id", sa.String(length=255), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_action", "audit_events", ["action"], unique=False
    )
    op.create_index(
        "ix_audit_events_actor_user_id",
        "audit_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_created_at", "audit_events", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_realtime_tickets_user_id", table_name="realtime_tickets")
    op.drop_index("ix_realtime_tickets_auth_session_id", table_name="realtime_tickets")
    op.drop_index("ix_realtime_tickets_token_hash", table_name="realtime_tickets")
    op.drop_table("realtime_tickets")

    op.drop_table("idempotency_operations")

    op.drop_index("ix_automation_runs_session_link", table_name="automation_runs")
    op.drop_index("ix_automation_runs_automation_id", table_name="automation_runs")
    op.drop_index(
        "ix_automation_runs_automation_created", table_name="automation_runs"
    )
    op.drop_table("automation_runs")

    op.drop_index("ix_automations_owner_id", table_name="automations")
    op.drop_table("automations")

    op.drop_index("ix_drafts_owner_updated", table_name="drafts")
    op.drop_index("ix_drafts_expires_at", table_name="drafts")
    op.drop_table("drafts")

    op.drop_index(
        "ix_attachment_refs_owner_session", table_name="attachment_references"
    )
    op.drop_table("attachment_references")

    op.drop_index("ix_session_tags_owner_tag", table_name="session_tags")
    op.drop_table("session_tags")

    op.drop_index("ix_tags_owner_name", table_name="tags")
    op.drop_table("tags")

    op.drop_index("ix_session_route", table_name="session_links")
    op.drop_index("ix_session_links_workspace_id", table_name="session_links")
    op.drop_index("ix_session_links_owner_id", table_name="session_links")
    op.drop_table("session_links")

    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_table("workspaces")

    op.drop_table("profile_refs")

    op.drop_index(
        "ix_gateway_credentials_gateway_id", table_name="gateway_credentials"
    )
    op.drop_table("gateway_credentials")

    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_table("gateways")

    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
