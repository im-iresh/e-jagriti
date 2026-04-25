"""
Route handlers for /api/batch/status endpoint.

Designed for developer debugging and operator dashboards.
Not cached — always returns live data from the DB.
"""

from __future__ import annotations

from flask_smorest import Blueprint

from auth import require_permission
from db.queries import get_batch_status
from schemas.responses import BatchQuerySchema, BatchStatusSchema, error_response, success_response

batch_bp = Blueprint("batch", __name__, url_prefix="/api/batch",
                     description="Ingestion pipeline status")


@batch_bp.get("/status")
@batch_bp.arguments(BatchQuerySchema, location="query")
@batch_bp.response(200, BatchStatusSchema)
@require_permission("batch:read")
def batch_status(args):
    """
    Return live ingestion pipeline status.

    Includes recent runs, queue depths, and recent error records.
    The `runs` param controls how many recent runs to return (max 50).
    """
    runs = min(50, max(1, args.get("runs", 10)))
    data = get_batch_status(runs=runs)
    return success_response(data)
