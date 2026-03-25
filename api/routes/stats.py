"""
Route handlers for /api/stats and /health endpoints.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from db.queries import get_health_data, get_stats
from schemas.responses import success_response

stats_bp = Blueprint("stats", __name__)


@stats_bp.get("/api/stats")
def aggregate_stats():
    """
    Return aggregate case statistics.

    Includes: total/open/closed/pending counts, breakdown by commission
    type, cases filed per month (last 12 months), and last ingestion run
    summary.

    Response is cached for CACHE_DEFAULT_TIMEOUT seconds (default 1 h).
    """
    from app import cache  # type: ignore[import]

    cached = cache.get("stats")
    if cached is not None:
        return success_response(cached)

    data = get_stats()
    cache.set("stats", data)
    return success_response(data)


@stats_bp.get("/health")
def health_check():
    """
    Return DB connectivity status and last ingestion run summary.

    Always returns 200 so that load balancer health checks don't cycle.
    Check ``db_ok`` in the response body for actual health status.
    """
    data = get_health_data()
    status_code = 200 if data["db_ok"] else 503
    return jsonify({"success": data["db_ok"], "data": data}), status_code
