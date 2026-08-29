"""Add owner-scoped encrypted integration credentials.

Revision ID: 0008_user_integrations
Revises: 0007_control_managed_profiles
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_user_integrations"
down_revision = "0007_control_managed_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_integrations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "api_key_ciphertext LIKE 'v1.%'",
            name="ck_user_integrations_encrypted_api_key",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "provider",
            name="uq_user_integrations_owner_provider",
        ),
    )
    op.create_index(
        "ix_user_integrations_owner_provider",
        "user_integrations",
        ["owner_id", "provider"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_integrations_owner_provider",
        table_name="user_integrations",
    )
    op.drop_table("user_integrations")
