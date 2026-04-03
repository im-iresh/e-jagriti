"""
Job: fetch_cases

For every commission in the DB, calls:
  GET /report/report/getCauseTitleListByCompany
    ?commissionTypeId=N&commissionId=N
    &filingDate1=YYYY-MM-DD&filingDate2=YYYY-MM-DD
    &complainant_respondent_name_en=samsung

Returns lightweight case-list rows. Upserts into `cases` with list-level
fields only; filing_reference_number is NOT available here — it is set
later by fetch_case_detail.

The date window defaults to [yesterday, today] so each daily run only
scans the previous day's filings. Override via EJAGRITI_FETCH_CASES_FROM_DATE
(e.g. set to 2015-01-01 for a one-off historical backfill).
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select, text

from client import EJagritiClient, calculate_interval
from db.models import Case, Commission, CommissionType, ErrorType, JobType
from db.session import get_session
from db.upsert import log_failed_job, log_ingestion_error, upsert_case

logger = structlog.get_logger(__name__)

_PATH = "/report/report/getCauseTitleListByCompany"
_SEARCH_KEYWORD = os.environ.get("EJAGRITI_SEARCH_KEYWORD", "samsung")

_TYPE_ID_MAP: dict[CommissionType, int] = {
    CommissionType.national: 1,
    CommissionType.state: 2,
    CommissionType.district: 3,
}


def _map_status(stage_name: str | None) -> str:
    """
    Derive a case status from the free-text stage name returned by the API.

    Args:
        stage_name: Raw case_stage_name string (e.g. "DISPOSED OFF", "REGISTERED").

    Returns:
        One of "open", "closed", "pending".
    """
    if not stage_name:
        return "pending"
    stage_upper = stage_name.upper()
    closed_keywords = ("DISPOSED", "DISMISSED", "WITHDRAWN", "CLOSED", "DECIDED", "ALLOWED", "REJECTED")
    if any(kw in stage_upper for kw in closed_keywords):
        return "closed"
    if stage_upper in ("REGISTERED", "ADMIT", "NOTICE ISSUED"):
        return "open"
    return "pending"


def _parse_date(value: str | None) -> date | None:
    """
    Parse a YYYY-MM-DD date string from the API.

    Args:
        value: ISO date string or None.

    Returns:
        date object or None.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _get_all_commissions() -> list[dict[str, Any]]:
    """
    Return all commissions from the DB as a list of dicts.

    Returns:
        List of dicts with keys: id, commission_id_ext, commission_type.
    """
    with get_session(read_only=True) as session:
        rows = session.execute(
            select(Commission.id, Commission.commission_id_ext, Commission.commission_type)
        ).all()
    return [{"id": r.id, "ext": r.commission_id_ext, "type": r.commission_type} for r in rows]


def run(
    client: EJagritiClient,
    run_id: int,
    dry_run: bool = False,
    daily_budget: int = 3500,
) -> dict[str, int]:
    """
    Execute the fetch_cases job for all commissions.

    Args:
        client: Authenticated eJagriti HTTP client.
        run_id: Current IngestionRun.id for error logging.
        dry_run: When True, fetch but skip DB writes.
        daily_budget: Total daily call budget (used for interval calculation).

    Returns:
        Dict with ``fetched``, ``upserted``, ``failed`` counts.
    """
    stats = {"fetched": 0, "upserted": 0, "failed": 0}
    log = logger.bind(job="fetch_cases", run_id=run_id, dry_run=dry_run)

    from_date = os.environ.get(
        "EJAGRITI_FETCH_CASES_FROM_DATE",
        (date.today() - timedelta(days=1)).isoformat(),
    )
    to_date = date.today().isoformat()

    commissions = _get_all_commissions()
    if not commissions:
        log.warning("no_commissions_in_db")
        return stats

    log.info("fetch_cases_start", commission_count=len(commissions), from_date=from_date, to_date=to_date)

    for comm in commissions:
        comm_type_id = _TYPE_ID_MAP.get(comm["type"], 3)
        params: dict[str, Any] = {
            "commissionTypeId":              comm_type_id,
            "commissionId":                  comm["ext"],
            "filingDate1":                   from_date,
            "filingDate2":                   to_date,
            "complainant_respondent_name_en": _SEARCH_KEYWORD,
        }

        time.sleep(calculate_interval(daily_budget))

        try:
            resp = client.get(_PATH, params=params)
            case_list: list[dict] = resp if isinstance(resp, list) else resp.get("data", [])
        except PermissionError as exc:
            log.error("cases_forbidden", commission_ext=comm["ext"], error=str(exc))
            with get_session() as session:
                log_failed_job(
                    session,
                    job_type=JobType.fetch_cases,
                    endpoint=_PATH,
                    reason=str(exc),
                    commission_id=comm["id"],
                    params=params,
                )
            stats["failed"] += 1
            continue
        except Exception as exc:
            log.error("cases_fetch_error", commission_ext=comm["ext"], error=str(exc))
            with get_session() as session:
                log_ingestion_error(
                    session,
                    run_id=run_id,
                    case_id=None,
                    endpoint=_PATH,
                    error_type=ErrorType.http_error,
                    error_message=str(exc),
                    request_payload=str(params),
                )
            stats["failed"] += 1
            continue

        stats["fetched"] += len(case_list)

        for item in case_list:
            case_data: dict[str, Any] = {
                "case_number":          item.get("case_number") or item.get("caseNumber", ""),
                "file_application_number": item.get("file_application_number"),
                "commission_id":        comm["id"],
                "case_type_name":       item.get("case_type_name"),
                "case_stage_name":      item.get("case_stage_name"),
                "case_category_name":   item.get("case_category_name"),
                "filing_date":          _parse_date(item.get("case_filing_date")),
                "date_of_next_hearing": _parse_date(item.get("date_of_next_hearing")),
                "complainant_name":     item.get("complainant_name"),
                "respondent_name":      item.get("respondent_name"),
                "complainant_advocate_names": (
                    f'["{item["complainant_advocate_name"]}"]'
                    if item.get("complainant_advocate_name")
                    else None
                ),
                "respondent_advocate_names": (
                    f'["{item["respondent_advocate_name"]}"]'
                    if item.get("respondent_advocate_name")
                    else None
                ),
                "status": _map_status(item.get("case_stage_name")),
            }

            if not case_data["case_number"]:
                log.warning("skip_empty_case_number", item=item)
                continue

            if not dry_run:
                try:
                    with get_session() as session:
                        upsert_case(session, case_data)
                    stats["upserted"] += 1
                except Exception as exc:
                    log.error(
                        "case_upsert_failed",
                        case_number=case_data["case_number"],
                        error=str(exc),
                    )
                    stats["failed"] += 1
            else:
                log.debug("dry_run_skip_case", case_number=case_data["case_number"])
                stats["upserted"] += 1

    log.info("fetch_cases_complete", **stats)
    return stats
