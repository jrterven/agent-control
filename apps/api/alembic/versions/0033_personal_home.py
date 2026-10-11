"""Personal Home configuration and durable derived insight records."""
from alembic import op
import sqlalchemy as sa

revision = "0033_personal_home"
down_revision = "0032_connector_updates"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def owner(primary=False):
    return sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=primary)


def upgrade():
    op.create_table("home_preferences", owner(True), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("layout", sa.JSON(), nullable=False),
        *[sa.Column(name, sa.DateTime(timezone=True)) for name in ("next_review_at", "last_checked_at", "last_analysis_at", "lease_until")],
        sa.Column("source_digest", sa.String(64)), sa.Column("last_error", sa.String(80)), sa.Column("lease_id", sa.String(36)), *timestamps())
    op.create_index("ix_home_preferences_next_review_at", "home_preferences", ["next_review_at"])
    op.create_table("home_panel_snapshots", sa.Column("id", sa.String(36), primary_key=True), owner(), sa.Column("panel_id", sa.String(80), nullable=False),
        sa.Column("definition_digest", sa.String(64), nullable=False), sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("content_ciphertext", sa.Text(), nullable=False), sa.Column("error_code", sa.String(80)), *timestamps(),
        sa.UniqueConstraint("owner_id", "panel_id", name="uq_home_panel_owner"))
    op.create_table("home_suggestions", sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("identity_key", sa.String(64), nullable=False), sa.Column("source_key", sa.String(200), nullable=False), sa.Column("action_key", sa.String(60), nullable=False),
        sa.Column("content_ciphertext", sa.Text(), nullable=False), sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("priority", sa.Integer(), nullable=False),
        *[sa.Column(name, sa.DateTime(timezone=True)) for name in ("due_at", "snoozed_until", "last_notified_at", "seen_at")],
        sa.Column("notified_day", sa.String(10)), sa.Column("urgent_day", sa.String(10)), *timestamps(),
        sa.UniqueConstraint("owner_id", "identity_key", name="uq_home_suggestion_identity"))
    op.create_table("home_reviews", sa.Column("id", sa.String(36), primary_key=True), owner(), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("status", sa.String(30), nullable=False), sa.Column("model", sa.String(40), nullable=False),
        sa.Column("error_code", sa.String(80)), sa.Column("input_tokens", sa.Integer(), nullable=False), sa.Column("output_tokens", sa.Integer(), nullable=False))
    for table in ("home_panel_snapshots", "home_suggestions", "home_reviews"):
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])


def downgrade():
    for table in ("home_reviews", "home_suggestions", "home_panel_snapshots", "home_preferences"):
        op.drop_table(table)
