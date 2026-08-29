"""Track the one lazy-persisted history boundary after session.create.

Revision ID: 0003_initial_history_boundary
Revises: 0002_runtime_identity
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_initial_history_boundary"
down_revision = "0002_runtime_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("session_links") as batch:
        batch.add_column(
            sa.Column(
                "initial_history_pending",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("session_links") as batch:
        batch.drop_column("initial_history_pending")
