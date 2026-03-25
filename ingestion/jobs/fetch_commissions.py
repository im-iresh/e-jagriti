"""
Job: fetch_commissions

Fetches all commissions from two eJagriti endpoints and upserts them:
  1. GET /master/master/v2/getAllCommission
     → national (NCDRC, commissionId=11000000) + state commissions
  2. GET /master/master/v2/getCommissionDetailsByStateId?stateId=N
     → state + district commissions with type, districtId, casePrefixText

Commission type inference:
  - commissionId == 11000000  → national  (NCDRC)
  - getCommissionDetailsByStateId returns commissionTypeId: 2=state, 3=district
  - getAllCommission entries not in getCommissionDetailsByStateId → state
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from client import EJagritiClient, calculate_interval
from db.models import CommissionType
from db.session import get_session
from db.upsert import (
    log_failed_job,
    log_ingestion_error,
    upsert_commission,
)
from db.models import ErrorType, JobType

logger = structlog.get_logger(__name__)

# eJagriti API paths
_PATH_ALL_COMMISSIONS = "/master/master/v2/getAllCommission"
_PATH_BY_STATE        = "/master/master/v2/getCommissionDetailsByStateId"

# The one national commission
_NCDRC_EXT_ID = 11000000


def _classify_top_level(commission_id_ext: int) -> CommissionType:
    """
    Infer commission type for entries returned by getAllCommission.

    NCDRC (11000000) is national; everything else at this level is state.

    Args:
        commission_id_ext: External commission ID from the API.

    Returns:
        CommissionType enum value.
    """
    if commission_id_ext == _NCDRC_EXT_ID:
        return CommissionType.national
    return CommissionType.state


def _api_type_to_enum(commission_type_id: int) -> CommissionType:
    """
    Map the numeric commissionTypeId from getCommissionDetailsByStateId.

    Args:
        commission_type_id: 1=national, 2=state, 3=district.

    Returns:
        CommissionType enum value.
    """
    mapping = {1: CommissionType.national, 2: CommissionType.state, 3: CommissionType.district}
    return mapping.get(commission_type_id, CommissionType.state)


def run(
    client: EJagritiClient,
    run_id: int,
    dry_run: bool = False,
    daily_budget: int = 3500,
) -> dict[str, int]:
    """
    Execute the fetch_commissions job.

    Steps:
      1. Call getAllCommission → upsert national + state rows.
      2. Collect distinct stateId values.
      3. For each stateId call getCommissionDetailsByStateId → upsert with
         full type / district detail and parent_commission_id linkage.

    Args:
        client: Authenticated eJagriti HTTP client.
        run_id: Current IngestionRun.id for error logging.
        dry_run: When True, fetch but skip all DB writes.
        daily_budget: Total daily call budget for interval calculation.

    Returns:
        Dict with ``upserted`` and ``failed`` counts.
    """
    stats = {"upserted": 0, "failed": 0}
    log = logger.bind(job="fetch_commissions", run_id=run_id, dry_run=dry_run)

    # ------------------------------------------------------------------
    # Step 1 — getAllCommission
    # ------------------------------------------------------------------
    try:
        resp = client.get(_PATH_ALL_COMMISSIONS)
        raw_list: list[dict] = resp.get("data", resp) if isinstance(resp, dict) else resp
    except PermissionError as exc:
        log.error("commission_list_forbidden", error=str(exc))
        with get_session() as session:
            log_failed_job(
                session,
                job_type=JobType.fetch_commissions,
                endpoint=_PATH_ALL_COMMISSIONS,
                reason=str(exc),
            )
        return stats
    except Exception as exc:
        log.error("commission_list_failed", error=str(exc))
        with get_session() as session:
            log_ingestion_error(
                session,
                run_id=run_id,
                case_id=None,
                endpoint=_PATH_ALL_COMMISSIONS,
                error_type=ErrorType.http_error,
                error_message=str(exc),
            )
        return stats

    # Map ext_id → internal id for parent linkage later
    ext_to_internal: dict[int, int] = {}

    for item in raw_list:
        ext_id: int = item["commissionId"]
        comm_type = _classify_top_level(ext_id)
        data: dict[str, Any] = {
            "commission_id_ext": ext_id,
            "name_en":           item["commissionNameEn"],
            "commission_type":   comm_type,
            "state_id":          item.get("stateId"),
        }
        if not dry_run:
            try:
                with get_session() as session:
                    row_id = upsert_commission(session, data)
                    ext_to_internal[ext_id] = row_id
                    stats["upserted"] += 1
            except Exception as exc:
                log.error("commission_upsert_failed", ext_id=ext_id, error=str(exc))
                with get_session() as session:
                    log_ingestion_error(
                        session,
                        run_id=run_id,
                        case_id=None,
                        endpoint=_PATH_ALL_COMMISSIONS,
                        error_type=ErrorType.db_error,
                        error_message=str(exc),
                    )
                stats["failed"] += 1
        else:
            log.debug("dry_run_skip_commission", ext_id=ext_id, name=item["commissionNameEn"])
            stats["upserted"] += 1

    log.info("top_level_commissions_done", upserted=stats["upserted"], failed=stats["failed"])

    # ------------------------------------------------------------------
    # Step 2 — getCommissionDetailsByStateId per unique stateId
    # ------------------------------------------------------------------
    state_ids: set[int] = {
        item["stateId"] for item in raw_list
        if item.get("stateId") and item["stateId"] != 0
    }

    for state_id in sorted(state_ids):
        time.sleep(calculate_interval(daily_budget))
        try:
            detail_resp = client.get(_PATH_BY_STATE, params={"stateId": state_id})
            detail_list: list[dict] = (
                detail_resp.get("data", detail_resp)
                if isinstance(detail_resp, dict)
                else detail_resp
            )
        except PermissionError as exc:
            log.error("state_detail_forbidden", state_id=state_id, error=str(exc))
            stats["failed"] += 1
            continue
        except Exception as exc:
            log.error("state_detail_failed", state_id=state_id, error=str(exc))
            with get_session() as session:
                log_ingestion_error(
                    session,
                    run_id=run_id,
                    case_id=None,
                    endpoint=_PATH_BY_STATE,
                    error_type=ErrorType.http_error,
                    error_message=str(exc),
                    request_payload=f"stateId={state_id}",
                )
            stats["failed"] += 1
            continue

        for item in detail_list:
            ext_id: int = item["commissionId"]
            comm_type = _api_type_to_enum(item.get("commissionTypeId", 2))

            # Resolve parent: district → state commission for this stateId
            parent_ext_id = next(
                (
                    c["commissionId"]
                    for c in raw_list
                    if c.get("stateId") == state_id
                    and _classify_top_level(c["commissionId"]) == CommissionType.state
                ),
                None,
            )
            parent_internal_id = ext_to_internal.get(parent_ext_id) if parent_ext_id else None

            data = {
                "commission_id_ext":             ext_id,
                "name_en":                       item["commissionNameEn"],
                "commission_type":               comm_type,
                "state_id":                      state_id,
                "district_id":                   item.get("districtId"),
                "case_prefix_text":              item.get("casePrefixText"),
                "circuit_addition_bench_status": item.get("circuitAdditionBenchStatus", 0),
                "parent_commission_id":          parent_internal_id,
            }

            if not dry_run:
                try:
                    with get_session() as session:
                        row_id = upsert_commission(session, data)
                        ext_to_internal[ext_id] = row_id
                        stats["upserted"] += 1
                except Exception as exc:
                    log.error("district_upsert_failed", ext_id=ext_id, error=str(exc))
                    stats["failed"] += 1
            else:
                log.debug("dry_run_skip_district", ext_id=ext_id, name=item["commissionNameEn"])
                stats["upserted"] += 1

    log.info("fetch_commissions_complete", **stats)
    return stats
