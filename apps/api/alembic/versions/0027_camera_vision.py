"""Owner-scoped camera preferences, encrypted observations and request receipts."""
from alembic import op
import sqlalchemy as sa

revision = "0027_camera_vision"
down_revision = "0026_background_tasks"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    op.create_table(
        "vision_preferences",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model_id", sa.String(40), nullable=False),
        sa.Column("interval_seconds", sa.Integer, nullable=False),
        sa.Column("active_request_id", sa.String(36)),
        sa.Column("busy_until", sa.DateTime(timezone=True)),
        sa.Column("last_analysis_at", sa.DateTime(timezone=True)),
        sa.Column("last_intent_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.CheckConstraint("model_id IN ('gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol')", name="ck_vision_model"),
        sa.CheckConstraint("interval_seconds IN (2, 5, 10)", name="ck_vision_interval"),
    )
    op.create_table(
        "vision_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("session_link_id", sa.String(36), nullable=False),
        sa.Column("activation_id", sa.String(36), nullable=False),
        sa.Column("payload_ciphertext", sa.Text, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.ForeignKeyConstraint(["session_link_id", "owner_id"], ["session_links.id", "session_links.owner_id"], ondelete="CASCADE", name="fk_vision_observation_session_owner"),
        sa.CheckConstraint("payload_ciphertext LIKE 'v1.%'", name="ck_vision_observation_encrypted"),
    )
    op.create_index("ix_vision_observation_session_created", "vision_observations", ["session_link_id", "created_at", "id"])
    op.create_index("ix_vision_observation_activation", "vision_observations", ["owner_id", "session_link_id", "activation_id"])
    op.create_table(
        "vision_request_receipts",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), primary_key=True),
        sa.Column("session_link_id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(12), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result_ciphertext", sa.Text),
        sa.Column("result_expires_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.ForeignKeyConstraint(["session_link_id", "owner_id"], ["session_links.id", "session_links.owner_id"], ondelete="CASCADE", name="fk_vision_receipt_session_owner"),
        sa.CheckConstraint("kind IN ('intent', 'analysis')", name="ck_vision_receipt_kind"),
        sa.CheckConstraint("state IN ('in_progress', 'completed', 'failed', 'expired')", name="ck_vision_receipt_state"),
        sa.CheckConstraint("result_ciphertext IS NULL OR result_ciphertext LIKE 'v1.%'", name="ck_vision_receipt_encrypted"),
    )
    op.create_index("ix_vision_receipt_expiry", "vision_request_receipts", ["owner_id", "result_expires_at"])


def downgrade():
    op.drop_table("vision_request_receipts")
    op.drop_table("vision_observations")
    op.drop_table("vision_preferences")
