"""
Marshmallow response schemas and envelope helpers.

All API responses are wrapped in one of two envelopes:
  Success: { "success": true,  "data": ..., "meta": { "pagination": ... } }
  Error:   { "success": false, "error": { "code": "...", "message": "..." } }

Schemas are used for documentation / validation of outbound data.
They are NOT used for deserialization of inbound payloads (this API is
read-only — no POST/PUT endpoints).
"""

from __future__ import annotations

from typing import Any

from flask import jsonify, Response
from marshmallow import Schema, fields


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def success_response(
    data: Any,
    status: int = 200,
    page: int | None = None,
    per_page: int | None = None,
    total: int | None = None,
) -> tuple[Response, int]:
    """
    Wrap data in the standard success envelope.

    Args:
        data: Serialisable data payload (dict or list).
        status: HTTP status code.
        page: Current page number (for paginated endpoints).
        per_page: Items per page.
        total: Total matching items across all pages.

    Returns:
        (Flask Response, HTTP status code) tuple.
    """
    body: dict[str, Any] = {"success": True, "data": data}

    if total is not None:
        pages = (total + per_page - 1) // per_page if per_page else 1
        body["meta"] = {
            "pagination": {
                "page":       page,
                "per_page":   per_page,
                "total":      total,
                "total_pages": pages,
            }
        }

    return jsonify(body), status


def error_response(
    code: str,
    message: str,
    status: int = 400,
) -> tuple[Response, int]:
    """
    Wrap an error in the standard error envelope.

    Args:
        code: Machine-readable error code (e.g. "NOT_FOUND").
        message: Human-readable description.
        status: HTTP status code.

    Returns:
        (Flask Response, HTTP status code) tuple.
    """
    return jsonify({"success": False, "error": {"code": code, "message": message}}), status


# ---------------------------------------------------------------------------
# Marshmallow schemas (used for outbound documentation / optional validation)
# ---------------------------------------------------------------------------

class CommissionSchema(Schema):
    """Schema for a commission object embedded in case responses."""
    id              = fields.Int()
    commission_id_ext = fields.Int()
    name            = fields.Str()
    type            = fields.Str()
    state_id        = fields.Int(allow_none=True)
    district_id     = fields.Int(allow_none=True)
    case_prefix_text= fields.Str(allow_none=True)
    parent_commission_id = fields.Int(allow_none=True)


class HearingSchema(Schema):
    """Schema for a single hearing entry."""
    id                       = fields.Int()
    court_room_hearing_id    = fields.Str()
    date                     = fields.Str(allow_none=True)
    next_date                = fields.Str(allow_none=True)
    case_stage               = fields.Str(allow_none=True)
    proceeding_text          = fields.Str(allow_none=True)
    sequence_number          = fields.Int()
    daily_order_available    = fields.Bool()


class DailyOrderSchema(Schema):
    """Schema for a daily order record."""
    id               = fields.Int()
    date             = fields.Str(allow_none=True)
    order_type_id    = fields.Int()
    pdf_fetched      = fields.Bool()
    pdf_storage_path = fields.Str(allow_none=True)
    pdf_fetched_at   = fields.Str(allow_none=True)


class CaseListItemSchema(Schema):
    """Lightweight case schema for list endpoint."""
    case_id          = fields.Int()
    case_number      = fields.Str()
    complainant_name = fields.Str(allow_none=True)
    commission_name  = fields.Str(allow_none=True)
    commission_type  = fields.Str(allow_none=True)
    filing_date      = fields.Str(allow_none=True)
    date_of_next_hearing = fields.Str(allow_none=True)
    status           = fields.Str()
    case_stage       = fields.Str(allow_none=True)
    last_updated     = fields.Str(allow_none=True)


class ComplainantSchema(Schema):
    """Complainant sub-object."""
    name            = fields.Str(allow_none=True)
    advocate_names  = fields.List(fields.Str())


class RespondentSchema(Schema):
    """Respondent sub-object."""
    name            = fields.Str(allow_none=True)
    advocate_names  = fields.List(fields.Str())


class CaseDetailSchema(Schema):
    """Full nested case detail schema."""
    case_id              = fields.Int()
    case_number          = fields.Str()
    filing_date          = fields.Str(allow_none=True)
    date_of_cause        = fields.Str(allow_none=True)
    status               = fields.Str()
    case_stage           = fields.Str(allow_none=True)
    case_category        = fields.Str(allow_none=True)
    date_of_next_hearing = fields.Str(allow_none=True)
    commission           = fields.Nested(CommissionSchema)
    complainant          = fields.Nested(ComplainantSchema)
    respondent           = fields.Nested(RespondentSchema)
    hearings             = fields.List(fields.Nested(HearingSchema))
    daily_orders         = fields.List(fields.Nested(DailyOrderSchema))
    last_fetched_at      = fields.Str(allow_none=True)


class PaginationMeta(Schema):
    """Pagination metadata sub-schema."""
    page        = fields.Int()
    per_page    = fields.Int()
    total       = fields.Int()
    total_pages = fields.Int()


class StatsSchema(Schema):
    """Stats endpoint response schema."""
    total_cases          = fields.Int()
    open_cases           = fields.Int()
    closed_cases         = fields.Int()
    pending_cases        = fields.Int()
    by_commission_type   = fields.Dict(keys=fields.Str(), values=fields.Int())
    cases_per_month      = fields.List(fields.Dict())
    last_ingestion_run   = fields.Dict(allow_none=True)
