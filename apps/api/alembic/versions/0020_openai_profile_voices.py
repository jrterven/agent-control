"""Add owner-scoped GPT-Live voice overrides per stable profile identity.

Revision ID: 0020_openai_profile_voices
Revises: 0019_openai_voice
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_openai_profile_voices"
down_revision = "0019_openai_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "openai_profile_voice_preferences",
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("profile_id", sa.String(36), nullable=False),
        sa.Column("openai_voice_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "profile_id"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profile_refs.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "openai_voice_id IN ('alloy', 'ash', 'ballad', 'beacon', 'bossa', 'cedar', "
            "'cinder', 'coral', 'delta', 'echo', 'gleam', 'marin', 'meridian', 'quartz', "
            "'ripple', 'sage', 'shimmer', 'stone', 'tempo', 'verse', 'vesper', 'willow')",
            name="ck_openai_profile_voice_supported",
        ),
    )
    op.create_index("ix_openai_profile_voice_profile_id", "openai_profile_voice_preferences", ["profile_id"])


def downgrade() -> None:
    op.drop_index("ix_openai_profile_voice_profile_id", table_name="openai_profile_voice_preferences")
    op.drop_table("openai_profile_voice_preferences")
