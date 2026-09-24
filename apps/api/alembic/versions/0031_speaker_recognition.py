"""Encrypted voiceprints and bounded pyannote pilot receipts."""
from alembic import op
import sqlalchemy as sa

revision = "0031_speaker_recognition"
down_revision = "0030_semantic_search"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def owner(primary=False):
    return sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=primary)


def upgrade():
    op.create_table("speaker_preferences", owner(True),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False), sa.Column("tested", sa.Boolean(), nullable=False),
        sa.Column("busy_job_id", sa.String(36)), sa.Column("last_submission", sa.Float(), nullable=False), *timestamps())
    op.create_table("voice_people", sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("name", sa.String(100), nullable=False), sa.Column("model", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False), sa.Column("voiceprint_ciphertext", sa.Text()),
        sa.Column("consent", sa.Boolean(), nullable=False), *timestamps())
    op.create_table("voice_captures", sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("auth_session_id", sa.String(36), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False), sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("session_id", sa.String(100)), sa.Column("person_id", sa.String(36)),
        sa.Column("active", sa.Boolean(), nullable=False), *timestamps())
    op.create_table("speaker_jobs", sa.Column("id", sa.String(36), primary_key=True), owner(),
        sa.Column("capture_id", sa.String(36), sa.ForeignKey("voice_captures.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False), sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("provider_job_id", sa.String(100)),
        sa.Column("submission_attempted", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("billed_seconds", sa.Float(), nullable=False), sa.Column("voiceprints_created", sa.Integer(), nullable=False),
        sa.Column("result_ciphertext", sa.Text()), sa.Column("timings", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64)), sa.Column("feedback", sa.String(16)),
        sa.Column("expected_person_id", sa.String(36)), *timestamps())
    for table in ("voice_people", "voice_captures", "speaker_jobs"):
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
    op.create_index("ix_speaker_jobs_capture_id", "speaker_jobs", ["capture_id"])


def downgrade():
    for table in ("speaker_jobs", "voice_captures", "voice_people", "speaker_preferences"):
        op.drop_table(table)
