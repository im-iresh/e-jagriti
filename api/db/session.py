"""
SQLAlchemy engine and session factory for the Flask API service.

Read replica support:
  When REPLICA_DATABASE_URL is set, SELECT queries are routed to the replica
  engine. All writes go to the primary. Zero code change needed when a
  replica is added later — just set the env var.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

import structlog
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger = structlog.get_logger(__name__)


def _build_engine(url: str, pool_size: int = 5, max_overflow: int = 10):
    """
    Build a SQLAlchemy Engine with connection pooling.

    Args:
        url: PostgreSQL connection URL.
        pool_size: Persistent connections to keep open.
        max_overflow: Extra connections above pool_size when pool is full.

    Returns:
        Configured Engine instance.
    """
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        echo=False,
    )


# Engines are initialised lazily so test code can set DATABASE_URL before import.
_primary_engine = None
_replica_engine = None
_SessionFactory: sessionmaker | None = None
_ReplicaSessionFactory: sessionmaker | None = None


def _get_engines():
    """Lazily initialise engines from current environment variables."""
    global _primary_engine, _replica_engine, _SessionFactory, _ReplicaSessionFactory

    if _primary_engine is None:
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            raise RuntimeError("DATABASE_URL environment variable is not set")

        pool_size   = int(os.environ.get("SA_POOL_SIZE", "5"))
        max_overflow = int(os.environ.get("SA_MAX_OVERFLOW", "10"))

        _primary_engine = _build_engine(db_url, pool_size, max_overflow)
        _SessionFactory = sessionmaker(bind=_primary_engine, expire_on_commit=False)

        replica_url = os.environ.get("REPLICA_DATABASE_URL") or None
        if replica_url:
            _replica_engine = _build_engine(replica_url, pool_size, max_overflow)
            _ReplicaSessionFactory = sessionmaker(bind=_replica_engine, expire_on_commit=False)
            logger.info("replica_engine_configured")
        else:
            _replica_engine = _primary_engine
            _ReplicaSessionFactory = _SessionFactory

    return _primary_engine, _replica_engine, _SessionFactory, _ReplicaSessionFactory


@contextmanager
def get_session(read_only: bool = False) -> Generator[Session, None, None]:
    """
    Yield a SQLAlchemy Session, committing on success and rolling back on error.

    Args:
        read_only: Route to replica engine when True.

    Yields:
        Active SQLAlchemy Session.

    Raises:
        Re-raises any exception after rollback.
    """
    _, _, write_factory, read_factory = _get_engines()
    factory = read_factory if read_only else write_factory
    session: Session = factory()
    try:
        yield session
        if not read_only:
            session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("db_session_rollback", error=str(exc))
        raise
    finally:
        session.close()


def check_db_connection() -> bool:
    """
    Verify that the primary database is reachable.

    Returns:
        True if a SELECT 1 succeeds, False otherwise.
    """
    try:
        primary, _, _, _ = _get_engines()
        with primary.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("db_health_check_failed", error=str(exc))
        return False


def reset_engines() -> None:
    """
    Dispose all engines and reset module state.

    Used in tests to swap DATABASE_URL between test cases.
    """
    global _primary_engine, _replica_engine, _SessionFactory, _ReplicaSessionFactory
    if _primary_engine:
        _primary_engine.dispose()
    if _replica_engine and _replica_engine is not _primary_engine:
        _replica_engine.dispose()
    _primary_engine = _replica_engine = _SessionFactory = _ReplicaSessionFactory = None
