"""Persist the owner's GPT-Live built-in voice independently of credentials.

Revision ID: 0019_openai_voice
Revises: 0018_voice_provider
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_openai_voice"
down_revision = "0018_voice_provider"
branch_labels = None
depends_on = None

# Keep this historical migration independent from future voice catalogs.
VOICE_CHECK = (
    "openai_voice_id IN ('alloy', 'ash', 'ballad', 'beacon', 'bossa', 'cedar', "
    "'cinder', 'coral', 'delta', 'echo', 'gleam', 'marin', 'meridian', 'quartz', "
    "'ripple', 'sage', 'shimmer', 'stone', 'tempo', 'verse', 'vesper', 'willow')"
)


def upgrade() -> None:
    with op.batch_alter_table("user_voice_preferences") as batch_op:
        batch_op.add_column(
            sa.Column(
                "openai_voice_id", sa.String(length=32),
                nullable=False, server_default="marin",
            )
        )
        batch_op.create_check_constraint(
            "ck_user_voice_preferences_openai_voice", VOICE_CHECK
        )


def downgrade() -> None:
    with op.batch_alter_table("user_voice_preferences") as batch_op:
        batch_op.drop_constraint("ck_user_voice_preferences_openai_voice", type_="check")
        batch_op.drop_column("openai_voice_id")
