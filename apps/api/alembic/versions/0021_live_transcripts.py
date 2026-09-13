"""Preserve encrypted, owner-scoped Live conversation transcripts."""

from alembic import op
import sqlalchemy as sa

revision = "0021_live_transcripts"
down_revision = "0020_openai_profile_voices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_transcripts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("session_link_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_link_id", "owner_id"], ["session_links.id", "session_links.owner_id"],
            ondelete="CASCADE", name="fk_live_transcripts_session_owner",
        ),
        sa.CheckConstraint("revision > 0", name="ck_live_transcripts_revision"),
        sa.CheckConstraint("payload_ciphertext LIKE 'v1.%'", name="ck_live_transcripts_encrypted"),
    )
    op.create_index("ix_live_transcripts_session_created", "live_transcripts", ["session_link_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_live_transcripts_session_created", table_name="live_transcripts")
    op.drop_table("live_transcripts")
