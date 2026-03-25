"""
Shared pytest fixtures for the eJagriti test suite.

Sets up:
  - sys.path so tests can import from ingestion/ and api/ without installation
  - A minimal Flask test client
  - Environment variable stubs for DB-less unit testing
"""

from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Add source roots to sys.path
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
_INGESTION = os.path.join(_REPO_ROOT, "ingestion")
_API       = os.path.join(_REPO_ROOT, "api")

for _p in (_INGESTION, _API):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub DATABASE_URL so session.py can be imported without a real DB
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "test-secret")


@pytest.fixture
def flask_app():
    """
    Create a Flask test application instance.

    Uses TestingConfig (no Redis, no rate limiting).

    Yields:
        Configured Flask app in testing mode.
    """
    # Lazy import to avoid DB connection at collection time
    from app import create_app
    from config import TestingConfig

    app = create_app(config_object=TestingConfig)
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    yield app


@pytest.fixture
def client(flask_app):
    """
    Return a Flask test client.

    Args:
        flask_app: Flask app fixture.

    Yields:
        Flask test client.
    """
    with flask_app.test_client() as c:
        yield c
