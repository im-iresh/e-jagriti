"""
Route handlers for /api/cases endpoints.
"""

from __future__ import annotations

from flask import Blueprint, request

import structlog

from auth import require_permission
from db.queries import attach_voc_to_case, get_alert_cases, get_case_by_id, get_cases_paginated
from schemas.responses import error_response, success_response

log = structlog.get_logger(__name__)

cases_bp = Blueprint("cases", __name__, url_prefix="/api/cases")


@cases_bp.get("")
@require_permission("cases:read")
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


@cases_bp.get("/alerts")
@require_permission("cases:read")
def alert_cases():
    """
    Return open/pending cases grouped by alert condition.

    Response shape:
      {
        "no_voc":       { "count": int, "items": [...] },
        "hearing_soon": { "count": int, "items": [...] }
      }

    no_voc       — cases with no linked VOC complaint (voc_number IS NULL).
    hearing_soon — cases whose next hearing falls within the coming 2 days.

    Not paginated. Not cached — always live.
    """
    data = get_alert_cases()
    return success_response(data)


@cases_bp.post("/<int:case_id>/voc")
@require_permission("cases:write")
def attach_voc(case_id: int):
    """
    Manually link a VOC complaint number to a case.

    Request body (JSON):
      { "voc_number": <int> }

    Flow:
      1. Validates voc_number exists in the complaint management system (CMS).
         The caller's SSO bearer token is forwarded to the CMS transparently.
      2. Upserts a voc_complaints row (match_status=matched) and stamps
         cases.voc_number so the no-VOC alert query stays accurate.

    Errors:
      400 INVALID_PARAMS    — voc_number missing or not an integer
      404 NOT_FOUND         — case_id does not exist
      404 VOC_NOT_FOUND     — voc_number not found in the CMS
      409 VOC_CONFLICT      — voc_number already linked to a different case
      502 CMS_UNAVAILABLE   — could not reach the complaint management system
    """
    body = request.get_json(silent=True) or {}
    voc_number = body.get("voc_number")
    if not isinstance(voc_number, int):
        return error_response("INVALID_PARAMS", "voc_number (integer) is required in the request body", 400)

    from app import cms_client
    token = request.headers.get("Authorization", "")
    try:
        cms_payload = cms_client.get_voc(voc_number, token)
    except LookupError:
        return error_response("VOC_NOT_FOUND", f"VOC {voc_number} not found in complaint management system", 404)
    except Exception as exc:
        log.error("cms_unavailable", case_id=case_id, voc_number=voc_number, error=str(exc))
        return error_response("CMS_UNAVAILABLE", "Could not reach complaint management system", 502)

    try:
        result = attach_voc_to_case(case_id, voc_number, cms_payload)
    except LookupError:
        return error_response("NOT_FOUND", f"Case {case_id} not found", 404)
    except ValueError:
        return error_response("VOC_CONFLICT", f"VOC {voc_number} is already linked to another case", 409)

    return success_response(result), 201


@cases_bp.get("/<int:case_id>")
@require_permission("cases:read")
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
