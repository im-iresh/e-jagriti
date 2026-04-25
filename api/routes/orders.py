"""
Route handlers for per-hearing daily orders and PDF serving.

Endpoints:
  GET /api/cases/<case_id>/hearings/<hearing_id>/orders
  GET /api/cases/<case_id>/hearings/<hearing_id>/orders/<order_id>/pdf
"""

from __future__ import annotations

import os

import structlog
from flask import current_app, send_file
from flask_smorest import Blueprint

from auth import require_permission
from db.queries import get_order_pdf_path, get_orders_for_hearing
from schemas.responses import OrderDetailSchema, OrderFilterSchema, error_response, success_response

log = structlog.get_logger(__name__)

orders_bp = Blueprint(
    "orders",
    __name__,
    url_prefix="/api/cases",
    description="Daily orders and PDF documents per hearing",
)


@orders_bp.get("/<int:case_id>/hearings/<int:hearing_id>/orders")
@orders_bp.arguments(OrderFilterSchema, location="query")
@orders_bp.response(200, OrderDetailSchema(many=True))
@require_permission("orders:read")
def list_orders(args, case_id: int, hearing_id: int):
    """
    Return paginated daily orders for a specific hearing.

    Each item includes a ``pdf_url`` field pointing to the PDF endpoint
    when the PDF has been fetched, otherwise ``null``.

    Returns 404 if the case or hearing does not exist.
    """
    page     = max(1, args.get("page", 1))
    per_page = min(100, max(1, args.get("per_page", 20)))

    result = get_orders_for_hearing(
        case_id=case_id,
        hearing_id=hearing_id,
        page=page,
        per_page=per_page,
    )
    if result is None:
        return error_response(
            "NOT_FOUND",
            f"Hearing {hearing_id} not found for case {case_id}",
            404,
        )

    return success_response(
        data=result["items"],
        page=page,
        per_page=per_page,
        total=result["total"],
    )


@orders_bp.get("/<int:case_id>/hearings/<int:hearing_id>/orders/<int:order_id>/pdf")
@require_permission("orders:read")
def serve_pdf(case_id: int, hearing_id: int, order_id: int):
    """
    Stream the daily order PDF from NFS storage.

    Returns the PDF inline so the browser renders it directly (suitable
    for use in an ``<iframe>`` or ``<a href=...>`` anchor tag).

    Errors:
      404 PDF_NOT_READY   — order exists but PDF has not been fetched yet
      404 FILE_NOT_FOUND  — pdf_storage_path is set but file is missing on disk
      404 NOT_FOUND       — order/hearing/case combination does not exist
    """
    order = get_order_pdf_path(case_id, hearing_id, order_id)
    if order is None:
        return error_response(
            "NOT_FOUND",
            f"Order {order_id} not found for hearing {hearing_id} of case {case_id}",
            404,
        )

    if not order["pdf_fetched"]:
        return error_response(
            "PDF_NOT_READY",
            "The PDF for this order has not been fetched yet.",
            404,
        )

    storage_path: str = order["pdf_storage_path"] or ""
    if not os.path.isabs(storage_path):
        root = current_app.config.get("PDF_STORAGE_ROOT", "/mnt/pdfs")
        storage_path = os.path.join(root, storage_path)

    if not os.path.isfile(storage_path):
        log.error(
            "pdf_file_missing",
            case_id=case_id,
            hearing_id=hearing_id,
            order_id=order_id,
            path=storage_path,
        )
        return error_response(
            "FILE_NOT_FOUND",
            "The PDF file could not be found on the server.",
            404,
        )

    return send_file(storage_path, mimetype="application/pdf", as_attachment=False)
