"""
Ingestion service entry point.

Reads RUN_ONCE and DRY_RUN environment variables to select the operating mode:

  EJAGRITI_RUN_ONCE=false (default):
    Starts an APScheduler BackgroundScheduler that runs jobs on a daily cron
    schedule.  The process runs indefinitely until SIGTERM or SIGINT.

  EJAGRITI_RUN_ONCE=true:
    Executes the full ingestion pipeline once (commissions → cases →
    case_detail → orders → judgments) and exits with code 0 on success or
    code 1 on unhandled error.  Suitable for Cloud Run Jobs / ECS Scheduled
    Tasks.

  EJAGRITI_DRY_RUN=true:
    Can be combined with either mode. Fetches data from the eJagriti API but
    skips all database writes. Useful for smoke-testing connectivity without
    side effects.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import structlog
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: load .env before any other import reads env vars
# ---------------------------------------------------------------------------
load_dotenv()

from db.session import check_db_connection  # noqa: E402 — must come after load_dotenv
from scheduler import create_scheduler, run_once_batch  # noqa: E402


def _configure_logging() -> None:
    """
    Configure structlog for structured JSON output to stdout and a daily
    rotating log file.

    Log files are written to EJAGRITI_LOG_DIR (default: ./logs) and rotated
    at midnight, keeping 30 days of history. Each file is named
    ingestion.log (current) or ingestion.log.YYYY-MM-DD (rotated).
    """
    import logging
    from logging.handlers import TimedRotatingFileHandler
    from pathlib import Path

    log_level_name = os.environ.get("EJAGRITI_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    log_dir = Path(os.environ.get("EJAGRITI_LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(message)s")

    # stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    # daily rotating file handler — rotates at midnight, keeps 30 days
    file_handler = TimedRotatingFileHandler(
        filename=log_dir / "ingestion.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(file_handler)

    # Suppress low-level transport noise from httpx/httpcore regardless of log level
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def main() -> None:
    """
    Entry point: configure logging, verify DB connectivity, then dispatch
    to either run_once or always-on scheduler mode.
    """
    _configure_logging()
    log = structlog.get_logger(__name__)

    run_once = os.environ.get("EJAGRITI_RUN_ONCE", "false").lower() == "true"
    dry_run  = os.environ.get("EJAGRITI_DRY_RUN",  "false").lower() == "true"

    log.info(
        "ingestion_service_starting",
        run_once=run_once,
        dry_run=dry_run,
        search_keyword=os.environ.get("EJAGRITI_SEARCH_KEYWORD", "samsung"),
        daily_budget=os.environ.get("EJAGRITI_DAILY_CALL_BUDGET", "3500"),
    )

    # Verify DB connectivity before doing any work
    if not check_db_connection():
        log.error("db_connection_failed_aborting")
        sys.exit(1)

    log.info("db_connection_ok")

    # ------------------------------------------------------------------
    # RUN_ONCE mode — run full batch once and exit (Cloud Run / ECS)
    # ------------------------------------------------------------------
    if run_once:
        try:
            run_once_batch(dry_run=dry_run)
            log.info("run_once_complete_exiting")
            sys.exit(0)
        except Exception as exc:
            log.error("run_once_fatal_error", error=str(exc))
            sys.exit(1)

    # ------------------------------------------------------------------
    # Always-on scheduler mode
    # ------------------------------------------------------------------
    scheduler = create_scheduler(dry_run=dry_run)

    def _shutdown(signum: int, _frame: object) -> None:
        """Handle SIGTERM / SIGINT gracefully by stopping the scheduler."""
        log.info("shutdown_signal_received", signal=signum)
        scheduler.shutdown(wait=True)
        log.info("scheduler_stopped")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    scheduler.start()
    log.info("scheduler_started_waiting_for_jobs")

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=True)
        log.info("ingestion_service_stopped")


if __name__ == "__main__":
    main()
