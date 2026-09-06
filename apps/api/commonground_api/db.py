"""Database engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


def normalise_url(url: str) -> str:
    """Accept the plain `postgresql://` URL every host hands out.

    Neon, Render and psql all produce `postgresql://`, but SQLAlchemy needs to
    be told which driver to use. Rewriting it here means one DATABASE_URL works
    for db/migrate.py, psql and the app alike, instead of the deployment needing
    two spellings of the same string.
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        normalise_url(settings.database_url),
        # Neon's free tier scales compute to zero, so a pooled connection can be
        # dead by the time it is reused. pre_ping costs a round trip and turns
        # a confusing OperationalError into a transparent reconnect.
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        future=True,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    with get_sessionmaker()() as session:
        yield session
