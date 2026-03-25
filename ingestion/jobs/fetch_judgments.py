"""
Job: fetch_judgments

The eJagriti API does not have a separate judgment endpoint distinct from
the daily-order PDF endpoint. A "judgment" is the final daily order issued
when a case's stage changes to a terminal state (DISPOSED, DECIDED, etc.).

This job identifies such orders and re-fetches them with orderTypeId=2
(judgment type) if not already stored, then marks the associated daily_order
row with a note.

If the portal later exposes a dedicated judgment endpoint, this job can be
extended without changing the rest of the pipeline.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from sqlalchemy import select, text

from client import EJagritiClient, calculate_interval
from db.models import Case, CaseStatus, DailyOrder
from db.session import get_session
from db.upsert import upsert_daily_order

logger = structlog.get_logger(__name__)

_JUDGMENT_ORDER_TYPE_ID = 2  # orderTypeId=2 is the judgment/final order type


def _get_closed_cases_without_judgment(limit: int = 50) -> list[dict[str, Any]]:
    """
    Return closed cases that have a filing_reference_number but no
    daily_order row with order_type_id=2 (judgment).

    Args:
        limit: Max rows to return.

    Returns:
        List of dicts with case details for judgment fetch.
    """
    with get_session(read_only=True) as session:
        # Cases that are closed, have a filing ref, and don't yet have a
        # judgment-type daily order row
        rows = session.execute(
            text("""
                SELECT c.id,
                       c.case_number,
                       c.filing_reference_number,
                       c.date_of_next_hearing
                  FROM cases c
                 WHERE c.status = 'closed'
                   AND c.filing_reference_number IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM daily_orders d
                        WHERE d.case_id = c.id
                          AND d.order_type_id = :jtype
                   )
                 ORDER BY c.updated_at DESC
                 LIMIT :lim
            """),
            {"jtype": _JUDGMENT_ORDER_TYPE_ID, "lim": limit},
        ).fetchall()
    return [
        {
            "id": r.id,
            "case_number": r.case_number,
            "filing_reference_number": r.filing_reference_number,
            "date_of_next_hearing": r.date_of_next_hearing,
        }
        for r in rows
    ]


def run(
    client: EJagritiClient,
    run_id: int,
    dry_run: bool = False,
    daily_budget: int = 3500,
) -> dict[str, int]:
    """
    Execute the fetch_judgments job.

    For each closed case without a judgment-type daily_order, creates a
    daily_orders stub row with order_type_id=2 so that fetch_orders will
    pick it up and download the PDF on the next cycle.

    Args:
        client: Authenticated eJagriti HTTP client (not used directly here;
                the actual PDF fetch is delegated to fetch_orders).
        run_id: Current IngestionRun.id.
        dry_run: Skip DB writes when True.
        daily_budget: Used for interval calculation if direct API calls
                      are added in the future.

    Returns:
        Dict with ``queued`` and ``skipped`` counts.
    """
    stats = {"queued": 0, "skipped": 0}
    log = logger.bind(job="fetch_judgments", run_id=run_id, dry_run=dry_run)

    cases = _get_closed_cases_without_judgment()
    if not cases:
        log.info("no_closed_cases_needing_judgment")
        return stats

    log.info("judgment_queue_start", count=len(cases))

    for case in cases:
        if dry_run:
            log.debug("dry_run_skip_judgment", case_number=case["case_number"])
            stats["skipped"] += 1
            continue

        # We need a hearing date to call the PDF endpoint.
        # Use the most recent hearing date available for this case.
        with get_session(read_only=True) as session:
            latest_hearing = session.execute(
                select(DailyOrder.date_of_hearing)
                .where(
                    DailyOrder.case_id == case["id"],
                    DailyOrder.pdf_fetched.is_(True),
                )
                .order_by(DailyOrder.date_of_hearing.desc())
                .limit(1)
            ).scalar_one_or_none()

        if not latest_hearing:
            log.debug("no_hearing_date_for_judgment", case_number=case["case_number"])
            stats["skipped"] += 1
            continue

        order_data: dict[str, Any] = {
            "case_id":                case["id"],
            "hearing_id":             None,
            "filing_reference_number": case["filing_reference_number"],
            "date_of_hearing":        latest_hearing,
            "order_type_id":          _JUDGMENT_ORDER_TYPE_ID,
            "pdf_fetched":            False,
        }

        try:
            with get_session() as session:
                upsert_daily_order(session, order_data)
            stats["queued"] += 1
            log.debug("judgment_queued", case_number=case["case_number"])
        except Exception as exc:
            log.error("judgment_queue_failed", case_number=case["case_number"], error=str(exc))
            stats["skipped"] += 1

    log.info("fetch_judgments_complete", **stats)
    return stats
