"""
Route handlers for /api/cases/:case_id/orders endpoints.
"""

from __future__ import annotations

from datetime import date

from flask import Blueprint, request

from db.queries import get_orders_for_case
from schemas.responses import error_response, success_response

orders_bp = Blueprint("orders", __name__, url_prefix="/api/cases")


@orders_bp.get("/<int:case_id>/orders")
def get_case_orders(case_id: int):
    """
    Return paginated daily orders for a case.

    Path params:
      - case_id (int): Internal surrogate id

    Query params:
      - from_date (YYYY-MM-DD)
      - to_date (YYYY-MM-DD)
      - page (int, default 1)
      - per_page (int, default 20)
    """
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        return error_response("INVALID_PARAMS", "page and per_page must be integers", 400)

    from_date: date | None = None
    to_date:   date | None = None

    if request.args.get("from_date"):
        try:
            from_date = date.fromisoformat(request.args["from_date"])
        except ValueError:
            return error_response("INVALID_DATE", "from_date must be YYYY-MM-DD", 400)

    if request.args.get("to_date"):
        try:
            to_date = date.fromisoformat(request.args["to_date"])
        except ValueError:
            return error_response("INVALID_DATE", "to_date must be YYYY-MM-DD", 400)

    result = get_orders_for_case(
        case_id=case_id,
        from_date=from_date,
        to_date=to_date,
        page=page,
        per_page=per_page,
    )

    if result is None:
        return error_response("NOT_FOUND", f"Case {case_id} not found", 404)

    return success_response(
        data=result["items"],
        page=page,
        per_page=per_page,
        total=result["total"],
    )
