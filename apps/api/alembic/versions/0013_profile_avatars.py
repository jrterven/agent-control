"""Add optional Control-owned profile avatars.

Revision ID: 0013_profile_avatars
Revises: 0012_automation_workspace
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_profile_avatars"
down_revision = "0012_automation_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("profile_refs") as batch_op:
        batch_op.add_column(sa.Column("avatar_mime_type", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("avatar_data", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("profile_refs") as batch_op:
        batch_op.drop_column("avatar_data")
        batch_op.drop_column("avatar_mime_type")
