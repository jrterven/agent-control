"""Persist Agent Control-created profile metadata and grants.

Revision ID: 0007_control_managed_profiles
Revises: 0006_automation_run_read
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_control_managed_profiles"
down_revision = "0006_automation_run_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("profile_refs") as batch:
        batch.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "managed_by_control",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("profile_refs") as batch:
        batch.drop_column("managed_by_control")
        batch.drop_column("description")
