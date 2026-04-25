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

import httpx
import structlog
from flask import Flask, current_app, g, make_response, request

from schemas.responses import error_response

logger = structlog.get_logger(__name__)

# Paths that bypass the /api/* authentication gate entirely.
# The Swagger UI and its spec are public so developers can open the page in a
# browser; actual API endpoints are still protected by the auth gate below.
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/api/docs",
    "/api/openapi.json",
    "/api/docs/oauth2-redirect",   # Swagger UI OAuth redirect helper
})


def register_middleware(app: Flask) -> None:
    """
    Attach before/after request hooks to the Flask app.

    Hook order (Flask executes before_request hooks in registration order):
      1. _resolve_user  — calls SSO, populates g.user_info
      2. _enforce_api_auth — blocks /api/* when g.user_info is None
      3. _before        — request ID + timing

    Args:
        app: The Flask application instance to instrument.
    """

    @app.before_request
    def _resolve_user() -> None:
        """
        Resolve the Bearer token to a user object via the SSO userinfo endpoint.

        Only fires when an Authorization: Bearer <token> header is present —
        requests without a token (e.g. /health) incur zero SSO overhead.

        Populates g.user_info with the SSO response dict, or None on:
          - Missing / malformed Authorization header
          - SSO returns non-200
          - SSO network error / timeout

        Expected SSO response shape:
            {
              "user_id":        "abc123",
              "email":          "user@example.com",
              "name":           "Jane Doe",
              "permission_ids": ["cases:read", "orders:read"]
            }
        """
        g.user_info = None
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            # No token — let _enforce_api_auth handle the 401.
            return

        token = auth_header[7:]
        sso_url = current_app.config.get("SSO_URL", "")
        if not sso_url:
            logger.error("sso_url_not_configured", request_path=request.path)
            return error_response("CONFIGURATION_ERROR", "SSO service is not configured.", 500)

        try:
            resp = httpx.get(
                f"{sso_url}/api/v1/sso/userInfo",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("sso_unreachable", error=str(exc), request_path=request.path)
            return error_response("SSO_UNAVAILABLE", "Could not connect to SSO service.", 503)
        except Exception as exc:
            logger.warning("sso_call_failed", error=str(exc), request_path=request.path)
            return error_response("SSO_UNAVAILABLE", "Could not connect to SSO service.", 503)

        if resp.status_code != 200:
            logger.warning("sso_non_200", status=resp.status_code, request_path=request.path)
            proxied = make_response(resp.content, resp.status_code)
            proxied.headers["Content-Type"] = resp.headers.get("Content-Type", "application/json")
            return proxied

        body = resp.json()
        rsp  = body.get("rsp") if isinstance(body, dict) else None
        data = rsp.get("data") if isinstance(rsp, dict) else None
        if data is None:
            logger.warning("sso_unexpected_shape", request_path=request.path)
            return
        g.user_info = data

    @app.before_request
    def _enforce_api_auth():
        """
        Block all /api/* requests when g.user_info is None.

        Routes in _PUBLIC_PATHS are exempt. Per-route @require_permission
        decorators handle finer-grained permission checks on top of this gate.
        """
        if request.path in _PUBLIC_PATHS:
            return
        if not request.path.startswith("/api/"):
            return

        user_info = g.get("user_info")
        if user_info is None:
            return error_response(
                "UNAUTHORIZED",
                "Authentication required. Provide a valid Bearer token.",
                401,
            )

        service_id = current_app.config.get("SERVICE_ID", "")
        if not service_id:
            logger.warning("service_id_not_configured", request_path=request.path)
        else:
            user_services = user_info.get("services", [])
            if not any(s.get("id") == service_id for s in user_services):
                return error_response(
                    "FORBIDDEN",
                    "You do not have access to this service.",
                    403,
                )

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

        response.headers["X-Request-ID"]              = request_id
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["Cache-Control"]             = "no-store"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"

        # Swagger UI needs CDN assets and inline scripts; all other endpoints
        # get the strictest possible policy (pure JSON — no resources needed).
        if request.path.startswith("/api/docs") or request.path == "/api/openapi.json":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data:; "
                "connect-src 'self';"
            )
        else:
            response.headers["Content-Security-Policy"] = "default-src 'none'"

        return response
