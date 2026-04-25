"""
Route handlers for /api/stats and /health endpoints.
"""

from __future__ import annotations

from flask import jsonify
from flask_smorest import Blueprint

from auth import require_permission
from db.queries import get_health_data, get_stats
from schemas.responses import HealthSchema, StatsSchema, success_response

stats_bp = Blueprint("stats", __name__, description="Statistics and health")


@stats_bp.get("/api/stats")
@stats_bp.response(200, StatsSchema)
@require_permission("stats:read")
def aggregate_stats():
    """
    Return aggregate case statistics.

    Includes total/open/closed/pending counts, breakdown by commission type,
    cases filed per month (last 12 months), and last ingestion run summary.
    Cached for CACHE_DEFAULT_TIMEOUT seconds (default 1 h).
    """
    from app import cache  # type: ignore[import]

    cached = cache.get("stats")
    if cached is not None:
        return success_response(cached)

    data = get_stats()
    cache.set("stats", data)
    return success_response(data)


@stats_bp.get("/health")
@stats_bp.doc(security=[])
@stats_bp.response(200, HealthSchema)
def health_check():
    """
    Return DB connectivity status and last ingestion run summary.

    No authentication required. Returns 503 when the database is unreachable.
    """
    data = get_health_data()
    status_code = 200 if data["db_ok"] else 503
    resp = jsonify({"success": data["db_ok"], "data": data})
    resp.headers["Cache-Control"] = "no-cache"
    return resp, status_code
