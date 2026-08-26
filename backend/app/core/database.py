"""Platform metadata database wiring (SQLAlchemy 2.0)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for every platform metadata table."""


def _engine_kwargs(url: str) -> dict[str, Any]:
    if url.startswith("sqlite"):
        # check_same_thread=False so FastAPI's threadpool can share the engine.
        return {"connect_args": {"check_same_thread": False}, "future": True}
    return {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10, "future": True}


engine: Engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """SQLite ignores FK constraints unless asked to enforce them.

    Tenant isolation leans on FK integrity, so this is not optional.
    """
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a platform DB session."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
