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
    CACHE_DEFAULT_TIMEOUT: int = int(os.environ.get("EJAGRITI_CACHE_TTL_SECONDS", "3600"))

    # Flask-Limiter
    RATELIMIT_STORAGE_URI: str = os.environ.get("REDIS_URL", "memory://")
    RATELIMIT_DEFAULT: str = f"{os.environ.get('EJAGRITI_RATE_LIMIT_PER_MINUTE', '100')} per minute"

    # SQLAlchemy connection pool
    SA_POOL_SIZE: int = int(os.environ.get("EJAGRITI_SA_POOL_SIZE", "5"))
    SA_MAX_OVERFLOW: int = int(os.environ.get("EJAGRITI_SA_MAX_OVERFLOW", "10"))

    # Pagination defaults
    DEFAULT_PER_PAGE: int = 20
    MAX_PER_PAGE: int = 100

    # Logging
    LOG_LEVEL: str = os.environ.get("EJAGRITI_LOG_LEVEL", "INFO").upper()
    LOG_DIR:   str = os.environ.get("EJAGRITI_LOG_DIR", "logs")

    # SSO
    SSO_URL:    str = os.environ.get("EJAGRITI_SSO_URL", "https://sso.example.com")
    SERVICE_ID: str = os.environ.get("EJAGRITI_SERVICE_ID", "")

    # NFS mount root for PDF files — pdf_storage_path is resolved relative to this
    # when the stored path is not absolute. Override with EJAGRITI_PDF_STORAGE_ROOT.
    PDF_STORAGE_ROOT: str = os.environ.get("EJAGRITI_PDF_STORAGE_ROOT", "/mnt/pdfs")

    # CORS — controlled entirely by environment variables.
    #
    # EJAGRITI_CORS_ORIGINS: comma-separated list of allowed origins, or "*".
    #   e.g. "https://app.example.com,https://admin.example.com"
    #   Defaults to "*" (allow all) which is safe behind an SSO auth gate.
    #   Set to specific origins in production to restrict browser access.
    #
    # EJAGRITI_CORS_MAX_AGE: seconds browsers may cache preflight responses.
    #   Default 600 (10 min). Reduce if allowed origins change frequently.
    _cors_origins_raw: str = os.environ.get("EJAGRITI_CORS_ORIGINS", "*")
    CORS_ORIGINS: list[str] | str = (
        "*"
        if _cors_origins_raw.strip() == "*"
        else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    )
    CORS_MAX_AGE: int = int(os.environ.get("EJAGRITI_CORS_MAX_AGE", "600"))

    # Swagger / OpenAPI (flask-smorest)
    OPENAPI_VERSION: str = "3.0.3"
    OPENAPI_URL_PREFIX: str = "/api"          # spec at /api/openapi.json
    OPENAPI_SWAGGER_UI_PATH: str = "/docs"    # UI at /api/docs
    OPENAPI_SWAGGER_UI_URL: str = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
    API_TITLE: str = "eJagriti Samsung Case API"
    API_VERSION: str = "v1"


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
