"""
Route handlers for /api/cases endpoints.
"""

from __future__ import annotations

from flask_smorest import Blueprint

import structlog

from auth import require_permission
from db.queries import get_alert_cases, get_case_by_id, get_cases_paginated, get_hearings_for_case
from schemas.responses import (
    AlertsQuerySchema,
    AlertsResponseSchema,
    CaseDetailSchema,
    CaseFilterSchema,
    CaseListItemSchema,
    HearingSchema,
    error_response,
    success_response,
)

log = structlog.get_logger(__name__)

cases_bp = Blueprint("cases", __name__, url_prefix="/api/cases",
                     description="Consumer case management")


@cases_bp.get("")
@cases_bp.arguments(CaseFilterSchema, location="query")
@cases_bp.response(200, CaseListItemSchema(many=True))
@require_permission("cases:read")
def list_cases(args):
    """
    Return a paginated list of cases.

    Ordered by: nearest upcoming hearing date first, then by most recent
    filing date, then by case id descending for stable pagination.

    Filterable by status, commission type, and free-text search.
    """
    page            = max(1, args.get("page", 1))
    per_page        = min(100, max(1, args.get("per_page", 20)))
    status          = args.get("status")
    commission_type = args.get("commission_type")
    search          = args.get("search")

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


@cases_bp.get("/alerts")
@cases_bp.arguments(AlertsQuerySchema, location="query")
@cases_bp.response(200, AlertsResponseSchema)
@require_permission("cases:read")
def alert_cases(args):
    """
    Return open/pending cases grouped by alert condition.

    Pass **no_voc=Y** to include cases with no linked VOC complaint.

    Pass **hearing_soon=Y** to include cases whose next hearing falls
    within the coming 2 days.

    Omitting both params returns all alert sections.
    """
    include_no_voc       = (args.get("no_voc") or "").upper() == "Y"
    include_hearing_soon = (args.get("hearing_soon") or "").upper() == "Y"

    data = get_alert_cases(
        include_no_voc=include_no_voc,
        include_hearing_soon=include_hearing_soon,
    )
    return success_response(data)


@cases_bp.get("/<int:case_id>")
@cases_bp.response(200, CaseDetailSchema)
@require_permission("cases:read")
def get_case(case_id: int):
    """Return the full nested detail for a single case, including embedded hearings."""
    case = get_case_by_id(case_id)
    if case is None:
        return error_response("NOT_FOUND", f"Case {case_id} not found", 404)
    return success_response(case)


@cases_bp.get("/<int:case_id>/hearings")
@cases_bp.response(200, HearingSchema(many=True))
@require_permission("cases:read")
def get_hearings(case_id: int):
    """
    Return all hearings for a case in chronological order (sequence ascending).

    Returns 404 if the case does not exist.
    """
    hearings = get_hearings_for_case(case_id)
    if hearings is None:
        return error_response("NOT_FOUND", f"Case {case_id} not found", 404)
    return success_response(hearings)
