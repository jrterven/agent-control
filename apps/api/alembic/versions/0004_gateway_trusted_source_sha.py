"""Store an encrypted operator trust anchor per gateway.

Revision ID: 0004_gateway_trusted_sha
Revises: 0003_initial_history_boundary
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_gateway_trusted_sha"
down_revision = "0003_initial_history_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gateway_credentials") as batch:
        batch.add_column(
            sa.Column("trusted_source_sha_ciphertext", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("gateway_credentials") as batch:
        batch.drop_column("trusted_source_sha_ciphertext")
