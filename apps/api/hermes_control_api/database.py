from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings


class Base(DeclarativeBase):
    pass


def build_engine(settings: Settings):
    kwargs: dict = {"pool_pre_ping": True}
    if settings.deployment_mode == "cloud" and settings.database_url.startswith("postgresql"):
        # HTTP admission is capped at 20 before authentication. Keep room for
        # nested request sessions, connector replies, probes and supervisors.
        # A synchronous checkout must NEVER wait on the event loop: the holder
        # may itself be awaiting a connector reply or dependency cleanup there.
        kwargs.update(pool_size=64, max_overflow=0, pool_timeout=0)
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if settings.database_url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    database_url = settings.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(database_url, **kwargs)
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
    return engine


def build_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def session_dependency(factory: sessionmaker[Session]):
    def dependency() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    return dependency
