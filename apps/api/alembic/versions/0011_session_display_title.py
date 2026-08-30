"""Add an owner-selected display title for session links.

Revision ID: 0011_session_display_title
Revises: 0010_elevenlabs_tts_model
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_session_display_title"
down_revision = "0010_elevenlabs_tts_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("session_links") as batch_op:
        batch_op.add_column(sa.Column("display_title", sa.String(length=300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("session_links") as batch_op:
        batch_op.drop_column("display_title")
