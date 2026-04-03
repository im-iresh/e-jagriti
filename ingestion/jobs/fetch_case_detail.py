"""
Job: fetch_case_detail

For every open or pending case, calls:
  GET /case/caseFilingService/v2/getCaseStatus?caseNumber=DC%2F77%2FCC%2F104%2F2025

Parses the full nested response:
  - Updates the cases row (stage, status, filing_reference_number, advocates)
  - Upserts all caseHearingDetails[] → hearings rows
  - For hearings with daily_order_availability_status=2, creates daily_orders
    stub rows (pdf_fetched=False) so fetch_orders can pick them up

Cases are processed in id-order chunks of _CHUNK_SIZE so the full case list
is never loaded into memory at once. Every open/pending case is refreshed on
each daily run.

Key quirk: fillingReferenceNumber (double-l typo) in the API response.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, update as sa_update

from client import EJagritiClient, calculate_interval
from db.models import Case, ErrorType, JobType
from db.session import get_session
from db.upsert import (
    log_failed_job,
    log_ingestion_error,
    upsert_daily_order,
    upsert_hearing,
)

logger = structlog.get_logger(__name__)

_PATH = "/case/caseFilingService/v2/getCaseStatus"
_CHUNK_SIZE = 500  # cases fetched from DB per chunk — tune independently of budget


# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------

import nh3

_ALLOWED_TAGS: frozenset[str] = frozenset({
    # Headings
    "h1", "h2", "h3", "h4", "h5", "h6",
    # Text formatting
    "p", "br", "b", "i", "u", "strong", "em", "s", "sub", "sup",
    # Lists
    "ul", "ol", "li", "dl", "dt", "dd",
    # Tables
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    # Block / inline containers
    "span", "div", "blockquote", "pre", "code",
    # Misc document structure
    "hr", "section", "article",
})


def _sanitize_html(text: str | None) -> str | None:
    """
    Sanitize HTML from the API using an allowlist of safe formatting tags.

    Strips all dangerous tags (script, iframe, style, etc.) and all attributes
    (class, style, onclick, etc.). Safe formatting tags are preserved as-is.
    Returns None if input is None or empty.
    """
    if not text:
        return text
    cleaned = nh3.clean(text, tags=_ALLOWED_TAGS, attributes={})
    return cleaned or None


def _md5(payload: Any) -> str:
    """
    Compute the MD5 hex digest of a JSON-serialised payload.

    Args:
        payload: Any JSON-serialisable value.

    Returns:
        32-character lowercase hex string.
    """
    return hashlib.md5(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _parse_date(value: str | None) -> date | None:
    """
    Parse an ISO date or ISO datetime string from the API.

    Args:
        value: Date string (e.g. "2025-06-18" or "2025-06-18T12:38:48.495+00:00").

    Returns:
        date object or None.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _map_status(stage_name: str | None) -> str:
    """
    Derive a canonical case status from the API stage name.

    Args:
        stage_name: Raw stage string from getCaseStatus.

    Returns:
        "open", "closed", or "pending".
    """
    if not stage_name:
        return "pending"
    upper = stage_name.upper()
    closed_kw = ("DISPOSED", "DISMISSED", "WITHDRAWN", "CLOSED", "DECIDED", "ALLOWED", "REJECTED")
    if any(kw in upper for kw in closed_kw):
        return "closed"
    if upper in ("REGISTERED", "ADMIT", "NOTICE ISSUED"):
        return "open"
    return "pending"


