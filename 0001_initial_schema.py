"""Initial schema — all tables, enums, indexes, constraints.

Revision ID: 0001
Revises:
Create Date: 2026-03-25 00:00:00.000000

Tables (in dependency order):
  1. commissions       — self-referencing hierarchy (national > state > district)
  2. cases             — core case records, FK -> commissions
  3. hearings          — per-hearing rows, FK -> cases
  4. daily_orders      — PDF fetch tracker, FK -> cases + hearings
  5. ingestion_runs    — batch audit log
  6. ingestion_errors  — per-error log, FK -> ingestion_runs
  7. failed_jobs       — retry queue
  8. api_call_log      — outbound HTTP call log, FK -> ingestion_runs

Enums:
  commission_type_enum, case_status_enum, job_type_enum,
  error_type_enum, trigger_mode_enum
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    """Create all tables, enums, indexes, and constraints."""

    bind = op.get_bind()

    # ------------------------------------------------------------------
    # ENUM TYPES — must exist before any table that references them
    # ------------------------------------------------------------------

    sa.Enum("national", "state", "district",
            name="commission_type_enum").create(bind, checkfirst=True)

    sa.Enum("open", "closed", "pending",
            name="case_status_enum").create(bind, checkfirst=True)

    sa.Enum("fetch_commissions", "fetch_cases", "fetch_case_detail", "fetch_daily_order",
            name="job_type_enum").create(bind, checkfirst=True)

    sa.Enum("HTTP_ERROR", "PARSE_ERROR", "DB_ERROR", "TIMEOUT", "RATE_LIMITED", "UNKNOWN",
            name="error_type_enum").create(bind, checkfirst=True)

    sa.Enum("scheduler", "run_once", "manual",
            name="trigger_mode_enum").create(bind, checkfirst=True)

    # ------------------------------------------------------------------
    # 1. commissions
    # ------------------------------------------------------------------
    op.create_table(
        "commissions",
        sa.Column("id",                            sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column("commission_id_ext",             sa.BigInteger(),  nullable=False),
        sa.Column("name_en",                       sa.String(255),   nullable=False),
        sa.Column("commission_type",               sa.Enum("national", "state", "district",
                                                           name="commission_type_enum"), nullable=False),
        sa.Column("state_id",                      sa.Integer(),     nullable=True),
        sa.Column("district_id",                   sa.Integer(),     nullable=True),
        sa.Column("case_prefix_text",              sa.String(50),    nullable=True),
        sa.Column("circuit_addition_bench_status", sa.Integer(),     nullable=False, server_default="0"),
        sa.Column("parent_commission_id",          sa.BigInteger(),  nullable=True),
        sa.Column("created_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id",                  name="pk_commissions"),
        sa.UniqueConstraint("commission_id_ext",        name="uq_commissions_ext_id"),
        sa.ForeignKeyConstraint(["parent_commission_id"], ["commissions.id"],
                                name="fk_commissions_parent", ondelete="SET NULL"),
    )
    op.create_index("idx_commissions_parent_id", "commissions", ["parent_commission_id"])
    op.create_index("idx_commissions_state_id",  "commissions", ["state_id"])
    op.create_index("idx_commissions_type",      "commissions", ["commission_type"])

    # ------------------------------------------------------------------
    # 2. cases
    # ------------------------------------------------------------------
    op.create_table(
        "cases",
        sa.Column("id",                        sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column("case_number",               sa.String(100),   nullable=False),
        sa.Column("file_application_number",   sa.String(100),   nullable=True),
        sa.Column("filing_reference_number",   sa.BigInteger(),  nullable=True),
        sa.Column("commission_id",             sa.BigInteger(),  nullable=False),
        sa.Column("case_type_name",            sa.String(100),   nullable=True),
        sa.Column("case_type_id",              sa.Integer(),     nullable=True),
        sa.Column("case_stage_name",           sa.String(255),   nullable=True),
        sa.Column("case_stage_id",             sa.Integer(),     nullable=True),
        sa.Column("case_category_name",        sa.String(255),   nullable=True),
        sa.Column("filing_date",               sa.Date(),        nullable=True),
        sa.Column("date_of_cause",             sa.Date(),        nullable=True),
        sa.Column("date_of_next_hearing",      sa.Date(),        nullable=True),
        sa.Column("complainant_name",          sa.String(500),   nullable=True),
        sa.Column("respondent_name",           sa.String(500),   nullable=True),
        sa.Column("complainant_advocate_names",sa.Text(),        nullable=True),
        sa.Column("respondent_advocate_names", sa.Text(),        nullable=True),
        sa.Column("status", sa.Enum("open", "closed", "pending", name="case_status_enum"),
                  nullable=False, server_default="pending"),
        sa.Column("data_hash",         sa.String(32),              nullable=True),
        sa.Column("last_fetched_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id",                   name="pk_cases"),
        sa.UniqueConstraint("case_number",               name="uq_cases_case_number"),
        sa.UniqueConstraint("filing_reference_number",   name="uq_cases_filing_ref"),
        sa.ForeignKeyConstraint(["commission_id"], ["commissions.id"],
                                name="fk_cases_commission", ondelete="RESTRICT"),
    )
    op.create_index("idx_cases_commission_id",        "cases", ["commission_id"])
    op.create_index("idx_cases_status",               "cases", ["status"])
    op.create_index("idx_cases_filing_date",          "cases", ["filing_date"])
    op.create_index("idx_cases_stage_name",           "cases", ["case_stage_name"])
    op.create_index("idx_cases_date_of_next_hearing", "cases", ["date_of_next_hearing"])
    op.create_index("idx_cases_needs_detail_fetch",   "cases", ["last_fetched_at"],
                    postgresql_where=sa.text("last_fetched_at IS NULL"))

    # ------------------------------------------------------------------
    # 3. hearings
    # ------------------------------------------------------------------
    op.create_table(
        "hearings",
        sa.Column("id",                              sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("case_id",                         sa.BigInteger(), nullable=False),
        sa.Column("court_room_hearing_id",           sa.String(50),   nullable=False),
        sa.Column("date_of_hearing",                 sa.Date(),       nullable=True),
        sa.Column("date_of_next_hearing",            sa.Date(),       nullable=True),
        sa.Column("case_stage",                      sa.String(255),  nullable=True),
        sa.Column("proceeding_text",                 sa.Text(),       nullable=True),
        sa.Column("daily_order_status",              sa.Boolean(),    nullable=True),
        sa.Column("order_type_id",                   sa.Integer(),    nullable=True),
        sa.Column("daily_order_availability_status", sa.Integer(),    nullable=True),
        sa.Column("hearing_sequence_number",         sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("created_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_hearings"),
        sa.UniqueConstraint("case_id", "court_room_hearing_id", name="uq_hearing_case_courtroom"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"],
                                name="fk_hearings_case", ondelete="CASCADE"),
    )
    op.create_index("idx_hearings_case_id",         "hearings", ["case_id"])
    op.create_index("idx_hearings_date_of_hearing", "hearings", ["date_of_hearing"])
    op.create_index("idx_hearings_pdf_pending",     "hearings", ["case_id"],
                    postgresql_where=sa.text("daily_order_availability_status = 2"))

    # ------------------------------------------------------------------
    # 4. daily_orders
    # ------------------------------------------------------------------
    op.create_table(
        "daily_orders",
        sa.Column("id",                      sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column("case_id",                 sa.BigInteger(),  nullable=False),
        sa.Column("hearing_id",              sa.BigInteger(),  nullable=True),
        sa.Column("filing_reference_number", sa.BigInteger(),  nullable=False),
        sa.Column("date_of_hearing",         sa.Date(),        nullable=False),
        sa.Column("order_type_id",           sa.Integer(),     nullable=False, server_default="1"),
        sa.Column("pdf_storage_path",        sa.String(1024),  nullable=True),
        sa.Column("pdf_fetched",             sa.Boolean(),     nullable=False, server_default=sa.text("false")),
        sa.Column("pdf_fetched_at",          sa.DateTime(timezone=True), nullable=True),
        sa.Column("pdf_fetch_error",         sa.Text(),        nullable=True),
        sa.Column("created_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at",  sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_daily_orders"),
        sa.UniqueConstraint("filing_reference_number", "date_of_hearing", "order_type_id",
                            name="uq_daily_order_pdf_key"),
        sa.ForeignKeyConstraint(["case_id"],    ["cases.id"],    name="fk_daily_orders_case",    ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["hearing_id"], ["hearings.id"], name="fk_daily_orders_hearing", ondelete="SET NULL"),
    )
    op.create_index("idx_daily_orders_case_id",    "daily_orders", ["case_id"])
    op.create_index("idx_daily_orders_hearing_id", "daily_orders", ["hearing_id"])
    op.create_index("idx_daily_orders_unfetched",  "daily_orders", ["id"],
                    postgresql_where=sa.text("pdf_fetched = false"))

    # ------------------------------------------------------------------
    # 5. ingestion_runs
    # ------------------------------------------------------------------
    op.create_table(
        "ingestion_runs",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_started_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_finished_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_calls",      sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count",    sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fail_count",       sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skip_count",       sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(),   nullable=True),
        sa.Column("trigger_mode",     sa.Enum("scheduler", "run_once", "manual",
                                              name="trigger_mode_enum"),
                  nullable=False, server_default="scheduler"),
        sa.Column("notes",            sa.Text(),    nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_runs"),
    )
    op.create_index("idx_ingestion_runs_started_at", "ingestion_runs", ["run_started_at"])

    # ------------------------------------------------------------------
    # 6. ingestion_errors
    # ------------------------------------------------------------------
    op.create_table(
        "ingestion_errors",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id",           sa.BigInteger(), nullable=True),
        sa.Column("case_id",          sa.BigInteger(), nullable=True),
        sa.Column("endpoint",         sa.String(512),  nullable=False),
        sa.Column("http_status",      sa.Integer(),    nullable=True),
        sa.Column("error_type",       sa.Enum("HTTP_ERROR", "PARSE_ERROR", "DB_ERROR",
                                              "TIMEOUT", "RATE_LIMITED", "UNKNOWN",
                                              name="error_type_enum"),
                  nullable=False, server_default="UNKNOWN"),
        sa.Column("error_message",    sa.Text(),       nullable=False),
        sa.Column("request_payload",  sa.Text(),       nullable=True),
        sa.Column("response_body",    sa.Text(),       nullable=True),
        sa.Column("retry_count",      sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_errors"),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.id"],
                                name="fk_ingestion_errors_run", ondelete="SET NULL"),
    )
    op.create_index("idx_ingestion_errors_run_id",     "ingestion_errors", ["run_id"])
    op.create_index("idx_ingestion_errors_created_at", "ingestion_errors", ["created_at"])

    # ------------------------------------------------------------------
    # 7. failed_jobs
    # ------------------------------------------------------------------
    op.create_table(
        "failed_jobs",
        sa.Column("id",                 sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_type",           sa.Enum("fetch_commissions", "fetch_cases",
                                                "fetch_case_detail", "fetch_daily_order",
                                                name="job_type_enum"), nullable=False),
        sa.Column("case_id",            sa.BigInteger(), nullable=True),
        sa.Column("commission_id",      sa.BigInteger(), nullable=True),
        sa.Column("endpoint",           sa.String(512),  nullable=False),
        sa.Column("params",             sa.Text(),       nullable=True),
        sa.Column("retry_count",        sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("last_attempted_at",  sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_retry_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason",             sa.Text(),       nullable=False),
        sa.Column("resolved",           sa.Boolean(),    nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_failed_jobs"),
    )
    op.create_index("idx_failed_jobs_sweep",   "failed_jobs", ["next_retry_at", "resolved"],
                    postgresql_where=sa.text("resolved = false"))
    op.create_index("idx_failed_jobs_case_id", "failed_jobs", ["case_id"])

    # ------------------------------------------------------------------
    # 8. api_call_log
    # ------------------------------------------------------------------
    op.create_table(
        "api_call_log",
        sa.Column("id",            sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id",        sa.BigInteger(), nullable=True),
        sa.Column("endpoint",      sa.String(512),  nullable=False),
        sa.Column("method",        sa.String(10),   nullable=False, server_default="GET"),
        sa.Column("response_code", sa.Integer(),    nullable=True),
        sa.Column("duration_ms",   sa.Integer(),    nullable=True),
        sa.Column("retry_count",   sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("user_agent",    sa.String(512),  nullable=True),
        sa.Column("called_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_api_call_log"),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.id"],
                                name="fk_api_call_log_run", ondelete="SET NULL"),
    )
    op.create_index("idx_api_call_log_run_id",        "api_call_log", ["run_id"])
    op.create_index("idx_api_call_log_called_at",     "api_call_log", ["called_at"])
    op.create_index("idx_api_call_log_response_code", "api_call_log", ["response_code"])

    # ------------------------------------------------------------------
    # updated_at trigger — auto-set on every UPDATE for mutable tables
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    for tbl in ("commissions", "cases", "hearings", "daily_orders"):
        op.execute(f"""
            CREATE TRIGGER trg_{tbl}_updated_at
            BEFORE UPDATE ON {tbl}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    """Drop all tables, triggers, and enum types in reverse dependency order."""

    for tbl in ("commissions", "cases", "hearings", "daily_orders"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{tbl}_updated_at ON {tbl};")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")

    for tbl in ("api_call_log", "failed_jobs", "ingestion_errors", "ingestion_runs",
                "daily_orders", "hearings", "cases", "commissions"):
        op.drop_table(tbl)

    for enum_name in ("trigger_mode_enum", "error_type_enum", "job_type_enum",
                      "case_status_enum", "commission_type_enum"):
        op.execute(f"DROP TYPE IF EXISTS {enum_name};")
