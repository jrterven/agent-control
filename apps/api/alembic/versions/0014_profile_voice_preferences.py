"""Add owner-scoped profile voice preferences.

Revision ID: 0014_profile_voice_preferences
Revises: 0013_profile_avatars
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_profile_voice_preferences"
down_revision = "0013_profile_avatars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile_voice_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("integration_id", sa.String(length=36), nullable=False),
        sa.Column("profile_id", sa.String(length=36), nullable=False),
        sa.Column("api_key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("tts_voice_id", sa.String(length=128), nullable=False),
        sa.Column("tts_voice_name", sa.String(length=200), nullable=False),
        sa.Column(
            "tts_model_id",
            sa.String(length=40),
            server_default="eleven_flash_v2_5",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "tts_model_id IN ('eleven_flash_v2_5', 'eleven_multilingual_v2')",
            name="ck_profile_voice_preferences_supported_tts_model",
        ),
        sa.CheckConstraint(
            "length(api_key_fingerprint) = 64",
            name="ck_profile_voice_preferences_key_fingerprint_length",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["user_integrations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profile_refs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "integration_id",
            "profile_id",
            name="uq_profile_voice_preferences_integration_profile",
        ),
    )
    op.create_index(
        "ix_profile_voice_preferences_profile_id",
        "profile_voice_preferences",
        ["profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_profile_voice_preferences_profile_id",
        table_name="profile_voice_preferences",
    )
    op.drop_table("profile_voice_preferences")
