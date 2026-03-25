"""
Job: fetch_orders

Fetches daily-order PDFs for hearings that have been identified as
having an available PDF (daily_order_availability_status = 2).

For each daily_orders row where pdf_fetched = False, calls:
  GET /courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf
    ?filingReferenceNumber=N&dateOfHearing=YYYY-MM-DD&orderTypeId=N

The base64 response is decoded and either:
  - Written to the local filesystem (when PDF_STORAGE_DIR env var is set)
  - Uploaded to S3 (when AWS_S3_BUCKET env var is set)
  - Stored as a file path reference in pdf_storage_path

If neither storage option is configured the raw base64 is discarded and
only the pdf_fetched flag is set so the job does not re-fetch.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from client import EJagritiClient, calculate_interval
from db.models import DailyOrder, ErrorType, JobType
from db.session import get_session
from db.upsert import log_failed_job, log_ingestion_error

logger = structlog.get_logger(__name__)

_PATH = "/courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf"
_BATCH_SIZE = 100
_PDF_STORAGE_DIR = os.environ.get("PDF_STORAGE_DIR", "")
_AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")


def _store_pdf(
    pdf_bytes: bytes,
    filing_ref: int,
    hearing_date: str,
    order_type_id: int,
) -> str:
    """
    Persist a decoded PDF and return a storage path string.

    Storage priority:
      1. S3 (if AWS_S3_BUCKET is set)
      2. Local filesystem (if PDF_STORAGE_DIR is set)
      3. Returns empty string (caller marks pdf_fetched=True without a path)

    Args:
        pdf_bytes: Decoded PDF binary content.
        filing_ref: Filing reference number (used in filename).
        hearing_date: ISO date string (YYYY-MM-DD).
        order_type_id: Order type integer (used in filename).

    Returns:
        Storage path string (S3 URI, local path, or empty string).
    """
    filename = f"{filing_ref}_{hearing_date}_type{order_type_id}.pdf"

    if _AWS_S3_BUCKET:
        try:
            import boto3
            s3 = boto3.client("s3")
            key = f"daily_orders/{filename}"
            s3.put_object(Bucket=_AWS_S3_BUCKET, Key=key, Body=pdf_bytes, ContentType="application/pdf")
            path = f"s3://{_AWS_S3_BUCKET}/{key}"
            logger.debug("pdf_stored_s3", path=path)
            return path
        except Exception as exc:
            logger.error("s3_upload_failed", filename=filename, error=str(exc))
            return ""

    if _PDF_STORAGE_DIR:
        storage_dir = Path(_PDF_STORAGE_DIR)
        storage_dir.mkdir(parents=True, exist_ok=True)
        out_path = storage_dir / filename
        out_path.write_bytes(pdf_bytes)
        logger.debug("pdf_stored_local", path=str(out_path))
        return str(out_path)

    # No storage configured — note in path that content was discarded
    logger.debug("pdf_storage_not_configured_discarding", filename=filename)
    return ""


def _get_unfetched_orders(limit: int) -> list[dict[str, Any]]:
    """
    Return daily_order rows pending a PDF fetch.

    Args:
        limit: Maximum rows to return.

    Returns:
        List of dicts with order details.
    """
    with get_session(read_only=True) as session:
        rows = session.execute(
            select(
                DailyOrder.id,
                DailyOrder.case_id,
                DailyOrder.filing_reference_number,
                DailyOrder.date_of_hearing,
                DailyOrder.order_type_id,
            )
            .where(DailyOrder.pdf_fetched.is_(False))
            .order_by(DailyOrder.id)
            .limit(limit)
        ).all()
    return [
        {
            "id": r.id,
            "case_id": r.case_id,
            "filing_reference_number": r.filing_reference_number,
            "date_of_hearing": r.date_of_hearing,
            "order_type_id": r.order_type_id,
        }
        for r in rows
    ]


def run(
    client: EJagritiClient,
    run_id: int,
    dry_run: bool = False,
    daily_budget: int = 3500,
    batch_size: int = _BATCH_SIZE,
) -> dict[str, int]:
    """
    Execute the fetch_orders job for unfetched daily-order PDFs.

    Args:
        client: Authenticated eJagriti HTTP client.
        run_id: Current IngestionRun.id.
        dry_run: Skip DB writes and PDF storage when True.
        daily_budget: Daily call budget for interval pacing.
        batch_size: Max PDFs to fetch per run.

    Returns:
        Dict with ``fetched``, ``stored``, ``failed`` counts.
    """
    stats = {"fetched": 0, "stored": 0, "failed": 0}
    log = logger.bind(job="fetch_orders", run_id=run_id, dry_run=dry_run)

    orders = _get_unfetched_orders(batch_size)
    if not orders:
        log.info("no_unfetched_orders")
        return stats

    log.info("fetch_orders_start", count=len(orders))

    for order in orders:
        time.sleep(calculate_interval(daily_budget))

        hearing_date_str = order["date_of_hearing"].isoformat()
        params: dict[str, Any] = {
            "filingReferenceNumber": order["filing_reference_number"],
            "dateOfHearing":         hearing_date_str,
            "orderTypeId":           order["order_type_id"],
        }

        try:
            resp = client.get(_PATH, params=params)
            stats["fetched"] += 1
        except PermissionError as exc:
            log.error("order_pdf_forbidden", order_id=order["id"])
            with get_session() as session:
                log_failed_job(
                    session,
                    job_type=JobType.fetch_daily_order,
                    endpoint=_PATH,
                    reason=str(exc),
                    case_id=order["case_id"],
                    params=params,
                )
            stats["failed"] += 1
            # Mark with error so we don't retry in an infinite loop
            if not dry_run:
                with get_session() as session:
                    session.execute(
                        __import__("sqlalchemy").text(
                            "UPDATE daily_orders SET pdf_fetch_error=:err WHERE id=:id"
                        ),
                        {"err": str(exc), "id": order["id"]},
                    )
            continue
        except Exception as exc:
            log.error("order_pdf_fetch_error", order_id=order["id"], error=str(exc))
            with get_session() as session:
                log_ingestion_error(
                    session,
                    run_id=run_id,
                    case_id=order["case_id"],
                    endpoint=_PATH,
                    error_type=ErrorType.http_error,
                    error_message=str(exc),
                    request_payload=str(params),
                )
            stats["failed"] += 1
            if not dry_run:
                with get_session() as session:
                    session.execute(
                        __import__("sqlalchemy").text(
                            "UPDATE daily_orders SET pdf_fetch_error=:err WHERE id=:id"
                        ),
                        {"err": str(exc), "id": order["id"]},
                    )
            continue

        # Extract base64 PDF
        pdf_b64: str = (
            (resp.get("data") or {}).get("dailyOrderPdf", "")
            if isinstance(resp, dict)
            else ""
        )

        if not pdf_b64:
            log.warning("empty_pdf_response", order_id=order["id"])
            stats["failed"] += 1
            continue

        if dry_run:
            log.debug("dry_run_skip_pdf_store", order_id=order["id"])
            continue

        try:
            pdf_bytes = base64.b64decode(pdf_b64)
            storage_path = _store_pdf(
                pdf_bytes,
                order["filing_reference_number"],
                hearing_date_str,
                order["order_type_id"],
            )
        except Exception as exc:
            log.error("pdf_decode_store_failed", order_id=order["id"], error=str(exc))
            with get_session() as session:
                session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE daily_orders SET pdf_fetch_error=:err, updated_at=now() WHERE id=:id"
                    ),
                    {"err": str(exc)[:500], "id": order["id"]},
                )
            stats["failed"] += 1
            continue

        # Mark as successfully fetched
        try:
            with get_session() as session:
                session.execute(
                    __import__("sqlalchemy").text("""
                        UPDATE daily_orders
                           SET pdf_fetched      = true,
                               pdf_fetched_at   = :fetched_at,
                               pdf_storage_path = :path,
                               pdf_fetch_error  = NULL,
                               updated_at       = now()
                         WHERE id = :id
                    """),
                    {
                        "fetched_at": datetime.now(timezone.utc),
                        "path":       storage_path or None,
                        "id":         order["id"],
                    },
                )
            stats["stored"] += 1
        except Exception as exc:
            log.error("pdf_mark_failed", order_id=order["id"], error=str(exc))
            stats["failed"] += 1

    log.info("fetch_orders_complete", **stats)
    return stats
