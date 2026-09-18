"""Durable private image assets and deleted-conversation route tombstones."""
from alembic import op
import sqlalchemy as sa

revision = "0025_visual_media"
down_revision = "0024_managed_installation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "visual_media",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("gateway_id", sa.String(36), nullable=False),
        sa.Column("profile_name", sa.String(120), nullable=False),
        sa.Column("stored_session_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("thumbnail_hash", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("thumbnail_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(32), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("alt", sa.String(1000), nullable=False),
        sa.Column("caption", sa.String(2000)),
        sa.Column("source_url", sa.String(2048)),
        sa.Column("source_title", sa.String(300)),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'ready', 'failed')", name="ck_visual_media_status"),
    )
    op.create_index("ix_visual_media_owner_id", "visual_media", ["owner_id"])
    op.create_index("ix_visual_media_deleted_at", "visual_media", ["deleted_at"])
    op.create_index("ix_visual_media_route", "visual_media", ["owner_id", "gateway_id", "profile_name", "stored_session_id"])
    op.create_table(
        "visual_media_route_tombstones",
        sa.Column("owner_id", sa.String(36), primary_key=True),
        sa.Column("gateway_id", sa.String(36), primary_key=True),
        sa.Column("profile_name", sa.String(120), primary_key=True),
        sa.Column("stored_session_id", sa.String(255), primary_key=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("visual_media_route_tombstones")
    op.drop_table("visual_media")
