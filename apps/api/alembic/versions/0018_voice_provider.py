"""Add owner-scoped voice mode selection without changing existing credentials.

Revision ID: 0018_voice_provider
Revises: 0017_email_reference_cache
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_voice_provider"
down_revision = "0017_email_reference_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_voice_preferences",
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('elevenlabs', 'openai_live')",
            name="ck_user_voice_preferences_provider",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id"),
    )


def downgrade() -> None:
    op.drop_table("user_voice_preferences")
