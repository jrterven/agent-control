"""Separate profile connectivity from capability verification time.

Revision ID: 0005_profile_capability_time
Revises: 0004_gateway_trusted_sha
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_profile_capability_time"
down_revision = "0004_gateway_trusted_sha"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("profile_refs") as batch:
        batch.add_column(
            sa.Column("capabilities_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
    # Before this split, last_seen_at was written only when capabilities were
    # verified. Preserve that bounded cache age during upgrade. Subsequent
    # connection heartbeats update last_seen_at independently.
    op.execute(
        "UPDATE profile_refs "
        "SET capabilities_checked_at = last_seen_at "
        "WHERE capabilities_checked_at IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("profile_refs") as batch:
        batch.drop_column("capabilities_checked_at")
