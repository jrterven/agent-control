"""Add the owner-selected ElevenLabs text-to-speech voice.

Revision ID: 0009_elevenlabs_tts_voice
Revises: 0008_user_integrations
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_elevenlabs_tts_voice"
down_revision = "0008_user_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_integrations") as batch_op:
        batch_op.add_column(sa.Column("tts_voice_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("tts_voice_name", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_integrations") as batch_op:
        batch_op.drop_column("tts_voice_name")
        batch_op.drop_column("tts_voice_id")
