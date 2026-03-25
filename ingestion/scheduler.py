"""
APScheduler configuration and batch-run orchestration.

Scheduler modes:
  - Default (always-on): BackgroundScheduler with SQLAlchemyJobStore keeps
    jobs alive across container restarts.
  - RUN_ONCE=true: run_once_batch() executes the full pipeline sequentially
    and returns. Designed for Cloud Run Jobs / ECS Scheduled Tasks.

Daily job schedule (all UTC):
  00:00  fetch_commissions  — refresh all commission records
  01:00  fetch_cases        — scan all commissions for Samsung cases
  06:00  fetch_case_detail  — fill in detail for cases with last_fetched_at IS NULL
  12:00  fetch_orders       — download PDFs for ready hearings
  18:00  fetch_judgments    — queue judgment PDFs for closed cases

The scheduler job store uses Postgres so jobs survive container restarts
and don't double-fire on multi-instance deployments (APScheduler's
SQLAlchemyJobStore uses a table-level advisory lock per job).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import structlog
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from client import EJagritiClient
from db.session import get_session
from db.upsert import close_ingestion_run, create_ingestion_run
from db.models import TriggerMode
from jobs import fetch_case_detail, fetch_cases, fetch_commissions, fetch_judgments, fetch_orders

logger = structlog.get_logger(__name__)

_DATABASE_URL: str = os.environ["DATABASE_URL"]
_BASE_URL: str     = os.environ.get("EJAGRITI_BASE_URL", "https://e-jagriti.gov.in") + "/services"
_DAILY_BUDGET: int = int(os.environ.get("DAILY_CALL_BUDGET", "3500"))
_MAX_CONCURRENT: int = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "2"))
_MAX_RETRIES: int    = int(os.environ.get("MAX_RETRIES", "5"))


def _make_client() -> EJagritiClient:
    """
    Instantiate a fresh EJagritiClient from environment config.

    Returns:
        Configured EJagritiClient ready for use.
    """
    return EJagritiClient(
        base_url=_BASE_URL,
        max_concurrent=_MAX_CONCURRENT,
        max_retries=_MAX_RETRIES,
    )


def _run_job(job_fn, *, trigger_mode: TriggerMode, dry_run: bool) -> dict:
    """
    Wrap a job function with IngestionRun audit bookkeeping.

    Creates an IngestionRun row before execution and closes it afterwards
    with counts and duration. Catches all exceptions so one job failure
    does not abort other scheduled jobs.

    Args:
        job_fn: Callable(client, run_id, dry_run, daily_budget) -> dict.
        trigger_mode: How this run was triggered.
        dry_run: Forward DRY_RUN flag to the job.

    Returns:
        Stats dict returned by the job function, or empty dict on error.
    """
    log = logger.bind(job=job_fn.__name__, trigger_mode=trigger_mode.value, dry_run=dry_run)
    t_start = time.monotonic()
    run_id: int | None = None

    try:
        with get_session() as session:
            run_id = create_ingestion_run(session, trigger_mode=trigger_mode)
    except Exception as exc:
        log.error("run_create_failed", error=str(exc))
        run_id = None

    try:
        with _make_client() as client:
            stats = job_fn(
                client=client,
                run_id=run_id or 0,
                dry_run=dry_run,
                daily_budget=_DAILY_BUDGET,
            )
    except Exception as exc:
        log.error("job_failed_unexpectedly", error=str(exc))
        stats = {"total_calls": 0, "success_count": 0, "fail_count": 1, "skip_count": 0}
    finally:
        duration = time.monotonic() - t_start
        if run_id:
            try:
                with get_session() as session:
                    close_ingestion_run(
                        session,
                        run_id=run_id,
                        total_calls=stats.get("fetched", 0) + stats.get("upserted", 0),
                        success_count=stats.get("upserted", 0) + stats.get("stored", 0),
                        fail_count=stats.get("failed", 0),
                        skip_count=stats.get("skipped", 0),
                        duration_seconds=duration,
                    )
            except Exception as exc:
                log.error("run_close_failed", error=str(exc))

    log.info("job_done", duration_seconds=round(duration, 2), **stats)
    return stats


# ---------------------------------------------------------------------------
# Individual scheduler callbacks (called by APScheduler)
# ---------------------------------------------------------------------------

def _job_fetch_commissions() -> None:
    """APScheduler callback for fetch_commissions."""
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    _run_job(fetch_commissions.run, trigger_mode=TriggerMode.scheduler, dry_run=dry_run)


def _job_fetch_cases() -> None:
    """APScheduler callback for fetch_cases."""
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    _run_job(fetch_cases.run, trigger_mode=TriggerMode.scheduler, dry_run=dry_run)


def _job_fetch_case_detail() -> None:
    """APScheduler callback for fetch_case_detail."""
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    _run_job(fetch_case_detail.run, trigger_mode=TriggerMode.scheduler, dry_run=dry_run)


def _job_fetch_orders() -> None:
    """APScheduler callback for fetch_orders."""
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    _run_job(fetch_orders.run, trigger_mode=TriggerMode.scheduler, dry_run=dry_run)


def _job_fetch_judgments() -> None:
    """APScheduler callback for fetch_judgments."""
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    _run_job(fetch_judgments.run, trigger_mode=TriggerMode.scheduler, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------

def create_scheduler(dry_run: bool = False) -> BackgroundScheduler:
    """
    Build and configure an APScheduler BackgroundScheduler.

    Uses SQLAlchemyJobStore backed by Postgres so jobs survive restarts.
    All times are UTC cron triggers.

    Args:
        dry_run: Passed through env var; scheduler reads it dynamically.

    Returns:
        Configured (not yet started) BackgroundScheduler.
    """
    jobstores = {
        "default": SQLAlchemyJobStore(url=_DATABASE_URL, tablename="apscheduler_jobs"),
    }
    scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

    # Replace existing jobs so updated schedules take effect on restart
    scheduler.add_job(
        _job_fetch_commissions,
        trigger="cron", hour=0, minute=0,
        id="fetch_commissions", replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _job_fetch_cases,
        trigger="cron", hour=1, minute=0,
        id="fetch_cases", replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _job_fetch_case_detail,
        trigger="cron", hour=6, minute=0,
        id="fetch_case_detail", replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _job_fetch_orders,
        trigger="cron", hour=12, minute=0,
        id="fetch_orders", replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _job_fetch_judgments,
        trigger="cron", hour=18, minute=0,
        id="fetch_judgments", replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info("scheduler_configured", jobs=5, dry_run=dry_run)
    return scheduler


# ---------------------------------------------------------------------------
# RUN_ONCE batch (for Cloud Run / ECS Scheduled Tasks)
# ---------------------------------------------------------------------------

def run_once_batch(dry_run: bool = False) -> None:
    """
    Run the full ingestion pipeline sequentially and exit.

    Executes all jobs in dependency order:
      commissions → cases → case_detail → orders → judgments

    Designed for stateless scheduler invocations (Cloud Run Jobs,
    ECS Scheduled Tasks) where a persistent process is not desired.

    Args:
        dry_run: When True, fetch data but skip all DB writes.
    """
    log = logger.bind(mode="run_once", dry_run=dry_run)
    log.info("run_once_batch_start")

    steps = [
        fetch_commissions.run,
        fetch_cases.run,
        fetch_case_detail.run,
        fetch_orders.run,
        fetch_judgments.run,
    ]

    for step in steps:
        log.info("run_once_step_start", step=step.__module__)
        _run_job(step, trigger_mode=TriggerMode.run_once, dry_run=dry_run)

    log.info("run_once_batch_complete")
