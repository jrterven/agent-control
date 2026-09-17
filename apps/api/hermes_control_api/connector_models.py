from __future__ import annotations
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import Timestamped, new_id, utc_now


class Connector(Base, Timestamped):
    __tablename__ = "connectors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    gateway_id: Mapped[str] = mapped_column(ForeignKey("gateways.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    profiles: Mapped[list] = mapped_column(JSON)
    version: Mapped[str | None] = mapped_column(String(80))
    installation_kind: Mapped[str | None] = mapped_column(String(16))
    hermes_version: Mapped[str | None] = mapped_column(String(80))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceAuthorization(Base):
    __tablename__ = "connector_device_authorizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    device_code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    profiles: Mapped[list] = mapped_column(JSON)
    version: Mapped[str | None] = mapped_column(String(80))
    source_sha: Mapped[str] = mapped_column(String(40))
    installation_kind: Mapped[str | None] = mapped_column(String(16))
    hermes_version: Mapped[str | None] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    connector_id: Mapped[str | None] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
