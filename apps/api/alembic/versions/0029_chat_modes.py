"""Persist immutable chat policy for saved conversations."""
from alembic import op
import sqlalchemy as sa

revision = "0029_chat_modes"
down_revision = "0028_mail_plugins"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("session_links") as batch:
        batch.add_column(sa.Column("chat_mode", sa.String(24), nullable=False, server_default="memory_read_write"))
        batch.create_check_constraint("ck_session_chat_mode", "chat_mode IN ('memory_read_write', 'memory_read_only', 'temporary')")


def downgrade():
    with op.batch_alter_table("session_links") as batch:
        batch.drop_constraint("ck_session_chat_mode", type_="check")
        batch.drop_column("chat_mode")
