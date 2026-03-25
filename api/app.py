"""
Flask application factory for the eJagriti Samsung Case API.

Usage:
  # Development
  FLASK_ENV=development flask --app app:create_app run

  # Production (gunicorn reads this via the CMD in Dockerfile)
  gunicorn "app:create_app()"
"""

from __future__ import annotations

import logging
import os
import sys

import structlog
from flask import Flask
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Module-level singletons so routes can import them directly.
cache   = Cache()
limiter = Limiter(key_func=get_remote_address)


def _configure_logging(log_level: str = "INFO") -> None:
    """
    Configure structlog for structured JSON output.

    Args:
        log_level: Logging level string (DEBUG, INFO, WARNING, ERROR).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def create_app(config_object: object | None = None) -> Flask:
    """
    Create and configure the Flask application.

    Registers all extensions, blueprints, error handlers, and middleware.

    Args:
        config_object: Optional config class or object. When None the
                       appropriate class is selected from get_config().

    Returns:
        Configured Flask application instance.
    """
    from dotenv import load_dotenv
    load_dotenv()

    from config import get_config
    cfg = config_object or get_config()

    _configure_logging(log_level=getattr(cfg, "LOG_LEVEL", "INFO"))
    log = structlog.get_logger(__name__)

    app = Flask(__name__)
    app.config.from_object(cfg)

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    cache.init_app(app)
    limiter.init_app(app)

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------
    from middleware import register_middleware
    register_middleware(app)

    # ------------------------------------------------------------------
    # Blueprints
    # ------------------------------------------------------------------
    from routes.cases       import cases_bp
    from routes.commissions import commissions_bp
    from routes.judgments   import judgments_bp
    from routes.orders      import orders_bp
    from routes.stats       import stats_bp

    app.register_blueprint(cases_bp)
    app.register_blueprint(commissions_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(judgments_bp)
    app.register_blueprint(stats_bp)

    # ------------------------------------------------------------------
    # Global error handlers
    # ------------------------------------------------------------------
    from schemas.responses import error_response

    @app.errorhandler(404)
    def not_found(_e):
        """Return JSON 404 for unknown routes."""
        return error_response("NOT_FOUND", "The requested resource was not found.", 404)

    @app.errorhandler(405)
    def method_not_allowed(_e):
        """Return JSON 405 for disallowed methods."""
        return error_response("METHOD_NOT_ALLOWED", "Method not allowed.", 405)

    @app.errorhandler(429)
    def rate_limit_exceeded(_e):
        """Return JSON 429 when Flask-Limiter fires."""
        return error_response("RATE_LIMIT_EXCEEDED", "Too many requests. Try again later.", 429)

    @app.errorhandler(500)
    def internal_error(exc: Exception):
        """Return JSON 500 and log the exception."""
        log.error("unhandled_exception", error=str(exc))
        return error_response("INTERNAL_ERROR", "An unexpected error occurred.", 500)

    log.info("flask_app_created", env=os.environ.get("FLASK_ENV", "production"))
    return app
