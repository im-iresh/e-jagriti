"""
Job: fetch_voc

Fetches VOC (Voice of Customer) complaint records and links them to cases
in the DB by matching state_id + court_name → commission → full case_number.

Matching logic:
  1. Look up commissions WHERE state_id = :state_id AND name_en ILIKE :court_name
  2. Construct full case_number: commission.case_prefix_text + "/" + case_number_raw
  3. Look up cases WHERE case_number = :full_case_number
  4. Set match_status: matched / unmatched / ambiguous

NOTE: The API is not yet finalised. Data currently comes from _DUMMY_VOC_SOURCE.
To integrate the real API, replace _fetch_voc_data() only — no other changes needed.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import select, update as sa_update

from client import EJagritiClient
from db.models import Case, Commission, VocMatchStatus
from db.session import get_session
from db.upsert import upsert_voc_complaint

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Dummy data — replace _fetch_voc_data() when the real API is ready
# ---------------------------------------------------------------------------

_DUMMY_VOC_SOURCE: list[dict[str, Any]] = [
    {
        "vocNumber": 310256328,
        "stateId": 9,
        "courtName": "District Consumer Disputes Redressal Commission, Agra",
        "caseNumberRaw": "CC/104/2025",
        "complainantName": "Ramesh Kumar",
        "productCategory": "Mobile Phone",
        "complaintDate": "2025-03-01",
    },
    {
        "vocNumber": 310256329,
        "stateId": 7,
        "courtName": "Delhi State Consumer Disputes Redressal Commission",
        "caseNumberRaw": "CC/22/2024",
        "complainantName": "Sunita Sharma",
        "productCategory": "Television",
        "complaintDate": "2024-11-15",
    },
    {
        "vocNumber": 310256330,
        "stateId": 27,
        "courtName": "District Consumer Disputes Redressal Commission, Pune",
        "caseNumberRaw": "CC/88/2025",
        "complainantName": "Priya Mehta",
        "productCategory": "Refrigerator",
        "complaintDate": "2025-01-20",
    },
]


def _fetch_voc_data() -> list[dict[str, Any]]:
    """
    Return VOC complaint records to process.

    Currently returns hardcoded dummy data.
    Replace the body of this function with a real API call when ready:

        resp = client.get("/voc/api/complaints", params={"company": "samsung"})
        return resp.get("data", [])
    """
    return _DUMMY_VOC_SOURCE


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _find_matching_case(
    state_id: int | None,
    court_name: str | None,
    case_number_raw: str | None,
) -> tuple[int | None, VocMatchStatus]:
    """
    Resolve a VOC's court name + case number to an internal case id.

    Steps:
      1. Find commission(s) by state_id + name_en ILIKE court_name.
      2. Construct full case_number from commission.case_prefix_text + case_number_raw.
      3. Look up case by full case_number.

    Args:
        state_id: State ID from the VOC record.
        court_name: Court/commission name from the VOC record.
        case_number_raw: Case number without commission prefix (e.g. "CC/104/2025").

    Returns:
        Tuple of (case_id or None, VocMatchStatus).
    """
    if not state_id or not court_name or not case_number_raw:
        return None, VocMatchStatus.unmatched

    with get_session(read_only=True) as session:
        # Step 1 — find commission(s) by state + name
        commissions = session.execute(
            select(Commission.id, Commission.name_en, Commission.case_prefix_text)
            .where(
                Commission.state_id == state_id,
                Commission.name_en.ilike(f"%{court_name}%"),
            )
        ).all()

        if not commissions:
            return None, VocMatchStatus.unmatched

        status = VocMatchStatus.matched
        if len(commissions) > 1:
            status = VocMatchStatus.ambiguous
            logger.warning(
                "voc_ambiguous_commission_match",
                state_id=state_id,
                court_name=court_name,
                matches=[r.name_en for r in commissions],
            )

        # Use first match (best or only result)
        commission = commissions[0]

        if not commission.case_prefix_text:
            logger.warning(
                "voc_commission_no_prefix",
                commission_id=commission.id,
                court_name=court_name,
            )
            return None, VocMatchStatus.unmatched

        # Step 2 — construct full case_number
        full_case_number = f"{commission.case_prefix_text}/{case_number_raw}"

        # Step 3 — look up case
        case_id = session.execute(
            select(Case.id).where(Case.case_number == full_case_number)
        ).scalar_one_or_none()

    if case_id is None:
        return None, VocMatchStatus.unmatched

    return case_id, status


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------

def run(
    client: EJagritiClient,
    run_id: int,
    dry_run: bool = False,
    daily_budget: int = 3500,
) -> dict[str, int]:
    """
    Execute the fetch_voc job.

    Fetches VOC complaint records (currently from dummy data), matches each
    to a case in the DB, and upserts a voc_complaints row with the result.

    Args:
        client: eJagriti HTTP client (unused until real API is integrated).
        run_id: Current IngestionRun.id for logging context.
        dry_run: When True, perform matching but skip all DB writes.
        daily_budget: Unused (no rate-limited API calls in current stub).

    Returns:
        Dict with ``upserted``, ``matched``, ``unmatched``, ``failed`` counts.
    """
    stats: dict[str, int] = {"upserted": 0, "matched": 0, "unmatched": 0, "failed": 0}
    log = logger.bind(job="fetch_voc", run_id=run_id, dry_run=dry_run)

    records = _fetch_voc_data()
    if not records:
        log.info("no_voc_records")
        return stats

    log.info("fetch_voc_start", count=len(records))

    for record in records:
        voc_number = record.get("vocNumber")
        if not voc_number:
            log.warning("voc_missing_number", record=record)
            stats["failed"] += 1
            continue

        state_id       = record.get("stateId")
        court_name     = record.get("courtName")
        case_number_raw = record.get("caseNumberRaw")

        case_id, match_status = _find_matching_case(state_id, court_name, case_number_raw)

        log.debug(
            "voc_match_result",
            voc_number=voc_number,
            match_status=match_status.value,
            case_id=case_id,
        )

        if match_status == VocMatchStatus.matched:
            stats["matched"] += 1
        else:
            stats["unmatched"] += 1

        if dry_run:
            log.debug("dry_run_skip_voc", voc_number=voc_number, match_status=match_status.value)
            continue

        voc_data: dict[str, Any] = {
            "voc_number":      voc_number,
            "case_id":         case_id,
            "state_id":        state_id,
            "court_name":      court_name,
            "case_number_raw": case_number_raw,
            "match_status":    match_status,
            "raw_payload":     json.dumps(record),
        }

        try:
            with get_session() as session:
                upsert_voc_complaint(session, voc_data)
                if case_id is not None:
                    session.execute(
                        sa_update(Case)
                        .where(Case.id == case_id)
                        .values(voc_number=voc_number)
                    )
            stats["upserted"] += 1
        except Exception as exc:
            log.error("voc_upsert_failed", voc_number=voc_number, error=str(exc))
            stats["failed"] += 1

    log.info("fetch_voc_complete", **stats)
    return stats
