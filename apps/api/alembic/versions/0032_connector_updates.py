"""Owner update preferences and bounded connector release diagnostics."""
from alembic import op
import sqlalchemy as sa

revision = "0032_connector_updates"
down_revision = "0031_speaker_recognition"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("connectors", sa.Column("update_settings", sa.JSON(), nullable=True))
    op.add_column("connectors", sa.Column("update_status", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("connectors", "update_status")
    op.drop_column("connectors", "update_settings")
