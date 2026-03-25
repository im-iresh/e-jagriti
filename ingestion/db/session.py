"""SQLAlchemy engine and session factory for the ingestion service."""

import os
from contextlib import contextmanager
from typing import Generator

import structlog
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger = structlog.get_logger(__name__)


def _build_engine(url: str, pool_size: int = 5, max_overflow: int = 10):
    """Create a SQLAlchemy engine with connection pooling."""
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_connection, connection_record):  # noqa: ANN001
        """Set default search path on each new connection."""
        cursor = dbapi_connection.cursor()
        cursor.execute("SET search_path TO public")
        cursor.close()

    return engine


# Primary (read-write) engine
_DATABASE_URL: str = os.environ["DATABASE_URL"]
engine = _build_engine(_DATABASE_URL)

# Optional read replica: if REPLICA_DATABASE_URL is set use it for SELECTs,
# otherwise fall back silently to the primary engine.
_REPLICA_URL: str | None = os.environ.get("REPLICA_DATABASE_URL") or None
replica_engine = _build_engine(_REPLICA_URL) if _REPLICA_URL else engine

SessionFactory: sessionmaker = sessionmaker(bind=engine, expire_on_commit=False)
ReplicaSessionFactory: sessionmaker = sessionmaker(bind=replica_engine, expire_on_commit=False)


@contextmanager
def get_session(read_only: bool = False) -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session, committing on success and rolling back on error.

    Args:
        read_only: When True, use the replica engine for SELECT queries.

    Yields:
        An active SQLAlchemy Session.
    """
    factory = ReplicaSessionFactory if read_only else SessionFactory
    session: Session = factory()
    try:
        yield session
        if not read_only:
            session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("db_session_error", error=str(exc))
        raise
    finally:
        session.close()


def check_db_connection() -> bool:
    """Return True if the primary DB is reachable, False otherwise."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("db_health_check_failed", error=str(exc))
        return False
