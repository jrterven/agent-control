"""Optional, diagnostic metadata for managed Hermes installations."""
from alembic import op
import sqlalchemy as sa

revision = "0024_managed_installation"
down_revision = "0023_connectors"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("connectors", "connector_device_authorizations"):
        op.add_column(table, sa.Column("installation_kind", sa.String(16), nullable=True))
        op.add_column(table, sa.Column("hermes_version", sa.String(80), nullable=True))


def downgrade():
    for table in ("connector_device_authorizations", "connectors"):
        op.drop_column(table, "hermes_version")
        op.drop_column(table, "installation_kind")
