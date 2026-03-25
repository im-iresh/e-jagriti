"""
Flask application configuration loaded entirely from environment variables.

Never hardcode secrets here. All sensitive values come from the container
environment (docker-compose env_file, Cloud Run secrets, ECS task definition).
"""

from __future__ import annotations

import os


class Config:
    """
    Production configuration class.

    All attributes are sourced from environment variables with safe defaults
    for local development. Override via .env file or container environment.
    """

    # Flask
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
    DEBUG: bool = os.environ.get("DEBUG", "false").lower() == "true"
    TESTING: bool = False

    # Database — primary (read-write)
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

    # Optional read replica — SELECT queries routed here when set
    REPLICA_DATABASE_URL: str | None = os.environ.get("REPLICA_DATABASE_URL") or None

    # Redis — used by Flask-Caching and Flask-Limiter
    REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    # Flask-Caching
    CACHE_TYPE: str = "RedisCache" if os.environ.get("REDIS_URL") else "SimpleCache"
    CACHE_REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    CACHE_DEFAULT_TIMEOUT: int = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))

    # Flask-Limiter
    RATELIMIT_STORAGE_URI: str = os.environ.get("REDIS_URL", "memory://")
    RATELIMIT_DEFAULT: str = f"{os.environ.get('RATE_LIMIT_PER_MINUTE', '100')} per minute"

    # SQLAlchemy connection pool
    SA_POOL_SIZE: int = int(os.environ.get("SA_POOL_SIZE", "5"))
    SA_MAX_OVERFLOW: int = int(os.environ.get("SA_MAX_OVERFLOW", "10"))

    # Pagination defaults
    DEFAULT_PER_PAGE: int = 20
    MAX_PER_PAGE: int = 100

    # Logging
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()


class TestingConfig(Config):
    """Configuration for pytest — disables Redis caching and rate limiting."""

    TESTING: bool = True
    DEBUG: bool = True
    CACHE_TYPE: str = "SimpleCache"
    RATELIMIT_ENABLED: bool = False
    DATABASE_URL: str = os.environ.get(
        "TEST_DATABASE_URL",
        os.environ.get("DATABASE_URL", ""),
    )


def get_config() -> type[Config]:
    """
    Return the appropriate Config class based on the FLASK_ENV environment variable.

    Returns:
        Config or TestingConfig class (not an instance).
    """
    env = os.environ.get("FLASK_ENV", "production")
    if env == "testing":
        return TestingConfig
    return Config
