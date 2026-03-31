"""
Route handlers for /api/batch/status endpoint.

Designed for developer debugging and operator dashboards.
Not cached — always returns live data from the DB.
"""

from __future__ import annotations

from flask import Blueprint, request

from db.queries import get_batch_status
from schemas.responses import error_response, success_response

batch_bp = Blueprint("batch", __name__, url_prefix="/api/batch")


@batch_bp.get("/status")
def batch_status():
    """
    Return live ingestion pipeline status.

    Query params:
      - runs (int, default 10, max 50): Number of recent ingestion runs to include.

    Response shape:
      {
        "recent_runs":  [ { run_id, started_at, finished_at, status,
                            trigger_mode, total_calls, success_count,
                            fail_count, skip_count, duration_seconds, notes } ],
        "queue_depths": {
          "cases_pending_detail_fetch": int,
          "pdfs_pending_fetch":         int,
          "failed_jobs_unresolved":     int
        },
        "recent_errors": [ { id, run_id, case_id, endpoint, http_status,
                              error_type, error_message, retry_count, created_at } ]
      }

    ``status`` on each run is derived:
      "running"   — run_finished_at is NULL
      "failed"    — run finished but fail_count > 0
      "completed" — run finished with fail_count == 0
    """
    try:
        runs = min(50, max(1, int(request.args.get("runs", 10))))
    except ValueError:
        return error_response("INVALID_PARAMS", "runs must be an integer", 400)

    data = get_batch_status(runs=runs)
    return success_response(data)
