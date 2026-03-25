"""
ORM models for the eJagriti Samsung case tracker.

This file is the single source of truth for the database schema.
Both the ingestion service and the API service import from here.
Alembic's env.py also imports Base from here for autogenerate support.

Design notes:
- All tables use BigInteger surrogate PKs (auto-increment) as internal IDs.
- External IDs from the e-jagriti API are stored in separate _ext columns
  so internal FKs never depend on third-party ID stability.
- Enums are defined as Python enum + SQLAlchemy Enum type so Postgres
  enforces the constraint at the DB level.
- TOAST compression handles large text columns (proceeding_text, etc.)
  automatically — no manual intervention needed.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, Float,
    ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CommissionType(str, enum.Enum):
    """Mirrors commissionTypeId values from the e-jagriti API."""
    national = "national"   # commissionTypeId = 1  (NCDRC)
    state    = "state"      # commissionTypeId = 2
    district = "district"   # commissionTypeId = 3


class CaseStatus(str, enum.Enum):
    """Derived status — not a direct API field, set by ingestion logic."""
    open    = "open"
    closed  = "closed"
    pending = "pending"


class JobType(str, enum.Enum):
    fetch_commissions  = "fetch_commissions"
    fetch_cases        = "fetch_cases"
    fetch_case_detail  = "fetch_case_detail"
    fetch_daily_order  = "fetch_daily_order"


class ErrorType(str, enum.Enum):
    http_error    = "HTTP_ERROR"
    parse_error   = "PARSE_ERROR"
    db_error      = "DB_ERROR"
    timeout       = "TIMEOUT"
    rate_limited  = "RATE_LIMITED"
    unknown       = "UNKNOWN"


class TriggerMode(str, enum.Enum):
    scheduler = "scheduler"
    run_once  = "run_once"
    manual    = "manual"


# ---------------------------------------------------------------------------
# commissions
# ---------------------------------------------------------------------------

class Commission(Base):
    """
    Stores national, state, and district commissions from e-jagriti.

    Populated by two API calls:
      GET /master/master/v2/getAllCommission
      GET /master/master/v2/getCommissionDetailsByStateId

    Self-referencing parent_commission_id captures state -> district hierarchy.
    """
    __tablename__ = "commissions"

    id: Mapped[int]                           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    commission_id_ext: Mapped[int]            = mapped_column(BigInteger, nullable=False, unique=True)
    name_en: Mapped[str]                      = mapped_column(String(255), nullable=False)
    commission_type: Mapped[CommissionType]   = mapped_column(Enum(CommissionType, name="commission_type_enum"), nullable=False)
    state_id: Mapped[Optional[int]]           = mapped_column(Integer, nullable=True)
    district_id: Mapped[Optional[int]]        = mapped_column(Integer, nullable=True)
    case_prefix_text: Mapped[Optional[str]]   = mapped_column(String(50), nullable=True)
    circuit_addition_bench_status: Mapped[int]= mapped_column(Integer, nullable=False, default=0)
    parent_commission_id: Mapped[Optional[int]]= mapped_column(
        BigInteger, ForeignKey("commissions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    parent:   Mapped[Optional["Commission"]]  = relationship("Commission", remote_side="Commission.id", back_populates="children")
    children: Mapped[list["Commission"]]      = relationship("Commission", back_populates="parent")
    cases:    Mapped[list["Case"]]            = relationship("Case", back_populates="commission")

    __table_args__ = (
        Index("idx_commissions_parent_id", "parent_commission_id"),
        Index("idx_commissions_state_id",  "state_id"),
        Index("idx_commissions_type",      "commission_type"),
    )

    def __repr__(self) -> str:
        return f"<Commission id={self.id} ext={self.commission_id_ext} name={self.name_en!r}>"


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

class Case(Base):
    """
    Core case record. Populated from two API sources:
      1. getCauseTitleListByCompany  — lightweight list fields
      2. getCaseStatus               — full detail including hearings

    data_hash (MD5 of full getCaseStatus JSON) + last_fetched_at are the
    change-detection mechanism. Child records only updated if hash changed.
    """
    __tablename__ = "cases"

    id: Mapped[int]                              = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_number: Mapped[str]                     = mapped_column(String(100), nullable=False, unique=True)
    file_application_number: Mapped[Optional[str]]= mapped_column(String(100), nullable=True)
    # Critical: used by the daily-order PDF endpoint as filingReferenceNumber
    filing_reference_number: Mapped[Optional[int]]= mapped_column(BigInteger, nullable=True, unique=True)
    commission_id: Mapped[int]                   = mapped_column(BigInteger, ForeignKey("commissions.id", ondelete="RESTRICT"), nullable=False)
    case_type_name: Mapped[Optional[str]]        = mapped_column(String(100), nullable=True)
    case_type_id: Mapped[Optional[int]]          = mapped_column(Integer, nullable=True)
    # Free text — API has many undocumented stage values
    case_stage_name: Mapped[Optional[str]]       = mapped_column(String(255), nullable=True)
    case_stage_id: Mapped[Optional[int]]         = mapped_column(Integer, nullable=True)
    case_category_name: Mapped[Optional[str]]    = mapped_column(String(255), nullable=True)
    filing_date: Mapped[Optional[date]]          = mapped_column(Date, nullable=True)
    date_of_cause: Mapped[Optional[date]]        = mapped_column(Date, nullable=True)
    date_of_next_hearing: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    complainant_name: Mapped[Optional[str]]      = mapped_column(String(500), nullable=True)
    respondent_name: Mapped[Optional[str]]       = mapped_column(String(500), nullable=True)
    # JSON text arrays — advocates are informational, never queried cross-case
    complainant_advocate_names: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    respondent_advocate_names: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    status: Mapped[CaseStatus]                   = mapped_column(
        Enum(CaseStatus, name="case_status_enum"),
        nullable=False, default=CaseStatus.pending, server_default=CaseStatus.pending.value
    )
    data_hash: Mapped[Optional[str]]             = mapped_column(String(32), nullable=True)
    last_fetched_at: Mapped[Optional[datetime]]  = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]                 = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime]                 = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    commission:   Mapped["Commission"]       = relationship("Commission", back_populates="cases")
    hearings:     Mapped[list["Hearing"]]    = relationship("Hearing", back_populates="case", cascade="all, delete-orphan")
    daily_orders: Mapped[list["DailyOrder"]] = relationship("DailyOrder", back_populates="case", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_cases_commission_id",        "commission_id"),
        Index("idx_cases_status",               "status"),
        Index("idx_cases_filing_date",          "filing_date"),
        Index("idx_cases_stage_name",           "case_stage_name"),
        Index("idx_cases_date_of_next_hearing", "date_of_next_hearing"),
        Index("idx_cases_needs_detail_fetch",   "last_fetched_at",
              postgresql_where="last_fetched_at IS NULL"),
    )

    def __repr__(self) -> str:
        return f"<Case id={self.id} case_number={self.case_number!r} status={self.status}>"


# ---------------------------------------------------------------------------
# hearings
# ---------------------------------------------------------------------------

class Hearing(Base):
    """
    One row per entry in the caseHearingDetails array from getCaseStatus.

    court_room_hearing_id is the external unique key for upserts.

    proceeding_text is an HTML blob (Word-generated markup). Stored as TEXT;
    Postgres TOAST compresses it transparently. Never included in list queries.

    daily_order_availability_status:
      null = not applicable (sequence 0 / future hearing)
      1    = order not yet available
      2    = order available — triggers a DailyOrder PDF fetch job
    """
    __tablename__ = "hearings"

    id: Mapped[int]                                  = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[int]                             = mapped_column(BigInteger, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    court_room_hearing_id: Mapped[str]               = mapped_column(String(50), nullable=False)
    date_of_hearing: Mapped[Optional[date]]          = mapped_column(Date, nullable=True)
    date_of_next_hearing: Mapped[Optional[date]]     = mapped_column(Date, nullable=True)
    case_stage: Mapped[Optional[str]]                = mapped_column(String(255), nullable=True)
    # Raw HTML — excluded from list queries, loaded only on case detail endpoint
    proceeding_text: Mapped[Optional[str]]           = mapped_column(Text, nullable=True)
    daily_order_status: Mapped[Optional[bool]]       = mapped_column(Boolean, nullable=True)
    order_type_id: Mapped[Optional[int]]             = mapped_column(Integer, nullable=True)
    # 1 = not available, 2 = available (fetch PDF), null = n/a
    daily_order_availability_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hearing_sequence_number: Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime]                     = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime]                     = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    case:        Mapped["Case"]                  = relationship("Case", back_populates="hearings")
    daily_order: Mapped[Optional["DailyOrder"]]  = relationship("DailyOrder", back_populates="hearing", uselist=False)

    __table_args__ = (
        UniqueConstraint("case_id", "court_room_hearing_id", name="uq_hearing_case_courtroom"),
        Index("idx_hearings_case_id",         "case_id"),
        Index("idx_hearings_date_of_hearing", "date_of_hearing"),
        Index("idx_hearings_pdf_pending",     "case_id",
              postgresql_where="daily_order_availability_status = 2"),
    )

    def __repr__(self) -> str:
        return f"<Hearing id={self.id} case_id={self.case_id} seq={self.hearing_sequence_number} date={self.date_of_hearing}>"


# ---------------------------------------------------------------------------
# daily_orders
# ---------------------------------------------------------------------------

class DailyOrder(Base):
    """
    Tracks daily order PDFs for each hearing.

    PDF fetched via:
      GET /courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf
        ?filingReferenceNumber=X&dateOfHearing=YYYY-MM-DD&orderTypeId=N

    Base64 PDF is decoded and uploaded to S3. Only pdf_storage_path lives here.
    A row may exist with pdf_fetched=False while the PDF job is queued.
    """
    __tablename__ = "daily_orders"

    id: Mapped[int]                          = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[int]                     = mapped_column(BigInteger, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    hearing_id: Mapped[Optional[int]]        = mapped_column(BigInteger, ForeignKey("hearings.id", ondelete="SET NULL"), nullable=True)
    # These three fields are the exact PDF API parameters
    filing_reference_number: Mapped[int]     = mapped_column(BigInteger, nullable=False)
    date_of_hearing: Mapped[date]            = mapped_column(Date, nullable=False)
    order_type_id: Mapped[int]               = mapped_column(Integer, nullable=False, default=1)
    # S3/disk path after base64 decode + upload (e.g. s3://bucket/orders/...)
    pdf_storage_path: Mapped[Optional[str]]  = mapped_column(String(1024), nullable=True)
    pdf_fetched: Mapped[bool]                = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    pdf_fetched_at: Mapped[Optional[datetime]]= mapped_column(DateTime(timezone=True), nullable=True)
    pdf_fetch_error: Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]             = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime]             = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    case:    Mapped["Case"]                 = relationship("Case", back_populates="daily_orders")
    hearing: Mapped[Optional["Hearing"]]    = relationship("Hearing", back_populates="daily_order")

    __table_args__ = (
        UniqueConstraint("filing_reference_number", "date_of_hearing", "order_type_id",
                         name="uq_daily_order_pdf_key"),
        Index("idx_daily_orders_case_id",    "case_id"),
        Index("idx_daily_orders_hearing_id", "hearing_id"),
        Index("idx_daily_orders_unfetched",  "id",
              postgresql_where="pdf_fetched = false"),
    )

    def __repr__(self) -> str:
        return f"<DailyOrder id={self.id} case_id={self.case_id} date={self.date_of_hearing} fetched={self.pdf_fetched}>"


# ---------------------------------------------------------------------------
# ingestion_runs
# ---------------------------------------------------------------------------

class IngestionRun(Base):
    """
    Audit record written at the end of every ingestion batch.
    One row per run. Used by /health endpoint to surface last run summary.
    """
    __tablename__ = "ingestion_runs"

    id: Mapped[int]                          = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_started_at: Mapped[datetime]         = mapped_column(DateTime(timezone=True), nullable=False)
    run_finished_at: Mapped[Optional[datetime]]= mapped_column(DateTime(timezone=True), nullable=True)
    total_calls: Mapped[int]                 = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int]               = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int]                  = mapped_column(Integer, nullable=False, default=0)
    skip_count: Mapped[int]                  = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[Optional[float]]= mapped_column(Float, nullable=True)
    trigger_mode: Mapped[TriggerMode]        = mapped_column(
        Enum(TriggerMode, name="trigger_mode_enum"),
        nullable=False, default=TriggerMode.scheduler
    )
    notes: Mapped[Optional[str]]             = mapped_column(Text, nullable=True)

    errors:    Mapped[list["IngestionError"]] = relationship("IngestionError", back_populates="run", cascade="all, delete-orphan")
    api_calls: Mapped[list["ApiCallLog"]]     = relationship("ApiCallLog", back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_ingestion_runs_started_at", "run_started_at"),
    )

    def __repr__(self) -> str:
        return f"<IngestionRun id={self.id} started={self.run_started_at} calls={self.total_calls} fails={self.fail_count}>"


# ---------------------------------------------------------------------------
# ingestion_errors
# ---------------------------------------------------------------------------

class IngestionError(Base):
    """
    Detailed error log per failed API call or DB write during an ingestion run.
    Historical log — not a retry queue (see FailedJob for that).
    """
    __tablename__ = "ingestion_errors"

    id: Mapped[int]                          = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[Optional[int]]            = mapped_column(BigInteger, ForeignKey("ingestion_runs.id", ondelete="SET NULL"), nullable=True)
    case_id: Mapped[Optional[int]]           = mapped_column(BigInteger, nullable=True)
    endpoint: Mapped[str]                    = mapped_column(String(512), nullable=False)
    http_status: Mapped[Optional[int]]       = mapped_column(Integer, nullable=True)
    error_type: Mapped[ErrorType]            = mapped_column(Enum(ErrorType, name="error_type_enum"), nullable=False, default=ErrorType.unknown)
    error_message: Mapped[str]               = mapped_column(Text, nullable=False)
    request_payload: Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    # Truncated response body (first 4 KB) for debugging
    response_body: Mapped[Optional[str]]     = mapped_column(Text, nullable=True)
    retry_count: Mapped[int]                 = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime]             = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped[Optional["IngestionRun"]] = relationship("IngestionRun", back_populates="errors")

    __table_args__ = (
        Index("idx_ingestion_errors_run_id",    "run_id"),
        Index("idx_ingestion_errors_created_at","created_at"),
    )

    def __repr__(self) -> str:
        return f"<IngestionError id={self.id} run_id={self.run_id} type={self.error_type} endpoint={self.endpoint!r}>"


# ---------------------------------------------------------------------------
# failed_jobs
# ---------------------------------------------------------------------------

class FailedJob(Base):
    """
    Retry queue for jobs that failed after exhausting retries.

    Unlike IngestionError (historical log), this table represents work still
    to be done. The ingestion service sweeps rows where:
      resolved = false AND next_retry_at <= now()

    resolved=True means either the retry succeeded, or was manually cleared.
    """
    __tablename__ = "failed_jobs"

    id: Mapped[int]                          = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_type: Mapped[JobType]                = mapped_column(Enum(JobType, name="job_type_enum"), nullable=False)
    case_id: Mapped[Optional[int]]           = mapped_column(BigInteger, nullable=True)
    commission_id: Mapped[Optional[int]]     = mapped_column(BigInteger, nullable=True)
    endpoint: Mapped[str]                    = mapped_column(String(512), nullable=False)
    # JSON-encoded query params for exact replay
    params: Mapped[Optional[str]]            = mapped_column(Text, nullable=True)
    retry_count: Mapped[int]                 = mapped_column(Integer, nullable=False, default=0)
    last_attempted_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), nullable=False)
    next_retry_at: Mapped[Optional[datetime]]= mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str]                      = mapped_column(Text, nullable=False)
    resolved: Mapped[bool]                   = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime]             = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_failed_jobs_sweep",   "next_retry_at", "resolved",
              postgresql_where="resolved = false"),
        Index("idx_failed_jobs_case_id", "case_id"),
    )

    def __repr__(self) -> str:
        return f"<FailedJob id={self.id} job_type={self.job_type} case_id={self.case_id} retries={self.retry_count} resolved={self.resolved}>"


# ---------------------------------------------------------------------------
# api_call_log
# ---------------------------------------------------------------------------

class ApiCallLog(Base):
    """
    Low-level log of every outbound HTTP call made by the ingestion service.

    Used for rate limit auditing, 429 debugging, UA rotation tracking,
    and performance profiling.

    High write volume — consider monthly time-based partitioning or a
    TTL purge job (keep last 90 days) in production.
    """
    __tablename__ = "api_call_log"

    id: Mapped[int]                     = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[Optional[int]]       = mapped_column(BigInteger, ForeignKey("ingestion_runs.id", ondelete="SET NULL"), nullable=True)
    endpoint: Mapped[str]               = mapped_column(String(512), nullable=False)
    method: Mapped[str]                 = mapped_column(String(10), nullable=False, default="GET")
    response_code: Mapped[Optional[int]]= mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    user_agent: Mapped[Optional[str]]   = mapped_column(String(512), nullable=True)
    called_at: Mapped[datetime]         = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped[Optional["IngestionRun"]] = relationship("IngestionRun", back_populates="api_calls")

    __table_args__ = (
        Index("idx_api_call_log_run_id",       "run_id"),
        Index("idx_api_call_log_called_at",    "called_at"),
        Index("idx_api_call_log_response_code","response_code"),
    )

    def __repr__(self) -> str:
        return f"<ApiCallLog id={self.id} endpoint={self.endpoint!r} code={self.response_code} duration={self.duration_ms}ms>"
