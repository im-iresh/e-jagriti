"""
Route handlers for /api/commissions endpoint.
"""

from __future__ import annotations

from flask import Blueprint, current_app

from auth import require_permission
from db.queries import get_all_commissions
from schemas.responses import success_response

commissions_bp = Blueprint("commissions", __name__, url_prefix="/api")


@commissions_bp.get("/commissions")
@require_permission("commissions:read")
def list_commissions():
    """
    Return the full list of commissions.

    Response is cached for CACHE_DEFAULT_TIMEOUT seconds (default 1 h)
    to avoid hitting the DB on every request. Cache is populated on first
    call and invalidated by TTL.

    Cache key: "commissions_list"
    """
    # Import cache here to avoid circular import at module load time
    from app import cache  # type: ignore[import]

    cached = cache.get("commissions_list")
    if cached is not None:
        return success_response(cached)

    data = get_all_commissions()
    cache.set("commissions_list", data)
    return success_response(data)
