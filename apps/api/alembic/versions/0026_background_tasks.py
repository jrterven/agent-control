"""Persist bounded task snapshots and independent native turn identity."""
from alembic import op
import sqlalchemy as sa

revision = "0026_background_tasks"
down_revision = "0025_visual_media"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("session_links", sa.Column("background_tasks", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("session_links", sa.Column("active_turn_id", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("session_links", "active_turn_id")
    op.drop_column("session_links", "background_tasks")
