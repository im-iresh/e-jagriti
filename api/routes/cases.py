"""
Route handlers for /api/cases endpoints.
"""

from __future__ import annotations

from flask import Blueprint, request

from db.queries import get_case_by_id, get_cases_paginated
from schemas.responses import error_response, success_response

cases_bp = Blueprint("cases", __name__, url_prefix="/api/cases")


@cases_bp.get("")
def list_cases():
    """
    Return a paginated list of cases.

    Query params:
      - page (int, default 1)
      - per_page (int, default 20, max 100)
      - status (open|closed|pending|all)
      - commission_type (national|state|district)
      - search (free text on case_number / complainant_name)
    """
    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        return error_response("INVALID_PARAMS", "page and per_page must be integers", 400)

    status          = request.args.get("status") or None
    commission_type = request.args.get("commission_type") or None
    search          = request.args.get("search") or None

    valid_statuses = ("open", "closed", "pending", "all", None)
    if status not in valid_statuses:
        return error_response("INVALID_STATUS", f"status must be one of {valid_statuses[:-1]}", 400)

    valid_types = ("national", "state", "district", None)
    if commission_type not in valid_types:
        return error_response("INVALID_COMMISSION_TYPE", f"commission_type must be one of {valid_types[:-1]}", 400)

    result = get_cases_paginated(
        page=page,
        per_page=per_page,
        status=status if status != "all" else None,
        commission_type=commission_type,
        search=search,
    )

    return success_response(
        data=result["items"],
        page=page,
        per_page=per_page,
        total=result["total"],
    )


@cases_bp.get("/<int:case_id>")
def get_case(case_id: int):
    """
    Return the full nested case detail for a single case.

    Path params:
      - case_id (int): Internal surrogate id
    """
    case = get_case_by_id(case_id)
    if case is None:
        return error_response("NOT_FOUND", f"Case {case_id} not found", 404)
    return success_response(case)
