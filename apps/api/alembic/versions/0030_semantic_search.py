"""Encrypted, owner-scoped semantic search and resumable indexing."""
from alembic import op
import sqlalchemy as sa

revision = "0030_semantic_search"
down_revision = "0029_chat_modes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("semantic_preferences",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64)), sa.Column("credential_version", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("semantic_index_states",
        sa.Column("session_link_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("indexed_version", sa.String(64)),
        sa.Column("active_generation", sa.String(36)), sa.Column("building_generation", sa.String(36)),
        sa.Column("history_offset", sa.Integer(), nullable=False),
        sa.Column("history_complete", sa.Boolean(), nullable=False),
        sa.Column("history_tail_ciphertext", sa.Text()),
        sa.Column("live_offset", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)), sa.Column("error_code", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("lease_token", sa.String(36)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_link_id", "owner_id"], ["session_links.id", "session_links.owner_id"],
                                ondelete="CASCADE", name="fk_semantic_state_session_owner"))
    op.create_index("ix_semantic_index_states_owner_id", "semantic_index_states", ["owner_id"])
    op.create_table("semantic_fragments",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("session_link_id", sa.String(36), nullable=False), sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("source", sa.String(16), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False), sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("payload_ciphertext", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_link_id", "owner_id"], ["session_links.id", "session_links.owner_id"],
                                ondelete="CASCADE", name="fk_semantic_fragment_session_owner"),
        sa.UniqueConstraint("session_link_id", "generation", "source", "content_hash", name="uq_semantic_fragment"),
        sa.CheckConstraint("payload_ciphertext LIKE 'v1.%'", name="ck_semantic_fragment_encrypted"))
    op.create_index("ix_semantic_fragment_owner_generation", "semantic_fragments", ["owner_id", "session_link_id", "generation"])


def downgrade():
    op.drop_table("semantic_fragments")
    op.drop_table("semantic_index_states")
    op.drop_table("semantic_preferences")
