"""Add the owner-selected ElevenLabs text-to-speech model.

Revision ID: 0010_elevenlabs_tts_model
Revises: 0009_elevenlabs_tts_voice
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_elevenlabs_tts_model"
down_revision = "0009_elevenlabs_tts_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_integrations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "tts_model_id",
                sa.String(length=40),
                nullable=False,
                server_default="eleven_flash_v2_5",
            )
        )
        batch_op.create_check_constraint(
            "ck_user_integrations_supported_tts_model",
            "tts_model_id IN ('eleven_flash_v2_5', 'eleven_multilingual_v2')",
        )


def downgrade() -> None:
    with op.batch_alter_table("user_integrations") as batch_op:
        batch_op.drop_constraint(
            "ck_user_integrations_supported_tts_model", type_="check"
        )
        batch_op.drop_column("tts_model_id")
