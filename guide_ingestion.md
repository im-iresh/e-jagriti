# eJagriti Ingestion Service — Developer Guide

This guide covers everything you need to understand, run, and extend the ingestion service. It assumes you are a developer but have no prior knowledge of this project.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Repository Layout](#2-repository-layout)
3. [Database Schema](#3-database-schema)
4. [HTTP Client](#4-http-client)
5. [Operating Modes](#5-operating-modes)
6. [Scheduling](#6-scheduling)
7. [Environment Variables](#7-environment-variables)
8. [Logging](#8-logging)
9. [Jobs Reference](#9-jobs-reference) *(added in Part 2 & 3)*

---

## 1. Overview

**What is this service?**

The ingestion service is a Python background worker that scrapes case data from the [e-Jagriti consumer court portal](https://e-jagriti.gov.in) — India's national online platform for consumer dispute cases — and stores it in a PostgreSQL database.

**Why does it exist?**

The e-Jagriti portal has no public API or bulk data export. This service reverse-engineers its internal REST endpoints to systematically collect all cases where Samsung is a respondent, across every consumer commission in India (national, state, and district levels).

**What data does it collect?**

| Data | Source API | Where stored |
|------|-----------|--------------|
| Commission registry (all ~700+ commissions) | `getAllCommission`, `getCommissionDetailsByStateId` | `commissions` table |
| Case list (Samsung cases per commission) | `getCauseTitleListByCompany` | `cases` table |
| Full case detail (parties, advocates, stage, hearings) | `getCaseStatus` | `cases` + `hearings` tables |
| Daily order PDFs (hearing orders) | `getDailyOrderJudgementPdf` | `daily_orders` table + filesystem/S3 |
| Final judgment PDFs (closed cases) | same PDF endpoint with `orderTypeId=2` | `daily_orders` table |

**Technology stack:**

- Python 3.11+, `httpx` for HTTP, `SQLAlchemy 2.x` ORM, `Alembic` for migrations
- `APScheduler` for cron scheduling, `structlog` for structured JSON logs
- PostgreSQL 15, optionally Redis (not yet used by ingestion)

---

## 2. Repository Layout

```
e-jagriti/
├── ingestion/                  # ← This service lives here
│   ├── main.py                 # Entry point: configures logging, checks DB, starts scheduler or run_once
│   ├── scheduler.py            # APScheduler setup + run_once_batch() orchestrator
│   ├── client.py               # EJagritiClient: HTTP client with retries, rate limiting, UA rotation
│   ├── .env                    # Local environment variables (not committed)
│   ├── requirements.txt        # Python dependencies
│   │
│   ├── jobs/                   # One module per ingestion job
│   │   ├── __init__.py         # Re-exports all job modules
│   │   ├── fetch_commissions.py
│   │   ├── fetch_cases.py
│   │   ├── fetch_case_detail.py
│   │   ├── fetch_orders.py
│   │   └── fetch_judgments.py
│   │
│   ├── db/
│   │   ├── models.py           # SQLAlchemy ORM models — single source of truth for schema
│   │   ├── session.py          # Engine + Session factory, check_db_connection()
│   │   └── upsert.py           # Idempotent upsert helpers for every table
│   │
│   └── logs/                   # Created at runtime; daily-rotating log files written here
│
├── migrations/                 # Alembic migration scripts
│   ├── env.py                  # Alembic runtime config (loads .env, sets sys.path)
│   ├── script.py.mako          # Template for new migration files
│   └── versions/
│       └── 0001_initial_schema.py  # Creates all 8 tables + enums + indexes
│
├── alembic.ini                 # Alembic config (script_location, placeholder DB URL)
│                               # Located at ingestion/alembic.ini — run alembic from ingestion/
├── docker-compose.yml          # Local dev: postgres + ingestion + migrations services
├── .env.example                # Template for .env files
└── guide_ingestion.md          # This file
```

**Key relationships between files:**

```
main.py
  └─ loads .env (python-dotenv)
  └─ calls check_db_connection() from db/session.py
  └─ calls create_scheduler() or run_once_batch() from scheduler.py

scheduler.py
  └─ imports all 5 job modules from jobs/
  └─ wraps each with _run_job() which creates/closes IngestionRun audit rows
  └─ each job imports EJagritiClient from client.py
  └─ each job imports get_session() from db/session.py
  └─ each job uses upsert helpers from db/upsert.py
```

---

## 3. Database Schema

All tables use `BigInteger` auto-increment surrogate PKs. External IDs from the API are stored in separate `_ext` columns so internal foreign keys never depend on third-party ID stability.

Run migrations from the `ingestion/` directory:

```bash
cd ingestion
alembic -c alembic.ini upgrade head
```

---

### 3.1 `commissions`

Stores every consumer commission in India — one NCDRC (national), ~35 state commissions, and ~700+ district commissions.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | Internal surrogate key |
| `commission_id_ext` | `bigint UNIQUE NOT NULL` | External ID from e-Jagriti API |
| `name_en` | `varchar(255)` | Commission name in English |
| `commission_type` | `enum` | `national`, `state`, or `district` |
| `state_id` | `integer` | API's state identifier |
| `district_id` | `integer` | API's district identifier (district commissions only) |
| `case_prefix_text` | `varchar(50)` | Case number prefix (e.g. `DC/77/`) |
| `circuit_addition_bench_status` | `integer` | From API; default 0 |
| `parent_commission_id` | `bigint FK → commissions.id` | District → State linkage (`SET NULL` on delete) |
| `created_at`, `updated_at` | `timestamptz` | Auto-managed |

**Upsert key:** `commission_id_ext`

**Indexes:**
- `idx_commissions_parent_id` on `parent_commission_id`
- `idx_commissions_state_id` on `state_id`
- `idx_commissions_type` on `commission_type`

---

### 3.2 `cases`

One row per Samsung case across all commissions. Populated in two phases: first a lightweight list record from `fetch_cases`, then enriched with full detail by `fetch_case_detail`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | Internal surrogate key |
| `case_number` | `varchar(100) UNIQUE NOT NULL` | e.g. `DC/77/CC/104/2025` |
| `file_application_number` | `varchar(100)` | Alternative reference |
| `filing_reference_number` | `bigint UNIQUE` | Critical: used as PDF API parameter; set only by `fetch_case_detail` |
| `commission_id` | `bigint FK → commissions.id NOT NULL` | Parent commission |
| `case_type_name`, `case_type_id` | `varchar / integer` | Case type |
| `case_stage_name`, `case_stage_id` | `varchar / integer` | Current stage (free text from API) |
| `case_category_name` | `varchar(255)` | Category |
| `filing_date`, `date_of_cause`, `date_of_next_hearing` | `date` | Key dates |
| `complainant_name`, `respondent_name` | `varchar(500)` | Party names |
| `complainant_advocate_names`, `respondent_advocate_names` | `text` | JSON array strings |
| `status` | `enum` | Derived: `open`, `closed`, or `pending` |
| `data_hash` | `varchar(32)` | MD5 of last `getCaseStatus` response; used to skip unchanged cases |
| `last_fetched_at` | `timestamptz` | NULL = never fetched detail; used as priority queue signal |
| `created_at`, `updated_at` | `timestamptz` | Auto-managed |

**Upsert key:** `case_number`

**Indexes:**
- `idx_cases_commission_id` on `commission_id`
- `idx_cases_status` on `status`
- `idx_cases_filing_date` on `filing_date`
- `idx_cases_stage_name` on `case_stage_name`
- `idx_cases_date_of_next_hearing` on `date_of_next_hearing`
- `idx_cases_needs_detail_fetch` on `last_fetched_at` **WHERE** `last_fetched_at IS NULL` (partial index — only indexes the rows the detail job queries)

---

### 3.3 `hearings`

One row per entry in the `caseHearingDetails` array from `getCaseStatus`. Each case can have many hearings.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | |
| `case_id` | `bigint FK → cases.id CASCADE` | Parent case |
| `court_room_hearing_id` | `varchar(50) NOT NULL` | External hearing ID from API |
| `date_of_hearing` | `date` | When this hearing took place |
| `date_of_next_hearing` | `date` | Next scheduled hearing |
| `case_stage` | `varchar(255)` | Stage at time of this hearing |
| `proceeding_text` | `text` | Raw HTML blob (Word-generated markup); stored compressed via Postgres TOAST |
| `daily_order_status` | `boolean` | Whether an order was issued |
| `order_type_id` | `integer` | Type of order |
| `daily_order_availability_status` | `integer` | `NULL`=N/A, `1`=not yet available, **`2`=PDF available** (triggers fetch) |
| `hearing_sequence_number` | `integer` | Order within the case; 0 = first/placeholder |
| `created_at`, `updated_at` | `timestamptz` | |

**Upsert key:** `(case_id, court_room_hearing_id)` — constraint name `uq_hearing_case_courtroom`

**Indexes:**
- `idx_hearings_case_id` on `case_id`
- `idx_hearings_date_of_hearing` on `date_of_hearing`
- `idx_hearings_pdf_pending` on `case_id` **WHERE** `daily_order_availability_status = 2` (partial index)

---

### 3.4 `daily_orders`

Tracks PDF fetch status for each hearing order. A row is created as a stub (`pdf_fetched=False`) by `fetch_case_detail` when it finds `dailyOrderAvailabilityStatus=2`. `fetch_orders` then fills in the PDF.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | |
| `case_id` | `bigint FK → cases.id CASCADE` | |
| `hearing_id` | `bigint FK → hearings.id SET NULL` | May be NULL for judgment stubs |
| `filing_reference_number` | `bigint NOT NULL` | PDF API parameter |
| `date_of_hearing` | `date NOT NULL` | PDF API parameter |
| `order_type_id` | `integer NOT NULL` | PDF API parameter; `1`=daily order, `2`=judgment |
| `pdf_storage_path` | `varchar(1024)` | S3 URI or local path after successful fetch |
| `pdf_fetched` | `boolean NOT NULL` | False = pending; True = done |
| `pdf_fetched_at` | `timestamptz` | When PDF was successfully stored |
| `pdf_fetch_error` | `text` | Error message if fetch failed |
| `created_at`, `updated_at` | `timestamptz` | |

**Upsert key:** `(filing_reference_number, date_of_hearing, order_type_id)` — constraint name `uq_daily_order_pdf_key`

**Indexes:**
- `idx_daily_orders_case_id` on `case_id`
- `idx_daily_orders_hearing_id` on `hearing_id`
- `idx_daily_orders_unfetched` on `id` **WHERE** `pdf_fetched = false` (partial index — only unfetched rows)

---

### 3.5 `ingestion_runs`

One audit row per batch execution. Created at the start of each job and updated with counts and duration at the end. Used by the `/health` API endpoint to surface the last run summary.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | |
| `run_started_at` | `timestamptz NOT NULL` | |
| `run_finished_at` | `timestamptz` | NULL while running |
| `total_calls` | `integer` | Total HTTP calls made |
| `success_count` | `integer` | Successful upserts |
| `fail_count` | `integer` | Failed calls/writes |
| `skip_count` | `integer` | Records skipped (hash unchanged) |
| `duration_seconds` | `float` | Wall-clock time |
| `trigger_mode` | `enum` | `scheduler`, `run_once`, or `manual` |
| `notes` | `text` | Optional summary |

**Index:** `idx_ingestion_runs_started_at` on `run_started_at`

---

### 3.6 `ingestion_errors`

Detailed error log for every failed API call or DB write. This is a **historical log** — rows are never deleted by the ingestion service. Not a retry queue (see `failed_jobs` for that).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | |
| `run_id` | `bigint FK → ingestion_runs.id SET NULL` | |
| `case_id` | `bigint` | Related case (no FK — may reference deleted cases) |
| `endpoint` | `varchar(512)` | URL path that failed |
| `http_status` | `integer` | HTTP response code if applicable |
| `error_type` | `enum` | `HTTP_ERROR`, `PARSE_ERROR`, `DB_ERROR`, `TIMEOUT`, `RATE_LIMITED`, `UNKNOWN` |
| `error_message` | `text NOT NULL` | Human-readable description |
| `request_payload` | `text` | JSON-serialised query params sent |
| `response_body` | `text` | First 4 KB of response body |
| `retry_count` | `integer` | How many retries were attempted |
| `created_at` | `timestamptz` | |

**Indexes:**
- `idx_ingestion_errors_run_id` on `run_id`
- `idx_ingestion_errors_created_at` on `created_at`

---

### 3.7 `failed_jobs`

Retry queue for jobs that failed after exhausting all retries. Unlike `ingestion_errors`, this represents **work still to be done**. The ingestion service is designed to sweep rows where `resolved = false AND next_retry_at <= now()`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | |
| `job_type` | `enum` | `fetch_commissions`, `fetch_cases`, `fetch_case_detail`, `fetch_daily_order` |
| `case_id` | `bigint` | Related case (informational) |
| `commission_id` | `bigint` | Related commission (informational) |
| `endpoint` | `varchar(512)` | URL path to retry |
| `params` | `text` | JSON-encoded query params for exact replay |
| `retry_count` | `integer` | Retries already attempted |
| `last_attempted_at` | `timestamptz NOT NULL` | |
| `next_retry_at` | `timestamptz` | When to attempt next; set by retry scheduler |
| `reason` | `text NOT NULL` | Why it failed |
| `resolved` | `boolean NOT NULL` | `false` = pending retry; `true` = resolved or cleared manually |
| `created_at` | `timestamptz` | |

**Indexes:**
- `idx_failed_jobs_sweep` on `(next_retry_at, resolved)` **WHERE** `resolved = false` (partial index for the sweeper query)
- `idx_failed_jobs_case_id` on `case_id`

---

### 3.8 `api_call_log`

Low-level log of every outbound HTTP request. Used for rate-limit auditing, 429 debugging, and performance profiling. High write volume — consider a TTL purge job (keep last 90 days) in production.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `bigint PK` | |
| `run_id` | `bigint FK → ingestion_runs.id SET NULL` | |
| `endpoint` | `varchar(512)` | URL path |
| `method` | `varchar(10)` | Default `GET` |
| `response_code` | `integer` | HTTP status returned |
| `duration_ms` | `integer` | Round-trip time |
| `retry_count` | `integer` | Retries before this final response |
| `user_agent` | `varchar(512)` | User-Agent header used |
| `called_at` | `timestamptz` | |

**Indexes:**
- `idx_api_call_log_run_id` on `run_id`
- `idx_api_call_log_called_at` on `called_at`
- `idx_api_call_log_response_code` on `response_code`

---

### 3.9 Schema Enums

All enums are created as native PostgreSQL enum types and enforced at the DB level:

| Enum type | Values |
|-----------|--------|
| `commission_type_enum` | `national`, `state`, `district` |
| `case_status_enum` | `open`, `closed`, `pending` |
| `trigger_mode_enum` | `scheduler`, `run_once`, `manual` |
| `error_type_enum` | `HTTP_ERROR`, `PARSE_ERROR`, `DB_ERROR`, `TIMEOUT`, `RATE_LIMITED`, `UNKNOWN` |
| `job_type_enum` | `fetch_commissions`, `fetch_cases`, `fetch_case_detail`, `fetch_daily_order` |

> **Note on `error_type_enum`:** The Python enum uses lowercase names (`http_error`) but uppercase values (`HTTP_ERROR`). SQLAlchemy is configured with `values_callable=lambda x: [e.value for e in x]` to store the uppercase value (what PostgreSQL expects), not the attribute name.

---

## 4. HTTP Client

**File:** `ingestion/client.py`

`EJagritiClient` is a synchronous HTTP client built on top of `httpx.Client`. It is the only class that makes outbound requests to the e-Jagriti portal.

### Constructor parameters

```python
EJagritiClient(
    base_url="https://e-jagriti.gov.in/services",
    max_concurrent=2,   # EJAGRITI_MAX_CONCURRENT_REQUESTS
    max_retries=5,      # EJAGRITI_MAX_RETRIES
    timeout=30.0,
)
```

### Rate limiting — `calculate_interval()`

Every job sleeps between API calls using `calculate_interval(daily_budget)`:

```
base_sleep = 86400 / daily_budget          # spread calls evenly across 24h
sleep_with_jitter = base * (1 ± 0.20)     # ±20% random deviation
```

With the default `EJAGRITI_DAILY_CALL_BUDGET=3500`, the base interval is ~24.7 seconds per call. The ±20% jitter prevents fingerprinting from perfectly regular request timing.

> **Dev note:** During development, `calculate_interval` has `return 3` hardcoded at the top of the function body to speed up local runs. The calculation code below it is dead. Remove this line before deploying to production.

### Concurrency cap

A `threading.Semaphore(max_concurrent)` wraps every request. Only `max_concurrent` (default: 2) requests can be in-flight simultaneously across all threads.

### User-Agent rotation

Before every request, `_build_headers()` randomly picks from a pool of 7 realistic browser User-Agent strings (Chrome/Firefox on Windows/Mac/Linux). This makes requests look like different browser users to avoid bot detection.

### Retry logic

The `get()` method loops up to `max_retries + 1` times:

| Scenario | Action |
|----------|--------|
| HTTP 403 Forbidden | Raise `PermissionError` immediately — no retry |
| HTTP 429, 502, 503, 504 | Sleep with exponential backoff + jitter, then retry |
| `httpx.TimeoutException` or `httpx.NetworkError` | Sleep with exponential backoff + jitter, then retry |
| Any other non-2xx (`HTTPStatusError`) | Log error, set `last_exc`, **break** (do not retry) |
| 2xx | Return parsed JSON body |

**Backoff formula:** `sleep = (2 ** attempt) * (1 ± 0.20)`

- Attempt 0: ~1s
- Attempt 1: ~2s
- Attempt 2: ~4s
- Attempt 3: ~8s
- Attempt 4: ~16s

After the loop:
- If `last_exc` is set (non-retryable status hit): raises `RuntimeError("Non-retryable HTTP error for <url>")`
- Otherwise (retries exhausted on retryable status): raises `RuntimeError("Exhausted N retries for <url>")`

### Context manager usage

```python
with EJagritiClient(base_url=...) as client:
    data = client.get("/some/path", params={"key": "val"})
# client.close() called automatically on exit
```

---

## 5. Operating Modes

The service has two main modes and one modifier flag, controlled entirely by environment variables.

### Mode 1: Always-on scheduler (default)

```
EJAGRITI_RUN_ONCE=false   (or not set)
```

`main.py` creates an APScheduler `BackgroundScheduler`, registers SIGTERM/SIGINT handlers for graceful shutdown, starts the scheduler, and enters an infinite `while True: sleep(60)` loop. Jobs fire at their configured UTC cron times each day.

Suitable for: long-running containers (Docker, Kubernetes, EC2).

### Mode 2: RUN_ONCE

```
EJAGRITI_RUN_ONCE=true
```

`main.py` calls `run_once_batch()` from `scheduler.py`, which runs all 5 jobs **sequentially in dependency order** and then returns. The process exits with code `0` on success or `1` on unhandled error.

Suitable for: Cloud Run Jobs, ECS Scheduled Tasks, cron-triggered serverless containers where you want a process that starts, does its work, and terminates.

### Modifier: DRY_RUN

```
EJAGRITI_DRY_RUN=true
```

Can be combined with either mode. The service fetches data from the API normally but **skips all DB writes and PDF storage**. Log lines with `dry_run_skip_*` events are emitted instead.

Use for: smoke-testing API connectivity, verifying the service can reach e-Jagriti without side effects.

### Startup sequence (both modes)

```
1. load_dotenv()              — load ingestion/.env
2. _configure_logging()       — set up stdout + rotating file handler + structlog
3. log startup parameters     — run_once, dry_run, search_keyword, daily_budget
4. check_db_connection()      — verify PostgreSQL is reachable (sys.exit(1) if not)
5. dispatch to mode           — scheduler.start() or run_once_batch()
```

---

## 6. Scheduling

**File:** `ingestion/scheduler.py`

### APScheduler setup

```python
BackgroundScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=DATABASE_URL, tablename="apscheduler_jobs")},
    timezone="UTC",
)
```

The job store is backed by the same PostgreSQL database as the application data. This means:
- **Jobs survive container restarts** — APScheduler persists job state to the `apscheduler_jobs` table
- **No double-firing on multi-instance deployments** — SQLAlchemyJobStore uses table-level advisory locks per job

All jobs use `replace_existing=True` so updated schedules take effect on restart without manual DB cleanup.

### Daily job schedule (UTC)

| Time (UTC) | Job | Description |
|------------|-----|-------------|
| 00:00 | `fetch_commissions` | Refresh all ~700+ commission records |
| 01:00 | `fetch_cases` | Scan all commissions for Samsung cases |
| 06:00 | `fetch_case_detail` | Fetch full detail for cases with `last_fetched_at IS NULL` |
| 12:00 | `fetch_orders` | Download PDFs for hearings with `pdf_fetched=False` |
| 18:00 | `fetch_judgments` | Queue judgment PDFs for closed cases |

The 6-hour spacing gives each job time to complete before the next one starts. The order matters: `fetch_cases` depends on commissions existing; `fetch_case_detail` depends on cases existing; `fetch_orders` depends on daily_order stubs created by `fetch_case_detail`.

### misfire_grace_time

Every job is configured with `misfire_grace_time=3600` (1 hour). If a job misses its scheduled time (e.g. the container was down), APScheduler will still run it if the missed time is within 1 hour of the scheduled time. If more than 1 hour has elapsed, the misfire is discarded.

### `_run_job()` wrapper

Every job is called through the `_run_job()` wrapper in `scheduler.py`, which handles:

1. Creates an `IngestionRun` row in the DB (for audit)
2. Instantiates a fresh `EJagritiClient`
3. Calls the job's `run()` function
4. On any exception: logs `job_failed_unexpectedly`, returns empty stats
5. Always closes the `IngestionRun` row with counts and duration (even on failure)

This means a single job crashing never affects other scheduled jobs.

---

## 7. Environment Variables

All variables are loaded from `ingestion/.env` via `python-dotenv` at startup.

### Platform-standard variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | — | Full PostgreSQL connection URL. e.g. `postgresql://postgres:root@localhost:5432/ejagriti` |
| `POSTGRES_USER` | `postgres` | Used by Docker Compose to initialize the PostgreSQL container |
| `POSTGRES_PASSWORD` | `root` | Used by Docker Compose |
| `POSTGRES_DB` | `ejagriti` | Database name; must match `DATABASE_URL` |
| `REPLICA_DATABASE_URL` | _(empty)_ | Optional read-replica URL for SELECT queries |
| `REDIS_URL` | `redis://redis:6379/0` | Redis URL (not yet used by ingestion service) |
| `SECRET_KEY` | — | Flask secret key (used by the API service, not ingestion) |
| `AWS_S3_BUCKET` | _(empty)_ | S3 bucket name for PDF storage. Leave empty to use local filesystem |

### eJagriti custom variables (all prefixed `EJAGRITI_`)

| Variable | Default | Description |
|----------|---------|-------------|
| `EJAGRITI_BASE_URL` | `https://e-jagriti.gov.in` | Root URL of the portal. `/services` is appended automatically |
| `EJAGRITI_SEARCH_KEYWORD` | `samsung` | Company name passed to `getCauseTitleListByCompany` |
| `EJAGRITI_DAILY_CALL_BUDGET` | `3500` | Target number of API calls per 24-hour window; controls sleep interval |
| `EJAGRITI_MAX_CONCURRENT_REQUESTS` | `2` | Semaphore cap on simultaneous in-flight HTTP requests |
| `EJAGRITI_MAX_RETRIES` | `5` | Max retry attempts per API call before raising |
| `EJAGRITI_FETCH_CASES_FROM_DATE` | `2015-01-01` | Start date for the case list scan window |
| `EJAGRITI_RUN_ONCE` | `false` | `true` = run full batch once and exit (Cloud Run / ECS mode) |
| `EJAGRITI_DRY_RUN` | `false` | `true` = fetch data but skip all DB writes |
| `EJAGRITI_PDF_STORAGE_DIR` | _(empty)_ | Local directory for PDF files. e.g. `daily_orders`. Ignored if `AWS_S3_BUCKET` is set |
| `EJAGRITI_LOG_LEVEL` | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `EJAGRITI_LOG_DIR` | `logs` | Directory for rotating log files (relative to `ingestion/` or absolute) |
| `EJAGRITI_RATE_LIMIT_PER_MINUTE` | `100` | Used by API service (not ingestion) |
| `EJAGRITI_CACHE_TTL_SECONDS` | `3600` | Used by API service (not ingestion) |
| `EJAGRITI_SA_POOL_SIZE` | `5` | SQLAlchemy connection pool size |
| `EJAGRITI_SA_MAX_OVERFLOW` | `10` | SQLAlchemy max overflow connections |

---

## 8. Logging

**File:** `ingestion/main.py` — `_configure_logging()`

### Output format

All logs are emitted as **structured JSON** via `structlog`. Each log line is a single JSON object:

```json
{
  "event": "http_call",
  "endpoint": "/case/caseFilingService/v2/getCaseStatus",
  "response_code": 200,
  "duration_ms": 312,
  "attempt": 0,
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
  "timestamp": "2026-03-29T06:12:34.123456Z",
  "level": "info",
  "logger": "client"
}
```

### Handlers

Two handlers are registered on the root logger:

| Handler | Destination | Format |
|---------|-------------|--------|
| `StreamHandler` | `stdout` | JSON |
| `TimedRotatingFileHandler` | `{EJAGRITI_LOG_DIR}/ingestion.log` | JSON |

### Log file rotation

- **Rotates at:** midnight UTC (`when="midnight"`, `utc=True`)
- **Retention:** 30 days (`backupCount=30`)
- **Current file:** `ingestion.log`
- **Rotated files:** `ingestion.log.YYYY-MM-DD`
- **Encoding:** UTF-8

The `EJAGRITI_LOG_DIR` directory is created automatically at startup if it does not exist.

### Suppressed loggers

These loggers are forced to `WARNING` level regardless of `EJAGRITI_LOG_LEVEL`:

| Logger | Reason |
|--------|--------|
| `httpx` | Suppresses verbose request/response debug output |
| `httpcore` | Suppresses low-level transport events: `connect_tcp.started`, `start_tls.started`, etc. These flood logs at DEBUG level with ~5 lines per HTTP call |

---

*— See sections 9.3–9.5 below for fetch_case_detail, fetch_orders, and fetch_judgments —*

---

## 9. Jobs Reference

Each job is a Python module under `ingestion/jobs/`. Every job exposes a single `run()` function with this signature:

```python
def run(
    client: EJagritiClient,
    run_id: int,
    dry_run: bool = False,
    daily_budget: int = 3500,
) -> dict[str, int]:
    ...
```

The `scheduler.py` `_run_job()` wrapper calls this function — jobs never instantiate their own client or manage `IngestionRun` rows directly.

---

### 9.1 `fetch_commissions`

**Scheduled:** 00:00 UTC daily

**Purpose:** Refreshes the full registry of consumer commissions from the portal. This is the foundation job — every other job depends on commissions being present. Must complete before `fetch_cases` runs at 01:00.

---

#### What the API returns

The job makes **two types of API calls**:

**Call 1 — Get all top-level commissions**

```
GET /master/master/v2/getAllCommission
```

Returns a flat list of the national commission (NCDRC) and all state commissions. Does **not** include district commissions and does **not** return `commissionTypeId` or district metadata.

Example response item:
```json
{
  "commissionId": 11000000,
  "commissionNameEn": "National Consumer Disputes Redressal Commission",
  "stateId": null
}
```

**Call 2 — Get state + district commissions by state**

```
GET /master/master/v2/getCommissionDetailsByStateId?stateId=N
```

Called once per unique `stateId` from Call 1. Returns richer data including `commissionTypeId`, `districtId`, and `casePrefixText`. This is where all district commissions come from.

Example response item:
```json
{
  "commissionId": 20300,
  "commissionNameEn": "District Consumer Disputes Redressal Commission, Agra",
  "commissionTypeId": 3,
  "stateId": 9,
  "districtId": 42,
  "casePrefixText": "DC/42/",
  "circuitAdditionBenchStatus": 0
}
```

---

#### Commission type inference

The `commission_type` enum value is derived differently for each call:

**For Call 1 (getAllCommission)** — `_classify_top_level(commission_id_ext)`:

```
commission_id_ext == 11000000  →  national   (NCDRC is the only national commission)
anything else                  →  state
```

**For Call 2 (getCommissionDetailsByStateId)** — `_api_type_to_enum(commissionTypeId)`:

```
commissionTypeId = 1  →  national
commissionTypeId = 2  →  state
commissionTypeId = 3  →  district
```

---

#### Parent-commission linkage

District commissions are linked to their parent state commission via `parent_commission_id` (a self-referencing FK in the `commissions` table). The job builds this link dynamically:

1. After upserting all Call 1 commissions, an `ext_to_internal` dict maps `commission_id_ext → internal DB id`
2. For each district commission in Call 2, the job scans the Call 1 list to find the state commission with the same `stateId`
3. That state commission's internal id becomes `parent_commission_id` for the district row

```
getAllCommission result:
  { commissionId: 5000, stateId: 9 }  →  internal id = 42  (state commission for UP)

getCommissionDetailsByStateId?stateId=9 result:
  { commissionId: 20300, commissionTypeId: 3, stateId: 9 }
    → parent_commission_id = 42  (linked to UP state commission)
```

---

#### DB writes

**Table:** `commissions`
**Upsert key:** `commission_id_ext`
**On conflict:** updates all non-key fields

Fields written per commission row:

| Field | Source |
|-------|--------|
| `commission_id_ext` | `commissionId` |
| `name_en` | `commissionNameEn` |
| `commission_type` | Inferred (see above) |
| `state_id` | `stateId` |
| `district_id` | `districtId` (Call 2 only) |
| `case_prefix_text` | `casePrefixText` (Call 2 only) |
| `circuit_addition_bench_status` | `circuitAdditionBenchStatus` (default 0) | Call 2 only |
| `parent_commission_id` | Resolved via `ext_to_internal` map | Call 2 only (district rows) |

---

#### Stats returned

```python
{"upserted": int, "failed": int}
```

---

#### Error handling

| Error | Logged to | Behaviour |
|-------|-----------|-----------|
| 403 on `getAllCommission` | `failed_jobs` | Returns immediately with 0 upserts |
| Any error on `getAllCommission` | `ingestion_errors` | Returns immediately |
| 403 on per-state call | Logged only | `stats["failed"] += 1`, continues to next state |
| Any error on per-state call | `ingestion_errors` | `stats["failed"] += 1`, continues to next state |
| DB upsert failure | `ingestion_errors` | `stats["failed"] += 1`, continues to next commission |

---

### 9.2 `fetch_cases`

**Scheduled:** 01:00 UTC daily

**Purpose:** For every commission in the DB, queries the portal for Samsung cases and upserts them into the `cases` table. This populates the case list with lightweight fields only — full case detail (hearings, advocates, filing reference number) is fetched separately by `fetch_case_detail`.

---

#### Selection criteria

The job reads **all commissions** from the DB:

```sql
SELECT id, commission_id_ext, commission_type FROM commissions
```

There is no filtering — every national, state, and district commission is queried. Typically ~700+ API calls per run (one per commission).

---

#### API call

```
GET /report/report/getCauseTitleListByCompany
  ?commissionTypeId=N
  &commissionId=N
  &filingDate1=YYYY-MM-DD
  &filingDate2=YYYY-MM-DD
  &complainant_respondent_name_en=samsung
```

Parameters per commission:

| Parameter | Value | Source |
|-----------|-------|--------|
| `commissionTypeId` | `1` (national), `2` (state), `3` (district) | Mapped from `commission_type` enum |
| `commissionId` | External commission ID | `commission_id_ext` |
| `filingDate1` | Start of date window | `EJAGRITI_FETCH_CASES_FROM_DATE` (default `2015-01-01`) |
| `filingDate2` | End of date window | `date.today()` at run time |
| `complainant_respondent_name_en` | `samsung` | `EJAGRITI_SEARCH_KEYWORD` |

**Date window note:** The default window starts from `2015-01-01`, which means the first run performs a full historical scan. If you want subsequent runs to only fetch recent cases, change `EJAGRITI_FETCH_CASES_FROM_DATE` to a more recent date (e.g. last month). The variable is read fresh each run, not persisted.

**Response:** A JSON list (or `{"data": [...]}` wrapper) of case objects. Returns an empty list for commissions with no Samsung cases — this is the common case for most district commissions.

---

#### DB writes

**Table:** `cases`
**Upsert key:** `case_number`
**On conflict:** updates all non-key fields

Fields written per case row:

| Field | Source | Notes |
|-------|--------|-------|
| `case_number` | `case_number` or `caseNumber` | Tries both key names |
| `file_application_number` | `file_application_number` | |
| `commission_id` | Internal commission id | From the outer loop — **not** from API |
| `case_type_name` | `case_type_name` | |
| `case_stage_name` | `case_stage_name` | Free text; many undocumented values |
| `case_category_name` | `case_category_name` | |
| `filing_date` | `case_filing_date` | Parsed as ISO date |
| `date_of_next_hearing` | `date_of_next_hearing` | Parsed as ISO date |
| `complainant_name` | `complainant_name` | |
| `respondent_name` | `respondent_name` | |
| `complainant_advocate_names` | `complainant_advocate_name` | Wrapped as JSON string `["name"]` |
| `respondent_advocate_names` | `respondent_advocate_name` | Wrapped as JSON string `["name"]` |
| `status` | Derived via `_map_status()` | See below |

**What is NOT written here:**

- `filing_reference_number` — not available in this endpoint; set later by `fetch_case_detail`
- `last_fetched_at` — remains `NULL` after `fetch_cases`; this is intentional and is the signal that `fetch_case_detail` uses to prioritise this case
- `data_hash` — not set here
- `hearings` — not returned by this endpoint

---

#### Status derivation — `_map_status(stage_name)`

The `status` column is a derived field computed from the free-text `case_stage_name`:

```
stage_name contains any of:
  DISPOSED, DISMISSED, WITHDRAWN, CLOSED, DECIDED, ALLOWED, REJECTED
  → "closed"

stage_name is exactly (case-insensitive):
  REGISTERED, ADMIT, NOTICE ISSUED
  → "open"

anything else (including None/empty)
  → "pending"
```

The same logic exists in both `fetch_cases.py` and `fetch_case_detail.py`. The detail job may update the status when it fetches a richer `caseStage` value.

---

#### Stats returned

```python
{"fetched": int, "upserted": int, "failed": int}
```

- `fetched` — total case records returned across all commission API calls
- `upserted` — cases successfully written to DB
- `failed` — commissions or cases that errored

---

#### Error handling

| Error | Logged to | Behaviour |
|-------|-----------|-----------|
| No commissions in DB | Warning log only | Returns immediately |
| 403 on a commission | `failed_jobs` (with `commission_id` and `params`) | `stats["failed"] += 1`, continues |
| Any HTTP/network error | `ingestion_errors` | `stats["failed"] += 1`, continues |
| Empty `case_number` in response | Warning log only | Skips that case item |
| DB upsert failure | Error log only | `stats["failed"] += 1`, continues |

---

### 9.3 `fetch_case_detail`

**Scheduled:** 06:00 UTC daily

**Purpose:** Enriches cases with full detail from the `getCaseStatus` endpoint. This is the heaviest job — it fetches one API call per case and unpacks nested hearing arrays. It also creates `daily_orders` stub rows that trigger PDF downloads in the next job.

---

#### Selection criteria

```sql
SELECT id, case_number, data_hash, filing_reference_number
  FROM cases
 WHERE last_fetched_at IS NULL
 LIMIT 50
```

Only cases where `last_fetched_at IS NULL` are processed — meaning cases that have **never** had a detail fetch. `fetch_cases` writes cases with `last_fetched_at = NULL`, so all newly discovered cases are automatically queued here. Once a case is successfully processed, `last_fetched_at` is set to the current timestamp and the case drops out of future runs.

The partial index `idx_cases_needs_detail_fetch` makes this query O(unfetched) rather than O(all cases).

**Batch size:** 50 cases per run (constant `_BATCH_SIZE = 50`). The job processes at most 50 cases per daily invocation. For the initial load with thousands of cases, this means the backlog drains over many days.

---

#### API call

```
GET /case/caseFilingService/v2/getCaseStatus?caseNumber=DC%2F77%2FCC%2F104%2F2025
```

`caseNumber` is the raw case number string (slashes included). The `httpx` client URL-encodes the query parameter automatically.

Expected response structure:

```json
{
  "status": 200,
  "data": {
    "caseStage": "ADMITTED",
    "caseStageId": 3,
    "caseTypeId": 1,
    "caseFilingDate": "2025-01-15",
    "dateOfCause": "2024-12-01",
    "dateOfNextearing": "2025-07-10",
    "fillingReferenceNumber": 987654,
    "complainant": "Rajesh Kumar",
    "respondent": "Samsung India Electronics Pvt. Ltd.",
    "complainantAdvocate": ["Adv. Sharma"],
    "respondentAdvocate": ["Adv. Mehta"],
    "caseHearingDetails": [ ... ]
  }
}
```

The job checks `resp.get("status") != 200 or not resp.get("data")` and logs a warning if the response is empty/unexpected, incrementing `failed`.

---

#### MD5 hash change detection

Before writing anything to the DB, the job computes:

```python
new_hash = md5(json.dumps(data, sort_keys=True, default=str))
```

If `new_hash == existing data_hash` (stored from the previous fetch), the case is **skipped** entirely — no DB writes, `stats["skipped"] += 1`.

**Important:** The hash check uses `if existing_hash and existing_hash == new_hash`. On a case's **first** fetch, `existing_hash` is `None` (never fetched before), so the condition short-circuits to `False` — the case always proceeds through on first fetch regardless of hash. `data_hash` and `last_fetched_at` are written together in the same DB operation, so a case with a stored hash will also have `last_fetched_at` set, excluding it from future selection queries. In practice, the hash check only activates if you manually reset `last_fetched_at` to NULL to force a re-fetch.

---

#### API quirk — double-l typo

The e-Jagriti API has a typo in the field name for the filing reference number:

```python
filing_ref = data.get("fillingReferenceNumber") or data.get("filingReferenceNumber")
```

The correct spelling is `filingReferenceNumber` but the API consistently returns `fillingReferenceNumber` (double-l). The code tries both to be safe. This value is critical — it is used as the PDF API parameter `filingReferenceNumber` in `fetch_orders`.

---

#### Case row update

The job does a **direct UPDATE** (never INSERT) on the `cases` row:

```python
session.execute(
    sa_update(Case).where(Case.case_number == case_number).values(**case_update)
)
```

Why UPDATE-only and not upsert? Because `commission_id` (NOT NULL) is not available in this response. An upsert that falls through to INSERT would fail the NOT NULL constraint. Since we queried the case from the DB to get here, it is guaranteed to exist — UPDATE is correct.

Fields updated on `cases`:

| Field | API source | Notes |
|-------|-----------|-------|
| `filing_reference_number` | `fillingReferenceNumber` / `filingReferenceNumber` | The critical PDF param |
| `case_stage_name` | `caseStage` | |
| `case_stage_id` | `caseStageId` | |
| `case_type_id` | `caseTypeId` | |
| `filing_date` | `caseFilingDate` or `dateOfCause` | First non-null wins |
| `date_of_cause` | `dateOfCause` | |
| `date_of_next_hearing` | `dateOfNextearing` | Note: API typo — missing `h` |
| `complainant_name` | `complainant` | |
| `respondent_name` | `respondent` | |
| `complainant_advocate_names` | `complainantAdvocate` | JSON-serialised array |
| `respondent_advocate_names` | `respondentAdvocate` | JSON-serialised array |
| `status` | Derived from `caseStage` via `_map_status()` | |
| `data_hash` | Computed MD5 of full `data` block | |
| `last_fetched_at` | `datetime.now(UTC)` | Set here for the first time |

`None` values are stripped from the update dict (except `date_of_next_hearing` which is explicitly allowed to be NULL so it can be cleared).

---

#### Hearing upsert — `caseHearingDetails[]`

For each item in `data["caseHearingDetails"]`:

1. Skip if `courtRoomHearingId` is absent/empty
2. Build a `hearing_data` dict and call `upsert_hearing(session, hearing_data)`

**Upsert key:** `(case_id, court_room_hearing_id)` — constraint `uq_hearing_case_courtroom`

Fields written to `hearings`:

| Field | API source |
|-------|-----------|
| `case_id` | Internal case DB id |
| `court_room_hearing_id` | `courtRoomHearingId` |
| `date_of_hearing` | `dateOfHearing` |
| `date_of_next_hearing` | `dateOfNextHearing` |
| `case_stage` | `caseStage` |
| `proceeding_text` | `proceedingText` (raw HTML blob) |
| `daily_order_status` | `dailyOrderStatus` |
| `order_type_id` | `orderTypeId` |
| `daily_order_availability_status` | `dailyOrderAvailabilityStatus` |
| `hearing_sequence_number` | `hearingSequenceNumber` (default 0) |

---

#### DailyOrder stub creation

After upserting a hearing, the job checks:

```python
if (
    h.get("dailyOrderAvailabilityStatus") == 2   # PDF is available
    and filing_ref                                # we have the PDF API param
    and h.get("dateOfHearing")                   # we have the PDF API param
):
```

If all three conditions are true, a stub row is inserted into `daily_orders`:

```python
{
    "case_id":                 case_db_id,
    "hearing_id":              hearing_db_id,    # from the upsert above
    "filing_reference_number": filing_ref,       # PDF API param
    "date_of_hearing":         parsed date,      # PDF API param
    "order_type_id":           h.get("orderTypeId") or 1,
    "pdf_fetched":             False,            # triggers fetch_orders
}
```

**Upsert key:** `(filing_reference_number, date_of_hearing, order_type_id)` — constraint `uq_daily_order_pdf_key`

This is an upsert, not an insert — re-running the job for the same case does not create duplicate rows.

`fetch_orders` will pick up any row where `pdf_fetched = False` at 12:00 UTC.

---

#### Stats returned

```python
{"fetched": int, "updated": int, "skipped": int, "failed": int}
```

---

#### Error handling

| Error | Logged to | Behaviour |
|-------|-----------|-----------|
| 403 on `getCaseStatus` | `failed_jobs` (with `case_id` and params) | `stats["failed"] += 1`, continues |
| Any HTTP/network error | `ingestion_errors` | `stats["failed"] += 1`, continues |
| Empty/unexpected API response | Warning log only | `stats["failed"] += 1`, continues |
| Hash unchanged | Debug log | `stats["skipped"] += 1`, continues |
| DB write failure | `ingestion_errors` | Returns `"failed"` from `_process_detail()` |

---

### 9.4 `fetch_orders`

**Scheduled:** 12:00 UTC daily

**Purpose:** Downloads daily order PDFs for all hearings that have been flagged as having available PDFs. Processes the `daily_orders` backlog — any row where `pdf_fetched = False`.

---

#### Selection criteria

```sql
SELECT id, case_id, filing_reference_number, date_of_hearing, order_type_id
  FROM daily_orders
 WHERE pdf_fetched = false
 ORDER BY id
 LIMIT 100
```

The partial index `idx_daily_orders_unfetched` makes this query efficient regardless of total table size.

**Batch size:** 100 PDFs per run (constant `_BATCH_SIZE = 100`).

Rows are ordered by `id` (insertion order), so older pending PDFs are fetched first.

---

#### API call

```
GET /courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf
  ?filingReferenceNumber=987654
  &dateOfHearing=2025-06-18
  &orderTypeId=1
```

All three parameters come directly from the `daily_orders` row — they were stored by `fetch_case_detail` when it created the stub.

Expected response:

```json
{
  "status": 200,
  "data": {
    "dailyOrderPdf": "<base64-encoded PDF string>"
  }
}
```

The PDF content is extracted as:

```python
pdf_b64 = (resp.get("data") or {}).get("dailyOrderPdf", "")
```

If `pdf_b64` is empty/missing, the job logs a warning and increments `failed`. The row is left with `pdf_fetched=False` so it will be retried next run.

---

#### Base64 decode

```python
pdf_bytes = base64.b64decode(pdf_b64)
```

The decoded bytes are a standard PDF binary. If decoding fails, the error is written to `daily_orders.pdf_fetch_error` and the row is left unfetched.

---

#### PDF storage — priority cascade

```
if AWS_S3_BUCKET is set:
    upload to s3://{bucket}/daily_orders/{filing_ref}_{date}_type{order_type_id}.pdf
    return S3 URI

elif EJAGRITI_PDF_STORAGE_DIR is set:
    write to {dir}/{filing_ref}_{date}_type{order_type_id}.pdf
    return local path string

else:
    discard PDF bytes
    return ""  (pdf_storage_path will be NULL)
```

The directory is created automatically if it does not exist. On S3 failure, the error is logged and an empty string is returned (not a fatal error).

> **Dev setup:** Set `EJAGRITI_PDF_STORAGE_DIR=daily_orders` in `.env` to store PDFs in `ingestion/daily_orders/` during local development.

---

#### DB update on success

```sql
UPDATE daily_orders
   SET pdf_fetched      = true,
       pdf_fetched_at   = <now UTC>,
       pdf_storage_path = <path or NULL>,
       pdf_fetch_error  = NULL,
       updated_at       = now()
 WHERE id = <order_id>
```

---

#### DB update on failure

If the API call raises `PermissionError` (403) or any other exception:

```sql
UPDATE daily_orders
   SET pdf_fetch_error = <error string>
 WHERE id = <order_id>
```

The row stays with `pdf_fetched=False` so it remains in the unfetched index and will be retried next run. A `failed_jobs` entry is also written for 403 errors.

> **Infinite retry risk:** If a PDF consistently returns a non-403 error, it will be retried every day indefinitely. To stop retrying, manually set `pdf_fetched=True` or clear the row.

---

#### Stats returned

```python
{"fetched": int, "stored": int, "failed": int}
```

- `fetched` — API calls that returned a response
- `stored` — PDFs successfully decoded and stored
- `failed` — rows that errored at any stage

---

#### Error handling

| Error | Logged to | DB update | Behaviour |
|-------|-----------|-----------|-----------|
| 403 | `failed_jobs` | `pdf_fetch_error` set | Continues |
| Any HTTP/network error | `ingestion_errors` | `pdf_fetch_error` set | Continues |
| Empty `dailyOrderPdf` in response | Warning log only | None | Continues |
| base64 decode / store failure | Error log only | `pdf_fetch_error` set | Continues |
| DB mark-fetched failure | Error log only | None | `stats["failed"] += 1`, continues |

---

### 9.5 `fetch_judgments`

**Scheduled:** 18:00 UTC daily

**Purpose:** Queues judgment PDFs for closed cases. The e-Jagriti portal does not have a separate judgment endpoint — a judgment is the final daily order issued when a case closes. This job identifies closed cases that don't yet have a judgment-type PDF queued and creates `daily_orders` stub rows with `order_type_id=2`. `fetch_orders` will download the actual PDFs in the next cycle.

> This job does **not** make any API calls itself. It is purely a DB-to-DB operation that seeds the `daily_orders` table.

---

#### Selection criteria

```sql
SELECT c.id, c.case_number, c.filing_reference_number, c.date_of_next_hearing
  FROM cases c
 WHERE c.status = 'closed'
   AND c.filing_reference_number IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM daily_orders d
        WHERE d.case_id = c.id
          AND d.order_type_id = 2
   )
 ORDER BY c.updated_at DESC
 LIMIT 50
```

Conditions explained:

| Condition | Reason |
|-----------|--------|
| `status = 'closed'` | Only closed cases have final judgments |
| `filing_reference_number IS NOT NULL` | Required for the PDF API call that `fetch_orders` will make |
| `NOT EXISTS (... order_type_id = 2)` | Avoids re-queuing already-queued or already-fetched judgments |
| `ORDER BY updated_at DESC` | Most recently changed cases first |
| `LIMIT 50` | Caps the batch |

---

#### Hearing date requirement

The PDF API requires a `dateOfHearing` parameter. The judgment stub needs a valid date. The job finds the most recent hearing date for which a PDF was already successfully fetched:

```sql
SELECT date_of_hearing
  FROM daily_orders
 WHERE case_id = <case_id>
   AND pdf_fetched = true
 ORDER BY date_of_hearing DESC
 LIMIT 1
```

If no successfully-fetched hearing date exists for the case (e.g. `fetch_orders` hasn't run yet, or all PDFs failed), the case is **skipped** (`stats["skipped"] += 1`). It will be picked up again in the next run once at least one hearing PDF has been fetched.

---

#### DailyOrder stub creation

For each qualifying case:

```python
{
    "case_id":                 case["id"],
    "hearing_id":              None,             # no specific hearing — this is case-level
    "filing_reference_number": case["filing_reference_number"],
    "date_of_hearing":         latest_hearing,   # most recent fetched hearing date
    "order_type_id":           2,               # judgment type
    "pdf_fetched":             False,
}
```

**Upsert key:** `(filing_reference_number, date_of_hearing, order_type_id)` — same constraint as all other daily orders. Re-running is safe.

After this job runs, `fetch_orders` will find these rows in its `WHERE pdf_fetched = false` query and download the judgment PDFs.

---

#### Stats returned

```python
{"queued": int, "skipped": int}
```

- `queued` — stub rows successfully created
- `skipped` — cases skipped (no hearing date found, or dry_run, or DB error)

---

#### Error handling

| Error | Behaviour |
|-------|-----------|
| No closed cases needing judgment | Logs `no_closed_cases_needing_judgment`, returns |
| No previously-fetched hearing date | `stats["skipped"] += 1`, continues |
| DB upsert failure | Error logged, `stats["skipped"] += 1`, continues |
| `dry_run=True` | `stats["skipped"] += 1`, continues (no DB writes) |

---

## 10. Full Data Flow

The following diagram shows how data moves through the pipeline from first API call to stored PDF:

```
Day 1 — 00:00 UTC
════════════════════════════════════════════════════════════════
fetch_commissions
  │
  ├─ GET /getAllCommission
  │    └─ UPSERT commissions (national + state rows)
  │         upsert key: commission_id_ext
  │
  └─ For each stateId:
       GET /getCommissionDetailsByStateId?stateId=N
         └─ UPSERT commissions (state + district rows)
              parent_commission_id linked via ext_to_internal map

Day 1 — 01:00 UTC
════════════════════════════════════════════════════════════════
fetch_cases
  │
  └─ For each commission in DB:
       GET /getCauseTitleListByCompany
         ?commissionId=N&filingDate1=2015-01-01&filingDate2=today&...=samsung
         │
         └─ UPSERT cases (lightweight fields only)
              upsert key: case_number
              last_fetched_at = NULL  ← signals detail job

Day 1 — 06:00 UTC
════════════════════════════════════════════════════════════════
fetch_case_detail
  │
  ├─ SELECT cases WHERE last_fetched_at IS NULL LIMIT 50
  │
  └─ For each case:
       GET /getCaseStatus?caseNumber=...
         │
         ├─ Compute MD5(data) == existing data_hash?
         │    YES → skip (no DB writes)
         │    NO  → continue
         │
         ├─ UPDATE cases (filing_reference_number, stage, dates, advocates,
         │                status, data_hash, last_fetched_at)
         │
         ├─ For each item in caseHearingDetails[]:
         │    UPSERT hearings
         │      upsert key: (case_id, court_room_hearing_id)
         │
         └─ For hearings where dailyOrderAvailabilityStatus == 2:
              UPSERT daily_orders (stub, pdf_fetched=False)
                upsert key: (filing_reference_number, date_of_hearing, order_type_id)

Day 1 — 12:00 UTC
════════════════════════════════════════════════════════════════
fetch_orders
  │
  ├─ SELECT daily_orders WHERE pdf_fetched = false LIMIT 100
  │
  └─ For each order:
       GET /getDailyOrderJudgementPdf
         ?filingReferenceNumber=N&dateOfHearing=YYYY-MM-DD&orderTypeId=N
         │
         ├─ base64 decode response
         │
         ├─ Store PDF:
         │    S3 bucket  (if AWS_S3_BUCKET set)
         │    local dir  (if EJAGRITI_PDF_STORAGE_DIR set)
         │    discard    (if neither set)
         │
         └─ UPDATE daily_orders SET pdf_fetched=true, pdf_storage_path=...,
                                     pdf_fetched_at=now()

Day 1 — 18:00 UTC
════════════════════════════════════════════════════════════════
fetch_judgments
  │
  ├─ SELECT closed cases with filing_ref but no order_type_id=2 row
  │
  └─ For each case:
       Find most recent daily_orders.date_of_hearing WHERE pdf_fetched=true
         │
         └─ UPSERT daily_orders (stub, order_type_id=2, pdf_fetched=False)
              ↑ fetch_orders will download this PDF tomorrow at 12:00
```

---

## 11. Error Handling Architecture

The service uses two separate mechanisms for errors — one for observability and one for retries:

### `ingestion_errors` — historical log

Written by `log_ingestion_error()` in `upsert.py`. Every failed API call or DB write appends a row. Rows are **never deleted or updated** by the service.

```
Use for: debugging, alerting, auditing
Query:   SELECT * FROM ingestion_errors ORDER BY created_at DESC LIMIT 50;
```

### `failed_jobs` — retry queue

Written by `log_failed_job()` in `upsert.py`. Created when a 403 is encountered (permanent-ish failures worth recording for replay). The `params` column stores the exact query parameters needed to retry the call.

```
Use for: manual or automated retry of permanently failed jobs
Query:   SELECT * FROM failed_jobs WHERE resolved = false ORDER BY created_at;
```

To mark a failed job as resolved after manual intervention:
```sql
UPDATE failed_jobs SET resolved = true WHERE id = <id>;
```

> **Note:** The retry sweeper (automatic re-execution of `failed_jobs`) is designed but **not yet implemented**. Currently, `next_retry_at` is written as `NULL` and no code reads from `failed_jobs` to re-run them. Rows accumulate for manual inspection.

### Error flow summary

```
API call fails
  │
  ├─ 403 Forbidden
  │    → log_failed_job()    (retry queue — permanent denial)
  │    → stats["failed"] += 1
  │    → continue to next item
  │
  ├─ Retryable (429/502/503/504) or network error
  │    → EJagritiClient retries internally (up to max_retries)
  │    → if all retries exhausted → raises RuntimeError
  │         → log_ingestion_error()
  │         → stats["failed"] += 1
  │         → continue to next item
  │
  └─ Non-retryable HTTP error (e.g. 500)
       → EJagritiClient raises RuntimeError immediately
            → log_ingestion_error()
            → stats["failed"] += 1
            → continue to next item

DB write fails
  └─ log_ingestion_error() with error_type=DB_ERROR
       → stats["failed"] += 1
       → continue to next item
```

No single item failure aborts the batch. Every loop catches exceptions per-item and moves on.

---

## 12. Running Locally

### Prerequisites

- Python 3.11+
- PostgreSQL 15 running locally (or via Docker)
- The `ejagriti` database must exist

**Create the database:**

```bash
# If you have psql:
psql -U postgres -c "CREATE DATABASE ejagriti;"

# Or via Python one-liner:
python -c "
import psycopg2
conn = psycopg2.connect('postgresql://postgres:root@localhost:5432/postgres')
conn.autocommit = True
conn.cursor().execute('CREATE DATABASE ejagriti')
conn.close()
print('done')
"
```

### Install dependencies

```bash
cd ingestion
pip install -r requirements.txt
```

### Configure environment

Copy `.env.example` to `ingestion/.env` and set your values:

```bash
DATABASE_URL=postgresql://postgres:root@localhost:5432/ejagriti
EJAGRITI_RUN_ONCE=true        # for a one-shot local run
EJAGRITI_DRY_RUN=false
EJAGRITI_LOG_LEVEL=DEBUG
EJAGRITI_PDF_STORAGE_DIR=daily_orders
```

### Run migrations

Always run from the `ingestion/` directory (Alembic reads `alembic.ini` from the current directory):

```bash
cd ingestion
alembic -c alembic.ini upgrade head
```

Expected output ends with:
```
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
```

Verify tables were created:
```bash
python -c "
from dotenv import load_dotenv; load_dotenv()
from db.session import get_session
from db.models import Commission, Case
with get_session(read_only=True) as s:
    print('commissions:', s.query(Commission).count())
    print('cases:', s.query(Case).count())
"
```

### Run the ingestion service

**One-shot mode** (recommended for testing):

```bash
cd ingestion
python main.py
```

With `EJAGRITI_RUN_ONCE=true`, this runs all 5 jobs sequentially and exits. Expect it to take several hours on first run due to the rate-limiting sleep intervals.

**Always-on scheduler mode:**

```bash
EJAGRITI_RUN_ONCE=false python main.py
```

The process runs indefinitely. Jobs fire at their UTC cron times. Send `Ctrl+C` or `SIGTERM` for graceful shutdown.

### Verifying output

**Check logs (stdout + file):**
```bash
# Live tail of the log file
tail -f ingestion/logs/ingestion.log | python -m json.tool
```

**Check DB row counts:**
```bash
python -c "
from dotenv import load_dotenv; load_dotenv()
from db.session import get_session
from sqlalchemy import text
with get_session(read_only=True) as s:
    for t in ['commissions','cases','hearings','daily_orders','ingestion_runs','ingestion_errors','failed_jobs']:
        n = s.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
        print(f'{t}: {n}')
"
```

**Check for errors:**
```bash
python -c "
from dotenv import load_dotenv; load_dotenv()
from db.session import get_session
from sqlalchemy import text
with get_session(read_only=True) as s:
    rows = s.execute(text('SELECT error_type, endpoint, error_message FROM ingestion_errors ORDER BY created_at DESC LIMIT 10')).fetchall()
    for r in rows: print(r)
"
```

---

## 13. Running in Docker

### Start all services

```bash
# From the project root
docker-compose up --build
```

This starts three containers:
1. `postgres` — PostgreSQL 15 with the `ejagriti` database
2. `migrations` — runs `alembic upgrade head` and exits
3. `ingestion` — waits for migrations to finish, then runs `main.py`

The `ingestion` container starts in `RUN_ONCE` or scheduler mode depending on the `EJAGRITI_RUN_ONCE` env var in `docker-compose.yml`.

### Check logs

```bash
# Follow ingestion service logs
docker-compose logs -f ingestion

# Pretty-print JSON logs
docker-compose logs -f ingestion | python -m json.tool 2>/dev/null

# View rotated log files inside container
docker-compose exec ingestion ls /app/logs/
docker-compose exec ingestion tail -f /app/logs/ingestion.log
```

### Run migrations manually

```bash
docker-compose run --rm migrations
```

### Connect to the database

```bash
docker-compose exec postgres psql -U postgres -d ejagriti
```

### Environment overrides

Override any env var at runtime without editing files:

```bash
EJAGRITI_DRY_RUN=true docker-compose up ingestion
```

Or set variables in a `.env` file at the project root (Docker Compose reads it automatically).
