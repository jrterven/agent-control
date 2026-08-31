"""Add Control-only session pinning.

Revision ID: 0015_session_pinning
Revises: 0014_profile_voice_preferences
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_session_pinning"
down_revision = "0014_profile_voice_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("session_links") as batch_op:
        batch_op.add_column(
            sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("session_links") as batch_op:
        batch_op.drop_column("pinned_at")
