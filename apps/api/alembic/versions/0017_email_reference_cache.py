"""Add encrypted, session-scoped email reference cache.

Revision ID: 0017_email_reference_cache
Revises: 0016_chat_notifications
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_email_reference_cache"
down_revision = "0016_chat_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_reference_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("session_link_id", sa.String(length=36), nullable=False),
        sa.Column("reference_id", sa.String(length=32), nullable=False),
        sa.Column("payload_ciphertext", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(reference_id) = 32",
            name="ck_email_reference_cache_reference_id_length",
        ),
        sa.CheckConstraint(
            "payload_ciphertext LIKE 'v1.%'",
            name="ck_email_reference_cache_encrypted_payload",
        ),
        sa.ForeignKeyConstraint(
            ["session_link_id", "owner_id"],
            ["session_links.id", "session_links.owner_id"],
            ondelete="CASCADE",
            name="fk_email_reference_cache_session_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_link_id",
            "reference_id",
            name="uq_email_reference_cache_session_reference",
        ),
    )
    op.create_index(
        "ix_email_reference_cache_owner_session",
        "email_reference_cache",
        ["owner_id", "session_link_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_reference_cache_expires_at",
        "email_reference_cache",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_reference_cache_expires_at",
        table_name="email_reference_cache",
    )
    op.drop_index(
        "ix_email_reference_cache_owner_session",
        table_name="email_reference_cache",
    )
    op.drop_table("email_reference_cache")
