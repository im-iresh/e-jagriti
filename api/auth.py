"""
Auth guard for the eJagriti API.

PERMISSIONS maps permission_id → human description.
These are dummy values — replace with real IDs from your SSO when ready.

Usage on a route:
    @cases_bp.get("")
    @require_permission("cases:read")
    def list_cases(): ...

The decorator reads g.user_info which is populated by the SSO before_request
hook in middleware.py. Returns 401 if unauthenticated, 403 if the user lacks
the required permission.
"""

from __future__ import annotations

from functools import wraps

import structlog
from flask import current_app, g

from schemas.responses import error_response

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Permission registry — dummy IDs, replace with real ones from your SSO
# ---------------------------------------------------------------------------

PERMISSIONS: dict[str, str] = {
    "cases:read":  "View case list, case details, and hearings",
    "orders:read": "View daily orders, judgments, and PDF documents",
    "stats:read":  "View ingestion statistics",
    "batch:read":  "View batch run status and recent errors",
}


def require_permission(permission_id: str):
    """
    Route decorator that enforces a permission check.

    Reads g.user_info (set by _resolve_user in middleware.py).
    Returns 401 if no user is authenticated.
    Returns 403 if the user lacks the required permission_id.

    Args:
        permission_id: Key from PERMISSIONS (e.g. "cases:read").
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_info = g.get("user_info")
            if user_info is None:
                logger.warning("permission_check_no_user", required_permission=permission_id)
                return error_response(
                    "UNAUTHORIZED",
                    "Authentication required. Provide a valid Bearer token.",
                    401,
                )
            service_id = current_app.config.get("SERVICE_ID", "")
            user_role_ids = {
                r["roleId"] for r in user_info.get("roles", [])
                if not service_id or r.get("serviceId") == service_id
            }
            has_permission = any(
                p.get("permissionName") == permission_id
                and bool(set(p.get("roleIdList", [])) & user_role_ids)
                for p in user_info.get("permissions", [])
            )
            if not has_permission:
                logger.warning(
                    "permission_denied",
                    user_id=user_info.get("userID"),
                    required_permission=permission_id,
                    user_role_ids=list(user_role_ids),
                )
                return error_response(
                    "FORBIDDEN",
                    f"User Doesn't Have Permission required for this resource.",
                    403,
                )
            logger.debug(
                "permission_granted",
                user_id=user_info.get("userID"),
                permission=permission_id,
            )
            return fn(*args, **kwargs)
        return wrapper
    return decorator
