"""
Route handlers for /api/cases/:case_id/judgment endpoint.
"""

from __future__ import annotations

from flask import Blueprint

from db.queries import get_judgment_for_case
from schemas.responses import error_response, success_response

judgments_bp = Blueprint("judgments", __name__, url_prefix="/api/cases")


@judgments_bp.get("/<int:case_id>/judgment")
def get_case_judgment(case_id: int):
    """
    Return judgment details for a case.

    The judgment is the daily order with order_type_id=2. Returns an empty
    data dict (with success=true) when the case exists but no judgment has
    been fetched yet.

    Path params:
      - case_id (int): Internal surrogate id
    """
    result = get_judgment_for_case(case_id)

    if result is None:
        return error_response("NOT_FOUND", f"Case {case_id} not found", 404)

    return success_response(result)
