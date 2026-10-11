"""Owner-scoped Home configuration and derived, encrypted insight data."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import Timestamped, new_id


class HomePreference(Base, Timestamped):
    __tablename__ = "home_preferences"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    layout: Mapped[dict] = mapped_column(JSON, default=dict)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_proactive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_digest: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(80))
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HomePanelSnapshot(Base, Timestamped):
    __tablename__ = "home_panel_snapshots"
    __table_args__ = (UniqueConstraint("owner_id", "panel_id", name="uq_home_panel_owner"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    panel_id: Mapped[str] = mapped_column(String(80))
    definition_digest: Mapped[str] = mapped_column(String(64))
    source_digest: Mapped[str] = mapped_column(String(64))
    content_ciphertext: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(80))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HomeSuggestion(Base, Timestamped):
    __tablename__ = "home_suggestions"
    __table_args__ = (UniqueConstraint("owner_id", "identity_key", name="uq_home_suggestion_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    identity_key: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(200))
    action_key: Mapped[str] = mapped_column(String(60))
    content_ciphertext: Mapped[str] = mapped_column(Text)
    evidence_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="open")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_day: Mapped[str | None] = mapped_column(String(10))
    urgent_day: Mapped[str | None] = mapped_column(String(10))
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HomeReview(Base):
    __tablename__ = "home_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="checking")
    model: Mapped[str] = mapped_column(String(40), default="gpt-6-luna")
    error_code: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
