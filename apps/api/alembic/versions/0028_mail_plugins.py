"""Encrypted mail accounts and native MCP profile grants."""
from alembic import op
import sqlalchemy as sa

revision = "0028_mail_plugins"
down_revision = "0027_camera_vision"
branch_labels = depends_on = None


def owner():
    return sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    op.create_table("mail_accounts",
        sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("identity", sa.String(64), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("credential_ciphertext", sa.Text, nullable=False),
        sa.Column("status", sa.String(30), nullable=False), *timestamps(),
        sa.UniqueConstraint("owner_id", "provider", "identity", name="uq_mail_account_identity"))
    op.create_table("mail_agents",
        sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("profile_refs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_ciphertext", sa.Text, nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)), *timestamps(),
        sa.UniqueConstraint("owner_id", "profile_id", name="uq_mail_agent_profile"))
    op.create_index("ix_mail_agents_token_hash", "mail_agents", ["token_hash"], unique=True)
    op.create_table("mail_grants",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("mail_accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("mail_agents.id", ondelete="CASCADE"), primary_key=True))
    op.create_table("mail_oauth_flows",
        sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("verifier_ciphertext", sa.Text, nullable=False),
        sa.Column("account_id", sa.String(36)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_table("mail_send_operations",
        sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("mail_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "operation_id", name="uq_mail_send_operation"))
    for table in ("mail_accounts", "mail_agents", "mail_oauth_flows", "mail_send_operations"):
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])


def downgrade():
    for table in ("mail_send_operations", "mail_oauth_flows", "mail_grants", "mail_agents", "mail_accounts"):
        op.drop_table(table)
