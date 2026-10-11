"""Daily notification receipts survive restarts and timezone changes."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import new_id


class HomeNotice(Base):
    __tablename__ = 'home_notices'
    __table_args__ = (UniqueConstraint('owner_id', 'suggestion_id', 'day', 'kind', name='uq_home_notice_daily'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    suggestion_id: Mapped[str] = mapped_column(ForeignKey('home_suggestions.id', ondelete='CASCADE'), index=True)
    day: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
