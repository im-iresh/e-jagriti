"""
Flask request/response middleware.

Provides:
  1. Request ID injection — every request gets a UUID attached to g and
     reflected back in the X-Request-ID response header.
  2. Structured request logging — logs method, path, status code, and
     duration_ms for every request using structlog.
"""

from __future__ import annotations

import time
import uuid

import structlog
from flask import Flask, g, request

logger = structlog.get_logger(__name__)


def register_middleware(app: Flask) -> None:
    """
    Attach before/after request hooks to the Flask app.

    Args:
        app: The Flask application instance to instrument.
    """

    @app.before_request
    def _before() -> None:
        """Generate a request ID and record the start time."""
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.start_time = time.monotonic()

    @app.after_request
    def _after(response):
        """Log completed request details and inject the request ID header."""
        duration_ms = int((time.monotonic() - g.get("start_time", time.monotonic())) * 1000)
        request_id  = g.get("request_id", "-")

        logger.info(
            "http_request",
            method=request.method,
            path=request.path,
            status=response.status_code,
            duration_ms=duration_ms,
            request_id=request_id,
            remote_addr=request.remote_addr,
        )

        response.headers["X-Request-ID"] = request_id
        return response
