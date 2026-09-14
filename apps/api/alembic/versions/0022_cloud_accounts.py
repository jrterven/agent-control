"""Cloud identity, invite-only enrollment and gateway ownership."""
from alembic import op
import sqlalchemy as sa

revision = "0022_cloud_accounts"
down_revision = "0021_live_transcripts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gateways") as batch:
        batch.add_column(sa.Column("owner_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("transport_kind", sa.String(20), nullable=False, server_default="direct"))
        batch.create_foreign_key("fk_gateway_owner", "users", ["owner_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_gateways_owner_id", ["owner_id"])
        batch.create_check_constraint("ck_gateway_transport", "transport_kind IN ('direct', 'connector')")
    # Private installations retain their administrator and all resource IDs.
    op.execute(sa.text("UPDATE gateways SET owner_id = (SELECT id FROM users WHERE is_admin = true ORDER BY created_at, id LIMIT 1) WHERE owner_id IS NULL"))
    op.create_table(
        "external_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="uq_external_identity_subject"),
    )
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])
    op.create_table(
        "beta_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_beta_invitations_email", "beta_invitations", ["email"], unique=True)
    op.create_table(
        "oidc_flows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("verifier_ciphertext", sa.Text(), nullable=False),
        sa.Column("return_to", sa.String(2048), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_oidc_flows_state_hash", "oidc_flows", ["state_hash"], unique=True)
    op.create_index("ix_oidc_flows_expires_at", "oidc_flows", ["expires_at"])


def downgrade() -> None:
    op.drop_table("oidc_flows")
    op.drop_table("beta_invitations")
    op.drop_table("external_identities")
    with op.batch_alter_table("gateways") as batch:
        batch.drop_constraint("ck_gateway_transport", type_="check")
        batch.drop_constraint("fk_gateway_owner", type_="foreignkey")
        batch.drop_index("ix_gateways_owner_id")
        batch.drop_column("transport_kind")
        batch.drop_column("owner_id")
