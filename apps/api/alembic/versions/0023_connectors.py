"""Personal connector credentials and expiring device authorization."""
from alembic import op
import sqlalchemy as sa
revision = "0023_connectors"
down_revision = "0022_cloud_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("connectors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gateway_id", sa.String(36), sa.ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("profiles", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(80)), sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_connectors_owner_id", "connectors", ["owner_id"])
    op.create_index("ix_connectors_token_hash", "connectors", ["token_hash"], unique=True)
    op.create_table("connector_device_authorizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("device_code_hash", sa.String(64), nullable=False),
        sa.Column("user_code_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False), sa.Column("profiles", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(80)), sa.Column("source_sha", sa.String(40), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("connector_id", sa.String(36), sa.ForeignKey("connectors.id", ondelete="CASCADE")),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for field in ("device_code_hash", "user_code_hash", "expires_at"):
        op.create_index(f"ix_connector_device_authorizations_{field}", "connector_device_authorizations", [field], unique=field != "expires_at")


def downgrade():
    op.drop_table("connector_device_authorizations")
    op.drop_table("connectors")
