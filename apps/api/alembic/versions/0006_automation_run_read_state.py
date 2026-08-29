"""Persist whether an automation result has been read.

Revision ID: 0006_automation_run_read
Revises: 0005_profile_capability_time
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_automation_run_read"
down_revision = "0005_profile_capability_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("automation_runs") as batch:
        batch.add_column(
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("automation_runs") as batch:
        batch.drop_column("read_at")
