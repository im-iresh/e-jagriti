"""
All SQLAlchemy query functions for the Flask API.

Design rule: no query logic lives in route handlers. Routes call these
functions and pass the results to serialisers. This keeps routes thin and
queries independently testable.

All read operations use get_session(read_only=True) to route to the replica
when one is configured.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from db.session import get_session

# ORM models re-exported through api/models.py which resolves the ingestion
# package path at import time (see that file for path logic).
from models import Case, Commission, DailyOrder, FailedJob, IngestionError, IngestionRun, VocComplaint, VocMatchStatus  # noqa: E402

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def get_cases_paginated(
    page: int = 1,
    per_page: int = 20,
    status: str | None = None,
    commission_type: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """
    Return a paginated list of cases for the homepage table.

    Args:
        page: 1-based page number.
        per_page: Rows per page (max 100).
        status: Filter by "open", "closed", or "pending". None = all.
        commission_type: Filter by "national", "state", or "district". None = all.
        search: Free-text search on case_number and complainant_name.

    Returns:
        Dict with ``items`` (list of case dicts) and ``total`` row count.
    """
    with get_session(read_only=True) as session:
        query = (
            select(
                Case.id,
                Case.case_number,
                Case.complainant_name,
                Case.case_stage_name,
                Case.filing_date,
                Case.date_of_next_hearing,
                Case.status,
                Case.updated_at,
                Commission.name_en.label("commission_name"),
                Commission.commission_type.label("commission_type"),
            )
            .join(Commission, Case.commission_id == Commission.id)
        )

        if status and status != "all":
            query = query.where(Case.status == status)
        if commission_type:
            query = query.where(Commission.commission_type == commission_type)
        if search:
            like = f"%{search}%"
            query = query.where(
                (Case.case_number.ilike(like)) | (Case.complainant_name.ilike(like))
            )

        total: int = session.execute(
            select(func.count()).select_from(query.subquery())
        ).scalar_one()

        offset = (page - 1) * per_page
        rows = session.execute(
            query.order_by(Case.filing_date.desc().nullslast())
            .offset(offset)
            .limit(per_page)
        ).all()

    items = [
        {
            "case_id":          r.id,
            "case_number":      r.case_number,
            "complainant_name": r.complainant_name,
            "commission_name":  r.commission_name,
            "commission_type":  r.commission_type,
            "filing_date":      r.filing_date.isoformat() if r.filing_date else None,
            "date_of_next_hearing": r.date_of_next_hearing.isoformat() if r.date_of_next_hearing else None,
            "status":           r.status,
            "case_stage":       r.case_stage_name,
            "last_updated":     r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
    return {"items": items, "total": total}


def get_case_by_id(case_id: int) -> dict[str, Any] | None:
    """
    Return the full nested case object for the detail endpoint.

    Loads the case with its commission, hearings (ordered by sequence),
    and daily_orders in a single query using joinedload to avoid N+1.

    Args:
        case_id: Internal surrogate id of the case.

    Returns:
        Nested dict or None if not found.
    """
    with get_session(read_only=True) as session:
        case = session.execute(
            select(Case)
            .options(
                joinedload(Case.commission),
                joinedload(Case.hearings),
                joinedload(Case.daily_orders),
            )
            .where(Case.id == case_id)
        ).unique().scalar_one_or_none()

        if not case:
            return None

        commission = case.commission
        hearings = sorted(case.hearings, key=lambda h: h.hearing_sequence_number)
        orders = sorted(
            case.daily_orders,
            key=lambda o: o.date_of_hearing or date.min,
        )

        complainant_advocates: list[str] = []
        respondent_advocates: list[str] = []
        try:
            if case.complainant_advocate_names:
                complainant_advocates = json.loads(case.complainant_advocate_names)
            if case.respondent_advocate_names:
                respondent_advocates = json.loads(case.respondent_advocate_names)
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "case_id":          case.id,
            "case_number":      case.case_number,
            "filing_date":      case.filing_date.isoformat() if case.filing_date else None,
            "date_of_cause":    case.date_of_cause.isoformat() if case.date_of_cause else None,
            "status":           case.status,
            "case_stage":       case.case_stage_name,
            "case_category":    case.case_category_name,
            "date_of_next_hearing": case.date_of_next_hearing.isoformat() if case.date_of_next_hearing else None,
            "commission": {
                "id":   commission.id,
                "ext_id": commission.commission_id_ext,
                "name": commission.name_en,
                "type": commission.commission_type,
                "state_id": commission.state_id,
            },
            "complainant": {
                "name":                case.complainant_name,
                "advocate_names":      complainant_advocates,
            },
            "respondent": {
                "name":                case.respondent_name,
                "advocate_names":      respondent_advocates,
            },
            "hearings": [
                {
                    "id":                             h.id,
                    "court_room_hearing_id":          h.court_room_hearing_id,
                    "date":                           h.date_of_hearing.isoformat() if h.date_of_hearing else None,
                    "next_date":                      h.date_of_next_hearing.isoformat() if h.date_of_next_hearing else None,
                    "case_stage":                     h.case_stage,
                    "proceeding_text":                h.proceeding_text,
                    "sequence_number":                h.hearing_sequence_number,
                    "daily_order_available":          h.daily_order_availability_status == 2,
                }
                for h in hearings
            ],
            "daily_orders": [
                {
                    "id":                      o.id,
                    "date":                    o.date_of_hearing.isoformat() if o.date_of_hearing else None,
                    "order_type_id":           o.order_type_id,
                    "pdf_fetched":             o.pdf_fetched,
                    "pdf_storage_path":        o.pdf_storage_path,
                    "pdf_fetched_at":          o.pdf_fetched_at.isoformat() if o.pdf_fetched_at else None,
                }
                for o in orders
            ],
            "last_fetched_at": case.last_fetched_at.isoformat() if case.last_fetched_at else None,
        }


def get_case_by_number(case_number: str) -> dict[str, Any] | None:
    """
    Look up a case by its case_number string and delegate to get_case_by_id.

    Args:
        case_number: e.g. "DC/77/CC/104/2025"

    Returns:
        Nested case dict or None.
    """
    with get_session(read_only=True) as session:
        row = session.execute(
            select(Case.id).where(Case.case_number == case_number)
        ).scalar_one_or_none()

    if not row:
        return None
    return get_case_by_id(row)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def get_orders_for_case(
    case_id: int,
    from_date: date | None = None,
    to_date: date | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict[str, Any] | None:
    """
    Return paginated daily orders for a case.

    Args:
        case_id: Internal case id.
        from_date: Filter orders on or after this date.
        to_date: Filter orders on or before this date.
        page: 1-based page number.
        per_page: Rows per page.

    Returns:
        Dict with ``items`` and ``total``, or None if case not found.
    """
    with get_session(read_only=True) as session:
        # Verify case exists
        exists = session.execute(
            select(Case.id).where(Case.id == case_id)
        ).scalar_one_or_none()
        if not exists:
            return None

        q = select(DailyOrder).where(DailyOrder.case_id == case_id)
        if from_date:
            q = q.where(DailyOrder.date_of_hearing >= from_date)
        if to_date:
            q = q.where(DailyOrder.date_of_hearing <= to_date)

        total = session.execute(
            select(func.count()).select_from(q.subquery())
        ).scalar_one()

        rows = session.execute(
            q.order_by(DailyOrder.date_of_hearing.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).scalars().all()

    items = [
        {
            "id":               o.id,
            "date":             o.date_of_hearing.isoformat() if o.date_of_hearing else None,
            "order_type_id":    o.order_type_id,
            "pdf_fetched":      o.pdf_fetched,
            "pdf_storage_path": o.pdf_storage_path,
            "pdf_fetched_at":   o.pdf_fetched_at.isoformat() if o.pdf_fetched_at else None,
            "pdf_fetch_error":  o.pdf_fetch_error,
        }
        for o in rows
    ]
    return {"items": items, "total": total}


# ---------------------------------------------------------------------------
# Judgment
# ---------------------------------------------------------------------------

def get_judgment_for_case(case_id: int) -> dict[str, Any] | None:
    """
    Return the judgment (orderTypeId=2) daily order for a case.

    Args:
        case_id: Internal case id.

    Returns:
        Order dict or None if no judgment order exists for this case.
    """
    with get_session(read_only=True) as session:
        exists = session.execute(
            select(Case.id).where(Case.id == case_id)
        ).scalar_one_or_none()
        if not exists:
            return None

        row = session.execute(
            select(DailyOrder)
            .where(DailyOrder.case_id == case_id, DailyOrder.order_type_id == 2)
            .order_by(DailyOrder.date_of_hearing.desc())
            .limit(1)
        ).scalar_one_or_none()

    if not row:
        return {}  # Case exists but no judgment yet

    return {
        "id":               row.id,
        "date":             row.date_of_hearing.isoformat() if row.date_of_hearing else None,
        "pdf_fetched":      row.pdf_fetched,
        "pdf_storage_path": row.pdf_storage_path,
        "pdf_fetched_at":   row.pdf_fetched_at.isoformat() if row.pdf_fetched_at else None,
    }


# ---------------------------------------------------------------------------
# Commissions
# ---------------------------------------------------------------------------

def get_all_commissions() -> list[dict[str, Any]]:
    """
    Return all commissions ordered by type and name.

    This result is cached at the route level (TTL 1 h).

    Returns:
        List of commission dicts.
    """
    with get_session(read_only=True) as session:
        rows = session.execute(
            select(Commission).order_by(Commission.commission_type, Commission.name_en)
        ).scalars().all()

    return [
        {
            "id":                             r.id,
            "commission_id_ext":              r.commission_id_ext,
            "name":                           r.name_en,
            "type":                           r.commission_type,
            "state_id":                       r.state_id,
            "district_id":                    r.district_id,
            "case_prefix_text":               r.case_prefix_text,
            "parent_commission_id":           r.parent_commission_id,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats() -> dict[str, Any]:
    """
    Return aggregate statistics for the /api/stats endpoint.

    Queries:
      - Total / open / closed / pending case counts
      - Case counts grouped by commission_type
      - Cases filed per month for the last 12 months

    This result is cached at the route level (TTL 1 h).

    Returns:
        Nested dict with counts and time-series data.
    """
    with get_session(read_only=True) as session:
        # Overall counts
        total = session.execute(select(func.count(Case.id))).scalar_one()
        status_rows = session.execute(
            select(Case.status, func.count(Case.id).label("cnt"))
            .group_by(Case.status)
        ).all()
        status_counts = {r.status: r.cnt for r in status_rows}

        # By commission type
        type_rows = session.execute(
            select(Commission.commission_type, func.count(Case.id).label("cnt"))
            .join(Case, Case.commission_id == Commission.id)
            .group_by(Commission.commission_type)
        ).all()
        by_type = {r.commission_type: r.cnt for r in type_rows}

        # Filed per month — last 12 calendar months
        monthly_rows = session.execute(
            text("""
                SELECT to_char(filing_date, 'YYYY-MM') AS month,
                       count(*)                         AS cnt
                  FROM cases
                 WHERE filing_date >= (CURRENT_DATE - INTERVAL '12 months')
                   AND filing_date IS NOT NULL
                 GROUP BY 1
                 ORDER BY 1
            """)
        ).fetchall()
        monthly = [{"month": r.month, "count": r.cnt} for r in monthly_rows]

        # Last ingestion run summary
        last_run = session.execute(
            select(IngestionRun)
            .order_by(IngestionRun.run_started_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    last_run_data = None
    if last_run:
        last_run_data = {
            "run_id":          last_run.id,
            "started_at":      last_run.run_started_at.isoformat(),
            "finished_at":     last_run.run_finished_at.isoformat() if last_run.run_finished_at else None,
            "total_calls":     last_run.total_calls,
            "success_count":   last_run.success_count,
            "fail_count":      last_run.fail_count,
            "duration_seconds": last_run.duration_seconds,
        }

    return {
        "total_cases":   total,
        "open_cases":    status_counts.get("open", 0),
        "closed_cases":  status_counts.get("closed", 0),
        "pending_cases": status_counts.get("pending", 0),
        "by_commission_type": {
            "national": by_type.get("national", 0),
            "state":    by_type.get("state", 0),
            "district": by_type.get("district", 0),
        },
        "cases_per_month": monthly,
        "last_ingestion_run": last_run_data,
    }


# ---------------------------------------------------------------------------
# VOC attachment (write path — uses primary DB)
# ---------------------------------------------------------------------------

def attach_voc_to_case(case_id: int, voc_number: int, cms_payload: dict) -> dict[str, Any]:
    """
    Manually link a VOC complaint to a case.

    Upserts the voc_complaints row (match_status=matched) and stamps
    cases.voc_number so the no-VOC alert index stays accurate.

    Uses get_session() without read_only so writes go to the primary DB.

    Args:
        case_id:     Internal surrogate id of the target case.
        voc_number:  VOC complaint number from the CMS.
        cms_payload: Full JSON response from the CMS (stored as raw_payload).

    Returns:
        Dict with ``case_id`` and ``voc_number``.

    Raises:
        LookupError:        case_id does not exist in the DB.
        ValueError("conflict"): voc_number is already linked to a different case.
    """
    with get_session() as session:
        # 1. Verify case exists
        exists = session.execute(
            select(Case.id).where(Case.id == case_id)
        ).scalar_one_or_none()
        if not exists:
            raise LookupError(f"Case {case_id} not found")

        # 2. Conflict check — VOC already linked to a different case?
        linked_case_id = session.execute(
            select(VocComplaint.case_id).where(VocComplaint.voc_number == voc_number)
        ).scalar_one_or_none()
        if linked_case_id is not None and linked_case_id != case_id:
            raise ValueError("conflict")

        # 3. Upsert voc_complaints row
        stmt = (
            pg_insert(VocComplaint)
            .values(
                voc_number=voc_number,
                case_id=case_id,
                match_status=VocMatchStatus.matched,
                raw_payload=json.dumps(cms_payload),
            )
            .on_conflict_do_update(
                index_elements=["voc_number"],
                set_={
                    "case_id":      pg_insert(VocComplaint).excluded.case_id,
                    "match_status": pg_insert(VocComplaint).excluded.match_status,
                    "raw_payload":  pg_insert(VocComplaint).excluded.raw_payload,
                    "updated_at":   func.now(),
                },
            )
        )
        session.execute(stmt)

        # 4. Stamp cases.voc_number so the partial index stays accurate
        session.execute(
            sa_update(Case).where(Case.id == case_id).values(voc_number=voc_number)
        )

    return {"case_id": case_id, "voc_number": voc_number}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def get_alert_cases() -> dict[str, Any]:
    """
    Return open/pending cases grouped by two alert conditions.

    Sections:
      no_voc       — cases where voc_number IS NULL (no VOC complaint linked).
                     Uses the partial index idx_cases_no_voc; no join needed.
      hearing_soon — cases where date_of_next_hearing falls within the next
                     2 days (today through today + 2, inclusive).

    Closed cases are excluded from both sections.

    Returns:
        Dict with ``no_voc`` and ``hearing_soon`` keys, each containing
        ``count`` (int) and ``items`` (list of case dicts).
    """
    today = date.today()
    cutoff = today + timedelta(days=2)

    _cols = (
        Case.id,
        Case.case_number,
        Case.complainant_name,
        Case.case_stage_name,
        Case.date_of_next_hearing,
        Case.status,
        Commission.name_en.label("commission_name"),
        Commission.commission_type.label("commission_type"),
    )
    _base = (
        select(*_cols)
        .join(Commission, Case.commission_id == Commission.id)
        .where(Case.status.in_(["open", "pending"]))
    )

    with get_session(read_only=True) as session:
        no_voc_rows = session.execute(
            _base.where(Case.voc_number.is_(None))
            .order_by(Case.filing_date.desc().nullslast())
        ).all()

        hearing_rows = session.execute(
            _base.where(Case.date_of_next_hearing.between(today, cutoff))
            .order_by(Case.date_of_next_hearing.asc())
        ).all()

    def _serialize(r) -> dict[str, Any]:
        return {
            "case_id":              r.id,
            "case_number":          r.case_number,
            "complainant_name":     r.complainant_name,
            "commission_name":      r.commission_name,
            "commission_type":      r.commission_type,
            "date_of_next_hearing": r.date_of_next_hearing.isoformat() if r.date_of_next_hearing else None,
            "status":               r.status,
            "case_stage":           r.case_stage_name,
        }

    return {
        "no_voc": {
            "count": len(no_voc_rows),
            "items": [_serialize(r) for r in no_voc_rows],
        },
        "hearing_soon": {
            "count": len(hearing_rows),
            "items": [_serialize(r) for r in hearing_rows],
        },
    }


# ---------------------------------------------------------------------------
# Batch Status
# ---------------------------------------------------------------------------

def get_batch_status(runs: int = 10) -> dict[str, Any]:
    """
    Return live ingestion pipeline status for developer debugging.

    Queries:
      - Most recent N IngestionRun rows with all counters
      - Queue depths: cases needing detail fetch, PDFs pending, failed jobs
      - Last 20 IngestionError rows (most recent first)

    Args:
        runs: Number of most-recent IngestionRun rows to include (max 50).

    Returns:
        Dict with ``recent_runs``, ``queue_depths``, and ``recent_errors``.
    """
    with get_session(read_only=True) as session:
        run_rows = session.execute(
            select(IngestionRun)
            .order_by(IngestionRun.run_started_at.desc())
            .limit(runs)
        ).scalars().all()

        cases_pending_detail: int = session.execute(
            select(func.count(Case.id)).where(Case.last_fetched_at.is_(None))
        ).scalar_one()

        pdfs_pending: int = session.execute(
            select(func.count(DailyOrder.id)).where(DailyOrder.pdf_fetched.is_(False))
        ).scalar_one()

        failed_jobs_pending: int = session.execute(
            select(func.count(FailedJob.id)).where(FailedJob.resolved.is_(False))
        ).scalar_one()

        error_rows = session.execute(
            select(IngestionError)
            .order_by(IngestionError.created_at.desc())
            .limit(20)
        ).scalars().all()

    recent_runs = [
        {
            "run_id":           r.id,
            "started_at":       r.run_started_at.isoformat(),
            "finished_at":      r.run_finished_at.isoformat() if r.run_finished_at else None,
            "status":           "running" if r.run_finished_at is None else (
                                    "failed" if r.fail_count > 0 else "completed"
                                ),
            "trigger_mode":     r.trigger_mode.value,
            "total_calls":      r.total_calls,
            "success_count":    r.success_count,
            "fail_count":       r.fail_count,
            "skip_count":       r.skip_count,
            "duration_seconds": r.duration_seconds,
            "notes":            r.notes,
        }
        for r in run_rows
    ]

    recent_errors = [
        {
            "id":            e.id,
            "run_id":        e.run_id,
            "case_id":       e.case_id,
            "endpoint":      e.endpoint,
            "http_status":   e.http_status,
            "error_type":    e.error_type.value,
            "error_message": e.error_message,
            "retry_count":   e.retry_count,
            "created_at":    e.created_at.isoformat(),
        }
        for e in error_rows
    ]

    return {
        "recent_runs":  recent_runs,
        "queue_depths": {
            "cases_pending_detail_fetch": cases_pending_detail,
            "pdfs_pending_fetch":         pdfs_pending,
            "failed_jobs_unresolved":     failed_jobs_pending,
        },
        "recent_errors": recent_errors,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def get_health_data() -> dict[str, Any]:
    """
    Return DB connectivity status and last ingestion run summary.

    Returns:
        Dict with ``db_ok`` bool and ``last_run`` dict.
    """
    from db.session import check_db_connection
    db_ok = check_db_connection()

    last_run = None
    if db_ok:
        with get_session(read_only=True) as session:
            row = session.execute(
                select(IngestionRun)
                .order_by(IngestionRun.run_started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row:
                last_run = {
                    "run_id":           row.id,
                    "started_at":       row.run_started_at.isoformat(),
                    "finished_at":      row.run_finished_at.isoformat() if row.run_finished_at else None,
                    "total_calls":      row.total_calls,
                    "success_count":    row.success_count,
                    "fail_count":       row.fail_count,
                    "trigger_mode":     row.trigger_mode,
                    "duration_seconds": row.duration_seconds,
                }

    return {"db_ok": db_ok, "last_ingestion_run": last_run}
