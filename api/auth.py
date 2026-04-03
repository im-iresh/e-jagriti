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

from flask import g

from schemas.responses import error_response

# ---------------------------------------------------------------------------
# Permission registry — dummy IDs, replace with real ones from your SSO
# ---------------------------------------------------------------------------

PERMISSIONS: dict[str, str] = {
    "cases:read":       "View case list and case details",
    "cases:write":      "Attach or modify case-linked records (e.g. VOC linkage)",
    "commissions:read": "View commission list",
    "orders:read":      "View daily orders and judgments",
    "stats:read":       "View ingestion statistics and batch status",
    "batch:read":       "View batch run status and recent errors",
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
                return error_response(
                    "UNAUTHORIZED",
                    "Authentication required. Provide a valid Bearer token.",
                    401,
                )
            if permission_id not in user_info.get("permission_ids", []):
                return error_response(
                    "FORBIDDEN",
                    f"Permission '{permission_id}' is required for this resource.",
                    403,
                )
            return fn(*args, **kwargs)
        return wrapper
    return decorator
