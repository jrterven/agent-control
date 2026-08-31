"""Add unread chat state and encrypted Web Push subscriptions.

Revision ID: 0016_chat_notifications
Revises: 0015_session_pinning
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_chat_notifications"
down_revision = "0015_session_pinning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("session_links") as batch_op:
        batch_op.add_column(
            sa.Column(
                "last_activity_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "unread",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute(
        "UPDATE session_links SET last_activity_at = updated_at "
        "WHERE last_activity_at IS NULL"
    )
    with op.batch_alter_table("session_links") as batch_op:
        batch_op.alter_column("last_activity_at", nullable=False)
        batch_op.create_index(
            "ix_session_links_last_activity_at",
            ["last_activity_at"],
            unique=False,
        )

    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint_hash", sa.String(length=64), nullable=False),
        sa.Column("subscription_ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "endpoint_hash",
            name="uq_push_subscriptions_owner_endpoint",
        ),
    )
    op.create_index(
        "ix_push_subscriptions_owner_updated",
        "push_subscriptions",
        ["owner_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_push_subscriptions_owner_updated",
        table_name="push_subscriptions",
    )
    op.drop_table("push_subscriptions")
    with op.batch_alter_table("session_links") as batch_op:
        batch_op.drop_index("ix_session_links_last_activity_at")
        batch_op.drop_column("unread")
        batch_op.drop_column("last_activity_at")
