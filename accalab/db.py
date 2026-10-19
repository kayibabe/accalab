"""SQLAlchemy engine and session helpers."""

from __future__ import annotations

import os

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from accalab.config import ROOT, _load_dotenv


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Persist UTC timestamps and restore their timezone after SQLite reads."""

    impl = DateTime
    cache_ok = True

    def __init__(self, timezone: bool = True) -> None:
        super().__init__(timezone=timezone)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from datetime import UTC

        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, _dialect):
        if value is None:
            return None
        from datetime import UTC

        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def database_url() -> str:
    _load_dotenv(ROOT / ".env")
    url = os.getenv("DATABASE_URL", "sqlite:///data/accalab.sqlite3")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        db_path = ROOT / url.removeprefix("sqlite:///")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path.as_posix()}"
    return url


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    resolved = url or database_url()
    connect_args = {"check_same_thread": False} if resolved.startswith("sqlite:") else {}
    engine = create_engine(resolved, echo=echo, connect_args=connect_args, future=True)
    if resolved.startswith("sqlite:"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
