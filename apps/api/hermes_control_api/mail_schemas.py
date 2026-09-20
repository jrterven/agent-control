from __future__ import annotations

import re
from typing import Literal
from uuid import UUID
from pydantic import Field, SecretStr, field_validator
from .schemas import ApiModel


def address(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[^\s<>@,;\x00-\x1f]{1,128}@[^\s<>@,;\x00-\x1f]{1,190}\.[^\s<>@,;\x00-\x1f]{2,63}", value):
        raise ValueError("Invalid email address")
    return value


class MailAccountInput(ApiModel):
    provider: Literal["hostinger", "imap"]
    address: str = Field(max_length=320)
    label: str = Field(default="", max_length=120)
    service: Literal["hostinger", "titan", "custom"] = "custom"
    username: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)
    imap_host: str = Field(default="", max_length=253)
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: Literal[465, 587] = 465
    account_id: UUID | None = None

    @field_validator("address")
    @classmethod
    def valid_address(cls, value):
        return address(value)

    @field_validator("username", "label", "imap_host", "smtp_host")
    @classmethod
    def printable(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("Control characters are not allowed")
        return value.strip()


class MailAccountUpdate(ApiModel):
    label: str = Field(min_length=1, max_length=120)
    profile_ids: list[UUID] = Field(default_factory=list, max_length=64)


class MailOAuthStart(ApiModel):
    account_id: UUID | None = None


class MailSearch(ApiModel):
    account_id: UUID
    text: str = Field(default="", max_length=200)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("text")
    @classmethod
    def safe_text(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("Control characters are not allowed")
        return value


class MailRead(ApiModel):
    account_id: UUID
    message_id: str = Field(min_length=1, max_length=1024)


class MailSend(ApiModel):
    account_id: UUID
    operation_id: UUID
    to: list[str] = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=48000)
    reply_to_message_id: str | None = Field(default=None, max_length=1024)
    user_requested_send: Literal[True]

    @field_validator("to")
    @classmethod
    def recipients(cls, value):
        return [address(item) for item in value]

    @field_validator("subject", "reply_to_message_id")
    @classmethod
    def header(cls, value):
        if value is not None and any(ord(c) < 32 for c in value):
            raise ValueError("Control characters are not allowed")
        return value
