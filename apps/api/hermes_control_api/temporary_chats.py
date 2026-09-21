"""Owner/tab-scoped, bounded session stores with no durable conversation data."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import secrets
import asyncio
from collections import OrderedDict
import threading
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, select, event, or_, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from .database import Base
from .models import SessionLink
from .eventing import EventHub

LEASE_SECONDS = 300
MAX_OWNER_CHATS = 4

# Copy routing/configuration only. Messages, drafts, media, audit events and
# receipts start empty and can never be copied back to the primary database.
REFERENCE_TABLES = frozenset({
    "users", "auth_sessions", "gateways", "gateway_credentials", "profile_refs",
    "workspaces", "user_integrations", "user_voice_preferences",
    "profile_voice_preferences", "openai_profile_voice_preferences", "vision_preferences",
    "connectors", "connector_profiles", "mail_accounts", "mail_agents", "mail_grants",
})


@dataclass
class TemporaryChat:
    owner_id: str
    access: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires_at: float = field(default_factory=lambda: time.monotonic() + LEASE_SECONDS)
    id: str | None = None
    route: tuple[str, str, str] | None = None
    closed: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    media: dict = field(default_factory=dict, repr=False)
    media_service: object = field(default=None, repr=False)
    events: EventHub = field(default_factory=EventHub, repr=False)
    reconciler: object = field(default=None, repr=False)

    def __post_init__(self):
        self.engine = create_engine("sqlite:///file:ac-" + uuid4().hex + "?mode=memory&cache=shared&uri=true", connect_args={"check_same_thread": False}, poolclass=QueuePool, pool_size=8, max_overflow=0)
        @event.listens_for(self.engine, "connect")
        def memory_only(connection, record):
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA max_page_count=32768")
        self.anchor = self.engine.connect()
        # Ephemeral schema, deliberately never an on-disk Control database.
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def session(self):
        if self.closed:
            raise HTTPException(410, "TEMPORARY_CHAT_ENDED")
        with self.factory() as db:
            db.info["temporary_chat"] = self
            yield db

    def refresh_references(self, source):
        with self.session() as target:
            for mapper in Base.registry.mappers:
                model = mapper.class_
                table = mapper.local_table
                if table.name not in REFERENCE_TABLES:
                    continue
                query = select(model)
                if hasattr(model, "owner_id"):
                    query = query.where(or_(model.owner_id == self.owner_id, model.owner_id.is_(None)))
                elif table.name == "users":
                    query = query.where(model.id == self.owner_id)
                elif table.name == "auth_sessions":
                    query = query.where(model.user_id == self.owner_id)
                # Revoked grants/configuration must disappear too; merge alone
                # would retain rows that the primary store has deleted.
                target.execute(delete(model))
                for row in source.scalars(query):
                    clone = model(**{column.key: getattr(row, column.key) for column in mapper.columns})
                    target.merge(clone)
            target.commit()

    def dispose(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.reconciler is not None:
                for task in self.reconciler.tasks.values():
                    task.cancel()
            self.media.clear()
            self.events.clear()
            self.anchor.close()
            self.engine.dispose()


class TemporaryChats:
    def __init__(self):
        self.entries: dict[str, TemporaryChat] = {}
        self._lock = threading.RLock()
        self.creation_lock = asyncio.Lock()
        self.creations = OrderedDict()

    def create(self, source, owner_id):
        with self._lock:
            if sum(entry.owner_id == owner_id for entry in self.entries.values()) >= MAX_OWNER_CHATS:
                raise HTTPException(429, "TEMPORARY_CHAT_LIMIT")
            chat = TemporaryChat(owner_id)
            chat.id = "tmp_" + uuid4().hex
            self.entries[chat.id] = chat
        try:
            chat.refresh_references(source)
            return chat
        except BaseException:
            self.remove(chat)
            raise

    def bind(self, chat, row: SessionLink):
        with self._lock:
            chat.id = row.id
            chat.route = (row.gateway_id, row.profile_name, row.stored_session_id)
            self.entries[row.id] = chat

    def owned(self, session_id, owner_id, access):
        with self._lock:
            chat = self.entries.get(session_id)
            if chat is None:
                return None
            if chat.owner_id != owner_id or not secrets.compare_digest(chat.access, access or ""):
                raise HTTPException(404, "Conversation not found")
            if chat.closed or time.monotonic() >= chat.expires_at:
                raise HTTPException(410, "TEMPORARY_CHAT_ENDED")
            return chat

    def for_event(self, event):
        return self.for_route(event.gateway_id, event.profile_name, event.stored_session_id)

    def for_route(self, gateway_id, profile_name, stored_session_id):
        route = (gateway_id, profile_name, stored_session_id)
        with self._lock:
            return next((chat for chat in self.entries.values() if chat.route == route and not chat.closed and time.monotonic() < chat.expires_at), None)

    async def reap(self, services):
        import asyncio
        from .services import SessionService
        service = SessionService(services)
        while True:
            await asyncio.sleep(30)
            for chat in list(self.entries.values()):
                if time.monotonic() < chat.expires_at:
                    continue
                try:
                    with chat.session() as db:
                        row = db.get(SessionLink, chat.id)
                        if row:
                            connection = await service.gateways.connection(db, row.gateway_id, row.profile_name)
                            provider = await services.provider_pool.get(connection)
                            await asyncio.wait_for(provider.close_temporary_session(service._route(row)), 5)
                except Exception:
                    pass  # Independent native lease handles an offline connector.
                finally:
                    self.remove(chat)

    def remove(self, chat):
        with self._lock:
            if chat.id:
                self.entries.pop(chat.id, None)
        chat.dispose()

    def dispose(self):
        with self._lock:
            for chat in self.entries.values():
                chat.dispose()
            self.entries.clear()
