"""Owner-scoped pilot data. Audio is never stored in these tables."""
from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import Timestamped, new_id


class SpeakerPreference(Base, Timestamped):
    __tablename__ = "speaker_preferences"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    generation: Mapped[str] = mapped_column(String(36), default=new_id)
    window_seconds: Mapped[int] = mapped_column(Integer, default=5)
    tested: Mapped[bool] = mapped_column(Boolean, default=False)
    busy_job_id: Mapped[str | None] = mapped_column(String(36))
    last_submission: Mapped[float] = mapped_column(Float, default=0)


class VoicePerson(Base, Timestamped):
    __tablename__ = "voice_people"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(40), default="pyannote")
    model: Mapped[str] = mapped_column(String(40), default="precision-3")
    generation: Mapped[str] = mapped_column(String(36), default=new_id)
    voiceprint_ciphertext: Mapped[str | None] = mapped_column(Text)
    consent: Mapped[bool] = mapped_column(Boolean, default=True)


class VoiceCapture(Base, Timestamped):
    __tablename__ = "voice_captures"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    auth_session_id: Mapped[str] = mapped_column(ForeignKey("auth_sessions.id", ondelete="CASCADE"))
    generation: Mapped[str] = mapped_column(String(36))
    mode: Mapped[str] = mapped_column(String(16))
    session_id: Mapped[str | None] = mapped_column(String(100))
    person_id: Mapped[str | None] = mapped_column(String(36))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SpeakerJob(Base, Timestamped):
    __tablename__ = "speaker_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    capture_id: Mapped[str] = mapped_column(ForeignKey("voice_captures.id", ondelete="CASCADE"), index=True)
    generation: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    provider_job_id: Mapped[str | None] = mapped_column(String(100))
    submission_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    fingerprint: Mapped[str] = mapped_column(String(64))
    duration: Mapped[float] = mapped_column(Float)
    billed_seconds: Mapped[float] = mapped_column(Float, default=0)
    voiceprints_created: Mapped[int] = mapped_column(Integer, default=0)
    result_ciphertext: Mapped[str | None] = mapped_column(Text)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    feedback: Mapped[str | None] = mapped_column(String(16))
    expected_person_id: Mapped[str | None] = mapped_column(String(36))
