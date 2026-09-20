"""Control-owned mail metadata. Provider credentials never enter Hermes."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import Timestamped, new_id, utc_now


class MailAccount(Base, Timestamped):
    __tablename__ = "mail_accounts"
    __table_args__ = (UniqueConstraint("owner_id", "provider", "identity", name="uq_mail_account_identity"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    identity: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(320))
    label: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_ciphertext: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="connected")


class MailAgent(Base, Timestamped):
    __tablename__ = "mail_agents"
    __table_args__ = (UniqueConstraint("owner_id", "profile_id", name="uq_mail_agent_profile"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profile_refs.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_ciphertext: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MailGrant(Base):
    __tablename__ = "mail_grants"
    account_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("mail_agents.id", ondelete="CASCADE"), primary_key=True)


class MailOAuthFlow(Base):
    __tablename__ = "mail_oauth_flows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("auth_sessions.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(20))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    verifier_ciphertext: Mapped[str] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(String(36))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MailSendOperation(Base):
    __tablename__ = "mail_send_operations"
    __table_args__ = (UniqueConstraint("owner_id", "operation_id", name="uq_mail_send_operation"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id", ondelete="CASCADE"))
    operation_id: Mapped[str] = mapped_column(String(36))
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="delivery_unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
