"""
Upsert helpers for all eJagriti tables.

Uses PostgreSQL's INSERT ... ON CONFLICT DO UPDATE so each helper is
idempotent and safe to call multiple times with the same data.

Design rules:
- Every public function accepts a plain dict (already parsed from the API
  response) and an open SQLAlchemy Session.
- Callers are responsible for commit / rollback — these helpers never commit.
- DRY_RUN mode is handled at the caller level (jobs/); these functions always
  write when called.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.models import (
    ApiCallLog,
    Case,
    Commission,
    DailyOrder,
    ErrorType,
    FailedJob,
    Hearing,
    IngestionError,
    IngestionRun,
    JobType,
    TriggerMode,
    VocComplaint,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Commission upsert
# ---------------------------------------------------------------------------

def upsert_commission(session: Session, data: dict[str, Any]) -> int:
    """
    Insert or update a commission row.

    Conflict key: commission_id_ext (unique external ID from the API).

    Args:
        session: Active SQLAlchemy session (not committed by this function).
        data: Dict with keys matching Commission columns. Must include
              ``commission_id_ext``.

    Returns:
        Internal surrogate id of the upserted row.
    """
    stmt = (
        pg_insert(Commission)
        .values(**data)
        .on_conflict_do_update(
            index_elements=["commission_id_ext"],
            set_={
                "name_en":                      pg_insert(Commission).excluded.name_en,
                "commission_type":              pg_insert(Commission).excluded.commission_type,
                "state_id":                     pg_insert(Commission).excluded.state_id,
                "district_id":                  pg_insert(Commission).excluded.district_id,
                "case_prefix_text":             pg_insert(Commission).excluded.case_prefix_text,
                "circuit_addition_bench_status":pg_insert(Commission).excluded.circuit_addition_bench_status,
                "parent_commission_id":         pg_insert(Commission).excluded.parent_commission_id,
                "updated_at":                   datetime.now(timezone.utc),
            },
        )
        .returning(Commission.id)
    )
    result = session.execute(stmt)
    row_id: int = result.scalar_one()
    logger.debug("upsert_commission", commission_id_ext=data.get("commission_id_ext"), row_id=row_id)
    return row_id


# ---------------------------------------------------------------------------
# Case upsert
# ---------------------------------------------------------------------------

def upsert_case(session: Session, data: dict[str, Any]) -> int:
    """
    Insert or update a case row.

    Conflict key: case_number.

    When the caller has already computed data_hash and the stored hash
    matches, the caller should skip calling this function entirely
    (checked in fetch_case_detail.py). The upsert here always writes
    the new values to handle partial updates from the list endpoint.

    Args:
        session: Active SQLAlchemy session.
        data: Dict matching Case columns. Must include ``case_number``
              and ``commission_id``.

    Returns:
        Internal surrogate id of the upserted row.
    """
    stmt = (
        pg_insert(Case)
        .values(**data)
        .on_conflict_do_update(
            index_elements=["case_number"],
            set_={k: pg_insert(Case).excluded[k]
                  for k in data
                  if k not in ("id", "case_number", "created_at")},
        )
        .returning(Case.id)
    )
    result = session.execute(stmt)
    row_id: int = result.scalar_one()
    logger.debug("upsert_case", case_number=data.get("case_number"), row_id=row_id)
    return row_id


# ---------------------------------------------------------------------------
# Hearing upsert
# ---------------------------------------------------------------------------

def upsert_hearing(session: Session, data: dict[str, Any]) -> int:
    """
    Insert or update a hearing row.

    Conflict key: (case_id, court_room_hearing_id).

    Args:
        session: Active SQLAlchemy session.
        data: Dict matching Hearing columns. Must include ``case_id``
              and ``court_room_hearing_id``.

    Returns:
        Internal surrogate id of the upserted row.
    """
    stmt = (
        pg_insert(Hearing)
        .values(**data)
        .on_conflict_do_update(
            constraint="uq_hearing_case_courtroom",
            set_={k: pg_insert(Hearing).excluded[k]
                  for k in data
                  if k not in ("id", "case_id", "court_room_hearing_id", "created_at")},
        )
        .returning(Hearing.id)
    )
    result = session.execute(stmt)
    row_id: int = result.scalar_one()
    logger.debug(
        "upsert_hearing",
        case_id=data.get("case_id"),
        court_room_hearing_id=data.get("court_room_hearing_id"),
        row_id=row_id,
    )
    return row_id


# ---------------------------------------------------------------------------
# DailyOrder upsert
# ---------------------------------------------------------------------------

def upsert_daily_order(session: Session, data: dict[str, Any]) -> int:
    """
    Insert or update a daily_order row.

    Conflict key: (filing_reference_number, date_of_hearing, order_type_id).

    Only updates pdf_* columns when pdf_fetched is changing from False to True
    so that a re-run does not overwrite a successfully fetched PDF path with
    an error state.

    Args:
        session: Active SQLAlchemy session.
        data: Dict matching DailyOrder columns. Must include
              ``filing_reference_number``, ``date_of_hearing``, ``order_type_id``.

    Returns:
        Internal surrogate id of the upserted row.
    """
    update_set = {k: pg_insert(DailyOrder).excluded[k]
                  for k in data
                  if k not in ("id", "filing_reference_number", "date_of_hearing",
                               "order_type_id", "created_at")}

    stmt = (
        pg_insert(DailyOrder)
        .values(**data)
        .on_conflict_do_update(
            constraint="uq_daily_order_pdf_key",
            set_=update_set,
        )
        .returning(DailyOrder.id)
    )
    result = session.execute(stmt)
    row_id: int = result.scalar_one()
    logger.debug(
        "upsert_daily_order",
        filing_reference_number=data.get("filing_reference_number"),
        date_of_hearing=str(data.get("date_of_hearing")),
        row_id=row_id,
    )
    return row_id


# ---------------------------------------------------------------------------
# Ingestion run helpers
# ---------------------------------------------------------------------------

def create_ingestion_run(
    session: Session,
    trigger_mode: TriggerMode = TriggerMode.scheduler,
) -> int:
    """
    Insert a new IngestionRun row at the start of a batch.

    Args:
        session: Active SQLAlchemy session.
        trigger_mode: How this run was triggered.

    Returns:
        New run id.
    """
    run = IngestionRun(
        run_started_at=datetime.now(timezone.utc),
        trigger_mode=trigger_mode,
    )
    session.add(run)
    session.flush()  # Populate run.id without committing.
    logger.info("ingestion_run_created", run_id=run.id, trigger_mode=trigger_mode.value)
    return run.id


def close_ingestion_run(
    session: Session,
    run_id: int,
    total_calls: int,
    success_count: int,
    fail_count: int,
    skip_count: int,
    duration_seconds: float,
    notes: str | None = None,
) -> None:
    """
    Update the IngestionRun row at the end of a batch.

    Args:
        session: Active SQLAlchemy session.
        run_id: ID of the run to close.
        total_calls: Total HTTP calls made.
        success_count: Calls that returned 2xx.
        fail_count: Calls that failed after retries.
        skip_count: Records skipped (hash unchanged).
        duration_seconds: Wall-clock seconds for the entire run.
        notes: Optional free-text summary.
    """
    session.execute(
        text("""
            UPDATE ingestion_runs
               SET run_finished_at  = :finished,
                   total_calls      = :total,
                   success_count    = :success,
                   fail_count       = :fail,
                   skip_count       = :skip,
                   duration_seconds = :duration,
                   notes            = :notes
             WHERE id = :run_id
        """),
        {
            "finished": datetime.now(timezone.utc),
            "total":    total_calls,
            "success":  success_count,
            "fail":     fail_count,
            "skip":     skip_count,
            "duration": duration_seconds,
            "notes":    notes,
            "run_id":   run_id,
        },
    )
    logger.info(
        "ingestion_run_closed",
        run_id=run_id,
        total_calls=total_calls,
        success_count=success_count,
        fail_count=fail_count,
        skip_count=skip_count,
        duration_seconds=round(duration_seconds, 2),
    )


# ---------------------------------------------------------------------------
# Error / failed-job helpers
# ---------------------------------------------------------------------------

def log_ingestion_error(
    session: Session,
    *,
    run_id: int | None,
    case_id: int | None,
    endpoint: str,
    error_type: ErrorType,
    error_message: str,
    http_status: int | None = None,
    request_payload: str | None = None,
    response_body: str | None = None,
    retry_count: int = 0,
) -> None:
    """
    Append an IngestionError row.

    Args:
        session: Active SQLAlchemy session.
        run_id: Parent run id (may be None for standalone errors).
        case_id: Related case internal id (may be None).
        endpoint: URL path that failed.
        error_type: Enum classifying the failure.
        error_message: Human-readable description.
        http_status: HTTP response code if applicable.
        request_payload: JSON-serialised query params sent.
        response_body: First 4 KB of the response body.
        retry_count: How many retries were attempted.
    """
    err = IngestionError(
        run_id=run_id,
        case_id=case_id,
        endpoint=endpoint,
        http_status=http_status,
        error_type=error_type,
        error_message=error_message,
        request_payload=request_payload,
        response_body=(response_body or "")[:4096] if response_body else None,
        retry_count=retry_count,
    )
    session.add(err)
    logger.warning(
        "ingestion_error_logged",
        run_id=run_id,
        endpoint=endpoint,
        error_type=error_type.value,
        http_status=http_status,
    )


def log_failed_job(
    session: Session,
    *,
    job_type: JobType,
    endpoint: str,
    reason: str,
    case_id: int | None = None,
    commission_id: int | None = None,
    params: dict | None = None,
    retry_count: int = 0,
) -> None:
    """
    Insert a FailedJob row so the retry sweeper can re-attempt it.

    Args:
        session: Active SQLAlchemy session.
        job_type: Type of job that failed.
        endpoint: URL path attempted.
        reason: Failure reason string.
        case_id: Related case internal id.
        commission_id: Related commission internal id.
        params: Original query parameters (will be JSON-serialised).
        retry_count: Retries already attempted.
    """
    now = datetime.now(timezone.utc)
    job = FailedJob(
        job_type=job_type,
        case_id=case_id,
        commission_id=commission_id,
        endpoint=endpoint,
        params=json.dumps(params) if params else None,
        retry_count=retry_count,
        last_attempted_at=now,
        next_retry_at=None,  # Retry scheduler sets this
        reason=reason,
        resolved=False,
    )
    session.add(job)
    logger.warning(
        "failed_job_logged",
        job_type=job_type.value,
        endpoint=endpoint,
        case_id=case_id,
        reason=reason,
    )


def log_api_call(
    session: Session,
    *,
    run_id: int | None,
    endpoint: str,
    response_code: int | None,
    duration_ms: int,
    retry_count: int = 0,
    user_agent: str | None = None,
    method: str = "GET",
) -> None:
    """
    Append an ApiCallLog row for observability.

    Args:
        session: Active SQLAlchemy session.
        run_id: Parent run id.
        endpoint: URL path called.
        response_code: HTTP status code returned.
        duration_ms: Round-trip time in milliseconds.
        retry_count: Number of retries before this final response.
        user_agent: User-Agent header used.
        method: HTTP method (default GET).
    """
    log = ApiCallLog(
        run_id=run_id,
        endpoint=endpoint,
        method=method,
        response_code=response_code,
        duration_ms=duration_ms,
        retry_count=retry_count,
        user_agent=user_agent,
    )
    session.add(log)


# ---------------------------------------------------------------------------
# VOC complaint upsert
# ---------------------------------------------------------------------------

def upsert_voc_complaint(session: Session, data: dict[str, Any]) -> int:
    """
    Insert or update a voc_complaints row.

    Conflict key: voc_number (unique per VOC record).

    Args:
        session: Active SQLAlchemy session.
        data: Dict matching VocComplaint columns. Must include ``voc_number``
              and ``match_status``.

    Returns:
        Internal surrogate id of the upserted row.
    """
    stmt = (
        pg_insert(VocComplaint)
        .values(**data)
        .on_conflict_do_update(
            index_elements=["voc_number"],
            set_={k: pg_insert(VocComplaint).excluded[k]
                  for k in data
                  if k not in ("id", "voc_number", "created_at")},
        )
        .returning(VocComplaint.id)
    )
    row_id: int = session.execute(stmt).scalar_one()
    logger.debug(
        "upsert_voc_complaint",
        voc_number=data.get("voc_number"),
        match_status=data.get("match_status"),
        case_id=data.get("case_id"),
        row_id=row_id,
    )
    return row_id