def _process_detail(
    case_db_id: int,
    case_number: str,
    data: dict[str, Any],
    existing_hash: str | None,
    run_id: int,
    dry_run: bool,
) -> str:
    """
    Parse and persist the getCaseStatus response for one case.

    Always stamps last_fetched_at regardless of whether the data changed.
    Skips all other DB writes when the hash is unchanged (data not modified).

    Args:
        case_db_id: Internal DB id of the case.
        case_number: Case number string (for logging).
        data: Parsed ``data`` block from the getCaseStatus response.
        existing_hash: Current data_hash stored in the DB (may be None).
        run_id: IngestionRun id for error attribution.
        dry_run: Skip DB writes when True.

    Returns:
        "updated", "skipped", or "failed".
    """
    log = logger.bind(case_number=case_number, case_db_id=case_db_id)
    new_hash = _md5(data)
    now = datetime.now(timezone.utc)

    if dry_run:
        log.debug("dry_run_skip_detail")
        return "skipped"

    # Always stamp last_fetched_at — even if data is unchanged — so monitoring
    # queries (e.g. "cases not refreshed in 48h") stay accurate.
    if existing_hash and existing_hash == new_hash:
        try:
            with get_session() as session:
                session.execute(
                    sa_update(Case)
                    .where(Case.case_number == case_number)
                    .values(last_fetched_at=now)
                )
        except Exception as exc:
            log.error("touch_last_fetched_at_failed", error=str(exc))
        log.debug("case_detail_unchanged")
        return "skipped"

    # NOTE: API typo — "fillingReferenceNumber" (double-l)
    filing_ref = data.get("fillingReferenceNumber") or data.get("filingReferenceNumber")

    case_update: dict[str, Any] = {
        "case_number":                case_number,
        "filing_reference_number":    filing_ref,
        "case_stage_name":            data.get("caseStage"),
        "case_stage_id":              data.get("caseStageId"),
        "case_type_id":               data.get("caseTypeId"),
        "filing_date":                _parse_date(data.get("caseFilingDate") or data.get("dateOfCause")),
        "date_of_cause":              _parse_date(data.get("dateOfCause")),
        "date_of_next_hearing":       _parse_date(data.get("dateOfNextearing")),
        "complainant_name":           data.get("complainant"),
        "respondent_name":            data.get("respondent"),
        "complainant_advocate_names": json.dumps(data.get("complainantAdvocate") or []),
        "respondent_advocate_names":  json.dumps(data.get("respondentAdvocate") or []),
        "status":                     _map_status(data.get("caseStage")),
        "data_hash":                  new_hash,
        "last_fetched_at":            now,
    }
    # Remove None values but preserve explicit None for date_of_next_hearing
    case_update = {k: v for k, v in case_update.items() if v is not None or k in ("date_of_next_hearing",)}

    hearings: list[dict] = data.get("caseHearingDetails") or []

    try:
        with get_session() as session:
            # Case already exists in DB — UPDATE only, never INSERT,
            # so commission_id (not in case_update) is preserved.
            session.execute(
                sa_update(Case).where(Case.case_number == case_number).values(**case_update)
            )

            for h in hearings:
                court_id = str(h.get("courtRoomHearingId", ""))
                if not court_id:
                    continue

                hearing_data: dict[str, Any] = {
                    "case_id":                        case_db_id,
                    "court_room_hearing_id":           court_id,
                    "date_of_hearing":                 _parse_date(h.get("dateOfHearing")),
                    "date_of_next_hearing":            _parse_date(h.get("dateOfNextHearing")),
                    "case_stage":                      h.get("caseStage"),
                    "proceeding_text":                 _sanitize_html(h.get("proceedingText")),
                    "daily_order_status":              h.get("dailyOrderStatus"),
                    "order_type_id":                   h.get("orderTypeId"),
                    "daily_order_availability_status": h.get("dailyOrderAvailabilityStatus"),
                    "hearing_sequence_number":         h.get("hearingSequenceNumber") or 0,
                }
                hearing_db_id = upsert_hearing(session, hearing_data)

                # Create daily_order stub for PDF fetch if available
                if (
                    h.get("dailyOrderAvailabilityStatus") == 2
                    and filing_ref
                    and h.get("dateOfHearing")
                ):
                    order_data: dict[str, Any] = {
                        "case_id":                case_db_id,
                        "hearing_id":             hearing_db_id,
                        "filing_reference_number": filing_ref,
                        "date_of_hearing":        _parse_date(h["dateOfHearing"]),
                        "order_type_id":           h.get("orderTypeId") or 1,
                        "pdf_fetched":             False,
                    }
                    upsert_daily_order(session, order_data)

        return "updated"

    except Exception as exc:
        log.error("detail_persist_failed", error=str(exc))
        with get_session() as session:
            log_ingestion_error(
                session,
                run_id=run_id,
                case_id=case_db_id,
                endpoint=_PATH,
                error_type=ErrorType.db_error,
                error_message=str(exc),
            )
        return "failed"


def run(
    client: EJagritiClient,
    run_id: int,
    dry_run: bool = False,
    daily_budget: int = 3500,
) -> dict[str, int]:
    """
    Execute the fetch_case_detail job for all open and pending cases.

    Cases are queried in id-order chunks of _CHUNK_SIZE to keep memory usage
    bounded. All open/pending cases are covered in a single daily run.

    Args:
        client: Authenticated eJagriti HTTP client.
        run_id: Current IngestionRun.id.
        dry_run: Skip DB writes when True.
        daily_budget: Daily call budget for interval calculation.

    Returns:
        Dict with ``fetched``, ``updated``, ``skipped``, ``failed`` counts.
    """
    stats = {"fetched": 0, "updated": 0, "skipped": 0, "failed": 0}
    log = logger.bind(job="fetch_case_detail", run_id=run_id, dry_run=dry_run)

    last_id = 0
    chunk_num = 0

    while True:
        # Fetch next chunk of open/pending cases ordered by id (stable cursor)
        with get_session(read_only=True) as session:
            rows = session.execute(
                select(Case.id, Case.case_number, Case.data_hash, Case.filing_reference_number)
                .where(
                    Case.status.in_(["open", "pending"]),
                    Case.id > last_id,
                )
                .order_by(Case.id.asc())
                .limit(_CHUNK_SIZE)
            ).all()

        if not rows:
            break

        chunk_num += 1
        log.info("detail_chunk_start", chunk=chunk_num, count=len(rows), from_id=last_id + 1)

        for row in rows:
            time.sleep(calculate_interval(daily_budget))

            try:
                resp = client.get(_PATH, params={"caseNumber": row.case_number})
                stats["fetched"] += 1
            except PermissionError as exc:
                log.error("detail_forbidden", case_number=row.case_number)
                with get_session() as session:
                    log_failed_job(
                        session,
                        job_type=JobType.fetch_case_detail,
                        endpoint=_PATH,
                        reason=str(exc),
                        case_id=row.id,
                        params={"caseNumber": row.case_number},
                    )
                stats["failed"] += 1
                continue
            except Exception as exc:
                log.error("detail_fetch_error", case_number=row.case_number, error=str(exc))
                with get_session() as session:
                    log_ingestion_error(
                        session,
                        run_id=run_id,
                        case_id=row.id,
                        endpoint=_PATH,
                        error_type=ErrorType.http_error,
                        error_message=str(exc),
                    )
                stats["failed"] += 1
                continue

            if resp.get("status") != 200 or not resp.get("data"):
                log.warning("detail_empty_response", case_number=row.case_number)
                stats["failed"] += 1
                continue

            result = _process_detail(
                case_db_id=row.id,
                case_number=row.case_number,
                data=resp["data"],
                existing_hash=row.data_hash,
                run_id=run_id,
                dry_run=dry_run,
            )
            stats[result] = stats.get(result, 0) + 1

        last_id = rows[-1].id
        log.info("detail_chunk_done", chunk=chunk_num, last_id=last_id, **stats)

    log.info("fetch_case_detail_complete", chunks=chunk_num, **stats)
    return stats
