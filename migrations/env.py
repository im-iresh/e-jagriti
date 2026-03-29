"""
Alembic environment configuration.

Supports both offline (--sql) and online migration modes.
Reads DATABASE_URL from the environment so migrations work identically
in local Docker, CI, and production without code changes.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Load .env from ingestion/ (local dev) or /app/ (Docker) — no-op if missing
_here = Path(__file__).parent
for _candidate in (_here / ".." / "ingestion" / ".env", _here / ".." / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# Make the ingestion package importable so Base and all models are found.
# Local dev: repo/migrations/../ingestion = repo/ingestion/  (db/models.py lives there)
# Docker:    /migrations/../ingestion doesn't exist; models are at /app/db/models.py
_migrations_dir = os.path.dirname(os.path.abspath(__file__))
_local_ingestion = os.path.join(_migrations_dir, "..", "ingestion")
_docker_app      = os.path.join(_migrations_dir, "..")
if os.path.isdir(os.path.join(_local_ingestion, "db")):
    sys.path.insert(0, _local_ingestion)
else:
    sys.path.insert(0, _docker_app)

from db.models import Base  # noqa: E402 — must come after sys.path update

# Alembic Config object (access to alembic.ini values)
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for autogenerate
target_metadata = Base.metadata

# Override sqlalchemy.url from environment (takes precedence over alembic.ini)
_db_url = os.environ.get("DATABASE_URL")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Emits SQL to stdout instead of connecting to the DB.
    Useful for generating migration scripts to review before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode — connects to the DB and applies changes.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
