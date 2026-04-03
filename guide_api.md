# eJagriti API Service — Developer Guide

This guide covers everything you need to understand, run, and extend the API service. It assumes you are a developer but have no prior knowledge of this project.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Repository Layout](#2-repository-layout)
3. [Response Envelope](#3-response-envelope)
4. [Middleware](#4-middleware)
5. [Configuration & Environment Variables](#5-configuration--environment-variables)
6. [Endpoints Reference](#6-endpoints-reference) *(Part 2)*
7. [Caching](#7-caching) *(Part 2)*
8. [Rate Limiting](#8-rate-limiting) *(Part 2)*
9. [Adding a New Endpoint](#9-adding-a-new-endpoint) *(Part 2)*
10. [Running Locally](#10-running-locally) *(Part 2)*

---

## 1. Overview

**What is this service?**

The API service is a read-only Flask HTTP API that exposes the case data collected by the ingestion service. It is the backend for the eJagriti Samsung case tracker frontend.

**Why does it exist?**

The ingestion service writes to PostgreSQL. The API service sits in front of that database and provides structured, paginated, filterable JSON endpoints so that a frontend (or any HTTP client) can query case data without touching the database directly.

**What does it expose?**

| Resource | Endpoints |
|----------|-----------|
| Cases | `GET /api/cases` (list + filters), `GET /api/cases/<id>` (detail) |
| Orders | `GET /api/cases/<id>/orders` (paginated daily orders) |
| Judgments | `GET /api/cases/<id>/judgment` (PDF status) |
| Commissions | `GET /api/commissions` (full list) |
| Stats | `GET /api/stats` (aggregate counts + monthly series) |
| Health | `GET /health` (DB liveness + last ingestion run) |
| Batch Status | `GET /api/batch/status` (ingestion pipeline status for debugging) |

**Technology stack:**

- Python 3.11+, `Flask 3.x` as the web framework
- `SQLAlchemy 2.x` ORM for database access (shared models with ingestion service)
- `Flask-Caching 2.x` for Redis-backed response caching
- `Flask-Limiter 3.x` for per-IP rate limiting
- `marshmallow 3.x` for response schema documentation
- `structlog` for structured JSON logging
- `gunicorn` as the production WSGI server
- PostgreSQL 15 as the database (optionally with a read replica)

**Design principle — read-only:**

Every query in `db/queries.py` uses `get_session(read_only=True)`, which routes to a replica when one is configured. There are no `POST`, `PUT`, or `DELETE` endpoints. All writes go through the ingestion service.

---

## 2. Repository Layout

```
e-jagriti/
├── api/                        # ← This service lives here
│   ├── app.py                  # Application factory: create_app()
│   │                           # Registers extensions, blueprints, error handlers
│   ├── config.py               # Config and TestingConfig classes loaded from env
│   ├── middleware.py           # Request ID injection + structured request logging
│   ├── models.py               # Shim: adds ingestion/ to sys.path, re-exports ORM models
│   │                           # Both services share the same model definitions
│   ├── requirements.txt        # Python dependencies
│   ├── Dockerfile              # Production image (gunicorn entry point)
│   │
│   ├── db/
│   │   ├── session.py          # Engine factory, get_session(), check_db_connection()
│   │   │                       # Handles primary + optional read replica routing
│   │   └── queries.py          # All SQLAlchemy query functions (no logic in routes)
│   │
│   ├── routes/                 # One module per resource group (Flask Blueprint per file)
│   │   ├── __init__.py
│   │   ├── cases.py            # GET /api/cases, GET /api/cases/<id>
│   │   ├── orders.py           # GET /api/cases/<id>/orders
│   │   ├── judgments.py        # GET /api/cases/<id>/judgment
│   │   ├── commissions.py      # GET /api/commissions
│   │   ├── stats.py            # GET /api/stats, GET /health
│   │   └── batch.py            # GET /api/batch/status (developer debugging)
│   │
│   └── schemas/
│       ├── __init__.py
│       └── responses.py        # success_response() / error_response() helpers
│                               # + Marshmallow schemas for documentation
│
├── ingestion/
│   └── db/
│       └── models.py           # ← Canonical ORM models (api/models.py imports from here)
│
└── guide_api.md                # This file
```

**Key relationships between files:**

```
app.py (create_app)
  └─ loads .env (python-dotenv)
  └─ reads config from config.py (get_config → Config or TestingConfig)
  └─ initialises Cache and Limiter extensions
  └─ calls register_middleware(app) from middleware.py
  └─ registers 6 Blueprints from routes/

routes/*.py
  └─ each route calls one or more query functions from db/queries.py
  └─ wraps results with success_response() / error_response() from schemas/responses.py

db/queries.py
  └─ imports ORM models from models.py (the shim)
  └─ calls get_session(read_only=True) from db/session.py

models.py (shim)
  └─ appends ../ingestion to sys.path
  └─ imports all ORM classes from ingestion/db/models.py
  └─ re-exports them so routes can do: from models import Case
```

**Why the shim?**

The canonical model definitions live in `ingestion/db/models.py`. Duplicating them in the API would cause schema drift. The shim (`api/models.py`) resolves the path at import time, so both services always use the same table definitions. In Docker this works because the Dockerfile COPYs the ingestion directory alongside the api directory.

---

## 3. Response Envelope

Every endpoint returns one of two JSON shapes. Routes never return raw dicts — they always call `success_response()` or `error_response()` from `schemas/responses.py`.

---

### 3.1 Success — non-paginated

Used when returning a single object or a non-paginated list (e.g. `/api/commissions`, `/api/cases/<id>`).

```json
{
  "success": true,
  "data": {
    "case_id": 42,
    "case_number": "DC/77/CC/104/2025",
    "status": "open"
  }
}
```

Python call:

```python
return success_response(data)          # status defaults to 200
return success_response(data, status=201)
```

---

### 3.2 Success — paginated

Used when returning a list with pagination metadata (e.g. `GET /api/cases`, `GET /api/cases/<id>/orders`). Pass `page`, `per_page`, and `total` to `success_response()` and the `meta.pagination` block is added automatically.

```json
{
  "success": true,
  "data": [
    {
      "case_id": 1,
      "case_number": "DC/77/CC/104/2025",
      "complainant_name": "Ramesh Kumar",
      "commission_name": "District Consumer Commission Delhi-77",
      "commission_type": "district",
      "filing_date": "2025-03-14",
      "date_of_next_hearing": "2025-08-20",
      "status": "open",
      "case_stage": "Arguments",
      "last_updated": "2025-07-10T04:32:11+00:00"
    }
  ],
  "meta": {
    "pagination": {
      "page": 1,
      "per_page": 20,
      "total": 347,
      "total_pages": 18
    }
  }
}
```

Python call:

```python
return success_response(
    data=result["items"],
    page=page,
    per_page=per_page,
    total=result["total"],
)
```

---

### 3.3 Error

All errors use the same envelope regardless of status code. The `code` field is a machine-readable string; `message` is human-readable.

```json
{
  "success": false,
  "error": {
    "code": "NOT_FOUND",
    "message": "Case 9999 not found"
  }
}
```

Common error codes used across routes:

| Code | HTTP status | When |
|------|-------------|------|
| `NOT_FOUND` | 404 | Resource does not exist |
| `INVALID_PARAMS` | 400 | `page`, `per_page`, or `runs` is not an integer |
| `INVALID_STATUS` | 400 | `status` query param is not a valid enum value |
| `INVALID_COMMISSION_TYPE` | 400 | `commission_type` not in `national/state/district` |
| `INVALID_DATE` | 400 | Date param not in `YYYY-MM-DD` format |
| `METHOD_NOT_ALLOWED` | 405 | Wrong HTTP method |
| `RATE_LIMIT_EXCEEDED` | 429 | Client hit the rate limit |
| `INTERNAL_ERROR` | 500 | Unhandled exception (logged server-side) |

Python call:

```python
return error_response("NOT_FOUND", f"Case {case_id} not found", 404)
```

---

## 4. Middleware

Middleware is registered in `middleware.py` via `register_middleware(app)`, called inside `create_app()` before blueprints are loaded. It attaches two `before_request` / `after_request` hooks.

---

### 4.1 Request ID

Every request is assigned a UUID stored on Flask's `g` object and echoed back in the `X-Request-ID` response header.

**How it works:**

1. `before_request`: reads `X-Request-ID` from the incoming headers. If present, reuses it (lets clients correlate their own IDs). If absent, generates a new `uuid.uuid4()`.
2. `after_request`: writes the ID into `response.headers["X-Request-ID"]`.

**Why it matters:** The request ID is not yet bound to the structlog context, but it is visible in the response headers. When debugging a specific failed request you can pass your own `X-Request-ID` header and trace it through server logs by grepping for the UUID.

---

### 4.2 Structured Request Logging

After every request, middleware emits a single structured JSON log line via structlog:

```json
{
  "event": "http_request",
  "method": "GET",
  "path": "/api/cases",
  "status": 200,
  "duration_ms": 14,
  "request_id": "3f7a2d1c-...",
  "remote_addr": "10.0.0.5",
  "level": "info",
  "timestamp": "2025-04-01T09:12:33.441Z"
}
```

`duration_ms` is measured with `time.monotonic()` from the `before_request` hook — it covers full request processing including DB query time. All logs go to stdout (captured by your container orchestrator).

---

## 5. Configuration & Environment Variables

Configuration is loaded entirely from environment variables via `config.py`. There are no hardcoded secrets. The `get_config()` function selects the right class based on `FLASK_ENV`:

| `FLASK_ENV` value | Config class used |
|------------------|-------------------|
| `production` (default) | `Config` |
| `testing` | `TestingConfig` (disables Redis + rate limiting) |

Set variables in a `.env` file at `api/.env` (loaded by `python-dotenv` inside `create_app()`), or inject them via Docker / Cloud Run / ECS environment.

---

### Variable Reference

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `DATABASE_URL` | *(none)* | **Yes** | PostgreSQL connection string. e.g. `postgresql://user:pass@localhost:5432/ejagriti`. Raises `RuntimeError` at startup if missing. |
| `REPLICA_DATABASE_URL` | `None` | No | If set, all `read_only=True` sessions are routed here instead of the primary. Useful for separating read traffic. When unset, both read and write sessions use `DATABASE_URL`. |
| `SECRET_KEY` | `dev-secret-change-in-prod` | **Yes in prod** | Flask secret key used for session signing. Override with a random string in production. |
| `FLASK_ENV` | `production` | No | Controls which Config class is loaded. Set to `testing` in CI. |
| `DEBUG` | `false` | No | Enables Flask debug mode (auto-reload, detailed tracebacks). Never set `true` in production. |
| `REDIS_URL` | `redis://localhost:6379/0` | No | Redis connection URL. Used by both Flask-Caching and Flask-Limiter. If not set, caching falls back to `SimpleCache` (in-process) and rate limiting to `memory://` (per-process, not shared). |
| `EJAGRITI_CACHE_TTL_SECONDS` | `3600` | No | Default TTL for cached responses in seconds. Applies to `/api/commissions` and `/api/stats`. Set lower in development if you need fresher data. |
| `EJAGRITI_RATE_LIMIT_PER_MINUTE` | `100` | No | Max requests per minute per IP address. Flask-Limiter enforces this globally. Individual routes can override it with a `@limiter.limit(...)` decorator. |
| `EJAGRITI_LOG_LEVEL` | `INFO` | No | Structlog/stdlib log level. Accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Set `DEBUG` locally to see all SQL queries and session events. |
| `EJAGRITI_SA_POOL_SIZE` | `5` | No | SQLAlchemy connection pool size (persistent connections kept open). Increase for high-concurrency deployments. |
| `EJAGRITI_SA_MAX_OVERFLOW` | `10` | No | Extra connections allowed above `SA_POOL_SIZE` when the pool is saturated. Total max connections = `SA_POOL_SIZE + SA_MAX_OVERFLOW`. |
| `TEST_DATABASE_URL` | falls back to `DATABASE_URL` | No | Used by `TestingConfig` so tests can target a separate database without overwriting `DATABASE_URL`. |

---

### Minimal `.env` for local development

```dotenv
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ejagriti
SECRET_KEY=local-dev-secret
FLASK_ENV=development
DEBUG=true
EJAGRITI_LOG_LEVEL=DEBUG
# REDIS_URL=redis://localhost:6379/0   # uncomment if Redis is running locally
# REPLICA_DATABASE_URL=               # leave unset to use primary for reads
```

---

### How `REDIS_URL` affects caching and rate limiting

Flask-Caching and Flask-Limiter both read `REDIS_URL` at startup:

- **With Redis:** Cache is shared across all gunicorn workers and container replicas. Rate limit counters are also shared (prevents a single IP from bypassing limits by hitting different workers).
- **Without Redis:** `CACHE_TYPE` falls back to `SimpleCache` (per-process, in-memory). Rate limit storage falls back to `memory://` (also per-process). This is fine for local development or single-worker deployments, but in a multi-worker / multi-replica setup each process has its own counter — the effective limit multiplies by the number of processes.

---

### Connection pool sizing guide

The total number of Postgres connections the API can hold open is:

```
max_connections = (SA_POOL_SIZE + SA_MAX_OVERFLOW) × gunicorn_workers
```

With defaults (5 + 10 = 15) and 4 gunicorn workers that's 60 connections. Postgres's default `max_connections` is 100, so with both the API and ingestion service running you should either raise Postgres's limit or use PgBouncer in transaction mode.

---

## 6. Endpoints Reference

All endpoints are prefixed with no version segment (e.g. `/api/cases`, not `/v1/api/cases`). All responses use the envelope described in section 3.

---

### 6.1 `GET /api/cases` — List cases

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Purpose:** Paginated, filterable list of all Samsung cases. This is the primary data source for the homepage table.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | `1` | 1-based page number |
| `per_page` | integer | `20` | Rows per page. Max `100`. |
| `status` | string | *(all)* | Filter by case status: `open`, `closed`, `pending`, or `all` |
| `commission_type` | string | *(all)* | Filter by commission level: `national`, `state`, or `district` |
| `search` | string | *(none)* | Case-insensitive substring match on `case_number` OR `complainant_name` |

**Example request:**

```bash
curl "http://localhost:8000/api/cases?status=open&commission_type=district&search=samsung&page=1&per_page=5"
```

**Example response:**

```json
{
  "success": true,
  "data": [
    {
      "case_id": 42,
      "case_number": "DC/77/CC/104/2025",
      "complainant_name": "Ramesh Kumar",
      "commission_name": "District Consumer Commission Delhi-77",
      "commission_type": "district",
      "filing_date": "2025-03-14",
      "date_of_next_hearing": "2025-08-20",
      "status": "open",
      "case_stage": "Arguments",
      "last_updated": "2025-07-10T04:32:11+00:00"
    }
  ],
  "meta": {
    "pagination": {
      "page": 1,
      "per_page": 5,
      "total": 347,
      "total_pages": 70
    }
  }
}
```

**Notes:**
- Results are ordered by `filing_date DESC NULLS LAST`.
- `status=all` is equivalent to omitting the `status` parameter — both return cases of every status.
- `search` runs `ILIKE '%term%'` on two columns; it is not a full-text index. Avoid very short search strings on large datasets.

---

### 6.2 `GET /api/cases/alerts` — Alert cases for notifications

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Purpose:** Returns open/pending cases that need attention, grouped into two named alert sections. Designed for notification feeds and operator dashboards.

**Auth:** `cases:read`
**Caching:** Not cached — always returns live data.
**Pagination:** None — returns all matching cases.

**Example request:**

```bash
curl http://localhost:8000/api/cases/alerts
```

**Response shape:**

```json
{
  "success": true,
  "data": {
    "no_voc": {
      "count": 42,
      "items": [
        {
          "case_id": 101,
          "case_number": "DC/77/CC/104/2025",
          "complainant_name": "Rajesh Kumar",
          "commission_name": "District Consumer Disputes Redressal Commission, Agra",
          "commission_type": "district",
          "date_of_next_hearing": "2025-07-10",
          "status": "open",
          "case_stage": "ADMITTED"
        }
      ]
    },
    "hearing_soon": {
      "count": 3,
      "items": [
        {
          "case_id": 205,
          "case_number": "SC/1/CC/22/2024",
          "complainant_name": "Sunita Sharma",
          "commission_name": "Delhi State Consumer Disputes Redressal Commission",
          "commission_type": "state",
          "date_of_next_hearing": "2025-04-05",
          "status": "open",
          "case_stage": "Arguments"
        }
      ]
    }
  }
}
```

**Alert conditions:**

| Section | Condition | Query |
|---------|-----------|-------|
| `no_voc` | Case has no linked VOC complaint | `cases.voc_number IS NULL` |
| `hearing_soon` | Next hearing within 2 days (today through today+2, inclusive) | `date_of_next_hearing BETWEEN today AND today+2` |

**Notes:**
- Both sections only include `status = open` or `status = pending` cases. Closed cases are excluded.
- `no_voc` items are ordered by `filing_date DESC`. `hearing_soon` items are ordered by `date_of_next_hearing ASC` (most imminent first).
- A case can appear in both sections simultaneously if it has no VOC and also has an imminent hearing.
- `no_voc` uses the partial index `idx_cases_no_voc` — no join against `voc_complaints` is performed. `cases.voc_number` is denormalised from `voc_complaints` and kept in sync by the `fetch_voc` ingestion job.

---

### 6.3 `GET /api/cases/<case_id>` — Case detail

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Purpose:** Full nested case object including commission, all hearings in sequence order, and all daily order PDF records.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `case_id` | integer | Internal surrogate PK from the `cases` table |

**Example request:**

```bash
curl "http://localhost:8000/api/cases/42"
```

**Example response:**

```json
{
  "success": true,
  "data": {
    "case_id": 42,
    "case_number": "DC/77/CC/104/2025",
    "filing_date": "2025-03-14",
    "date_of_cause": "2025-01-10",
    "status": "open",
    "case_stage": "Arguments",
    "case_category": "Goods",
    "date_of_next_hearing": "2025-08-20",
    "commission": {
      "id": 7,
      "ext_id": 1023,
      "name": "District Consumer Commission Delhi-77",
      "type": "district",
      "state_id": 7
    },
    "complainant": {
      "name": "Ramesh Kumar",
      "advocate_names": ["Adv. Priya Sharma"]
    },
    "respondent": {
      "name": "Samsung India Electronics Pvt. Ltd.",
      "advocate_names": ["Adv. Rohit Mehra", "Adv. Anjali Singh"]
    },
    "hearings": [
      {
        "id": 301,
        "court_room_hearing_id": "CRH-9921",
        "date": "2025-04-10",
        "next_date": "2025-05-15",
        "case_stage": "Admission",
        "proceeding_text": "<p>Case admitted...</p>",
        "sequence_number": 1,
        "daily_order_available": true
      },
      {
        "id": 302,
        "court_room_hearing_id": "CRH-9922",
        "date": "2025-05-15",
        "next_date": "2025-08-20",
        "case_stage": "Arguments",
        "proceeding_text": null,
        "sequence_number": 2,
        "daily_order_available": false
      }
    ],
    "daily_orders": [
      {
        "id": 88,
        "date": "2025-04-10",
        "order_type_id": 1,
        "pdf_fetched": true,
        "pdf_storage_path": "s3://ejagriti-pdfs/orders/42/2025-04-10.pdf",
        "pdf_fetched_at": "2025-04-11T02:15:00+00:00"
      }
    ],
    "last_fetched_at": "2025-07-10T04:32:11+00:00"
  }
}
```

**Notes:**
- `hearings` are sorted by `hearing_sequence_number` ascending (chronological order).
- `daily_orders` are sorted by `date_of_hearing` ascending.
- `daily_order_available` is `true` when `daily_order_availability_status = 2` on the hearing row.
- `proceeding_text` is sanitized HTML — dangerous tags (`script`, `iframe`, `style`, etc.) and all attributes are stripped by the ingestion service using an `nh3` allowlist. Safe formatting tags (`p`, `b`, `br`, `table`, etc.) are preserved. You can render it directly in the frontend; sandboxing is still good practice for defence-in-depth.
- Returns `404 NOT_FOUND` if the `case_id` does not exist.

---

### 6.4 `POST /api/cases/<case_id>/voc` — Attach a VOC complaint to a case

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Purpose:** Manually link a VOC (Voice of Customer) complaint number to a case. Validates the VOC number against the complaint management system (CMS) and upserts the linkage in the DB.

**Auth:** `cases:write`
**Caching:** Not cached.

**Request body (JSON):**
```json
{ "voc_number": 310256328 }
```

**Example request:**
```bash
curl -X POST http://localhost:8000/api/cases/101/voc \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"voc_number": 310256328}'
```

**Example response (201):**
```json
{
  "success": true,
  "data": {
    "case_id": 101,
    "voc_number": 310256328
  }
}
```

**Error responses:**

| Code | Error code | Condition |
|------|-----------|-----------|
| 400 | `INVALID_PARAMS` | `voc_number` missing or not an integer |
| 404 | `NOT_FOUND` | `case_id` does not exist |
| 404 | `VOC_NOT_FOUND` | `voc_number` not found in the CMS |
| 409 | `VOC_CONFLICT` | `voc_number` is already linked to a different case |
| 502 | `CMS_UNAVAILABLE` | Could not reach the complaint management system |

**Notes:**
- The caller's SSO bearer token is forwarded to the CMS transparently — no separate CMS credential is needed.
- On success, both `voc_complaints` (upserted, `match_status=matched`) and `cases.voc_number` are updated atomically, keeping the no-VOC alert index accurate.
- The CMS endpoint path is configured in `api/cms_client.py` (`_VOC_PATH`). Update it when the real CMS route is confirmed.
- Set `EJAGRITI_CMS_BASE_URL` in the environment to point to the complaint management system.

---

### 6.5 `GET /api/cases/<case_id>/orders` — Daily orders for a case

**Blueprint:** `orders_bp` (`routes/orders.py`)
**Purpose:** Paginated list of daily order PDF records for a single case, optionally filtered by date range.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `case_id` | integer | Internal surrogate PK |

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | `1` | 1-based page number |
| `per_page` | integer | `20` | Max `100` |
| `from_date` | string | *(none)* | Include orders on or after this date (`YYYY-MM-DD`) |
| `to_date` | string | *(none)* | Include orders on or before this date (`YYYY-MM-DD`) |

**Example request:**

```bash
curl "http://localhost:8000/api/cases/42/orders?from_date=2025-01-01&to_date=2025-06-30"
```

**Example response:**

```json
{
  "success": true,
  "data": [
    {
      "id": 88,
      "date": "2025-04-10",
      "order_type_id": 1,
      "pdf_fetched": true,
      "pdf_storage_path": "s3://ejagriti-pdfs/orders/42/2025-04-10.pdf",
      "pdf_fetched_at": "2025-04-11T02:15:00+00:00",
      "pdf_fetch_error": null
    }
  ],
  "meta": {
    "pagination": {
      "page": 1,
      "per_page": 20,
      "total": 1,
      "total_pages": 1
    }
  }
}
```

**Notes:**
- `order_type_id`: `1` = daily order, `2` = judgment (final order).
- `pdf_fetched: false` means the PDF has been queued but not yet downloaded. `pdf_fetch_error` will contain the last error message if the fetch failed.
- Results ordered by `date_of_hearing DESC`.
- Returns `404 NOT_FOUND` if the `case_id` does not exist.

---

### 6.6 `GET /api/cases/<case_id>/judgment` — Judgment for a case

**Blueprint:** `judgments_bp` (`routes/judgments.py`)
**Purpose:** Returns the final judgment order (`order_type_id = 2`) for a case. Closed cases should have exactly one.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `case_id` | integer | Internal surrogate PK |

**Example request:**

```bash
curl "http://localhost:8000/api/cases/42/judgment"
```

**Response — judgment exists:**

```json
{
  "success": true,
  "data": {
    "id": 91,
    "date": "2025-06-30",
    "pdf_fetched": true,
    "pdf_storage_path": "s3://ejagriti-pdfs/judgments/42/2025-06-30.pdf",
    "pdf_fetched_at": "2025-07-01T03:00:12+00:00"
  }
}
```

**Response — case exists but no judgment yet:**

```json
{
  "success": true,
  "data": {}
}
```

**Response — case does not exist:**

```json
{
  "success": false,
  "error": {
    "code": "NOT_FOUND",
    "message": "Case 9999 not found"
  }
}
```

**Notes:**
- An empty `data: {}` with `success: true` is intentional — it signals the case is known but the judgment PDF has not been fetched yet. This lets the frontend distinguish "case not found" from "no judgment available".
- If multiple judgment rows exist (edge case), the most recent by `date_of_hearing` is returned.

---

### 6.7 `GET /api/commissions` — List all commissions

**Blueprint:** `commissions_bp` (`routes/commissions.py`)
**Purpose:** Returns the full list of all ~700+ consumer commissions in India (national, state, district). Cached for 1 hour.

**No parameters.**

**Example request:**

```bash
curl "http://localhost:8000/api/commissions"
```

**Example response:**

```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "commission_id_ext": 1,
      "name": "National Consumer Disputes Redressal Commission",
      "type": "national",
      "state_id": null,
      "district_id": null,
      "case_prefix_text": "CC/",
      "parent_commission_id": null
    },
    {
      "id": 7,
      "commission_id_ext": 1023,
      "name": "District Consumer Commission Delhi-77",
      "type": "district",
      "state_id": 7,
      "district_id": 77,
      "case_prefix_text": "DC/77/",
      "parent_commission_id": 3
    }
  ]
}
```

**Notes:**
- Results ordered by `commission_type, name_en` (national first, then state, then district, each alphabetically).
- Response is cached under key `"commissions_list"` with TTL `EJAGRITI_CACHE_TTL_SECONDS` (default 1 h). The cached list is populated on the first request after startup or cache expiry.
- `parent_commission_id` links district → state; it is the internal surrogate `id`, not `commission_id_ext`.

---

### 6.8 `GET /api/stats` — Aggregate statistics

**Blueprint:** `stats_bp` (`routes/stats.py`)
**Purpose:** Dashboard-level aggregate counts and a 12-month filing time series. Cached for 1 hour.

**No parameters.**

**Example request:**

```bash
curl "http://localhost:8000/api/stats"
```

**Example response:**

```json
{
  "success": true,
  "data": {
    "total_cases": 1842,
    "open_cases": 1103,
    "closed_cases": 621,
    "pending_cases": 118,
    "by_commission_type": {
      "national": 14,
      "state": 203,
      "district": 1625
    },
    "cases_per_month": [
      { "month": "2024-04", "count": 87 },
      { "month": "2024-05", "count": 94 },
      { "month": "2025-03", "count": 112 }
    ],
    "last_ingestion_run": {
      "run_id": 58,
      "started_at": "2025-04-01T01:00:02+00:00",
      "finished_at": "2025-04-01T01:47:33+00:00",
      "total_calls": 2841,
      "success_count": 2798,
      "fail_count": 43,
      "duration_seconds": 2851.2
    }
  }
}
```

**Notes:**
- `cases_per_month` covers only the last 12 calendar months and only months where at least one case was filed. Months with zero filings are absent (not zero-filled) — account for this when rendering charts.
- `last_ingestion_run` reflects only the single most recent `ingestion_runs` row. For full run history use `GET /api/batch/status`.
- Cached under key `"stats"` with TTL `EJAGRITI_CACHE_TTL_SECONDS`.

---

### 6.9 `GET /health` — Health check

**Blueprint:** `stats_bp` (`routes/stats.py`)
**Purpose:** Liveness probe. Returns DB connectivity status and a summary of the last ingestion run. Designed for load balancer health checks.

**No parameters.**

**Example request:**

```bash
curl "http://localhost:8000/health"
```

**Response — healthy (HTTP 200):**

```json
{
  "success": true,
  "data": {
    "db_ok": true,
    "last_ingestion_run": {
      "run_id": 58,
      "started_at": "2025-04-01T01:00:02+00:00",
      "finished_at": "2025-04-01T01:47:33+00:00",
      "total_calls": 2841,
      "success_count": 2798,
      "fail_count": 43,
      "trigger_mode": "scheduler",
      "duration_seconds": 2851.2
    }
  }
}
```

**Response — DB unreachable (HTTP 503):**

```json
{
  "success": false,
  "data": {
    "db_ok": false,
    "last_ingestion_run": null
  }
}
```

**Notes:**
- This endpoint is **not cached** — it always runs a live `SELECT 1` against the primary database.
- The HTTP status code is `200` when `db_ok: true`, `503` when `db_ok: false`. Load balancers should check the status code, not the body.
- `last_ingestion_run` will be `null` on a brand-new deployment before the first ingestion run completes.

---

### 6.10 `GET /api/batch/status` — Ingestion pipeline status

**Blueprint:** `batch_bp` (`routes/batch.py`)
**Purpose:** Live snapshot of the ingestion pipeline state. Shows recent run history, current queue depths (work still to do), and the most recent error log entries. Designed for developer debugging and operator dashboards; can be wired into a frontend monitoring panel.

**This endpoint is not cached — it always queries live data.**

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `runs` | integer | `10` | Number of most recent ingestion runs to include. Max `50`. |

**Example request:**

```bash
curl "http://localhost:8000/api/batch/status?runs=3"
```

**Example response:**

```json
{
  "success": true,
  "data": {
    "recent_runs": [
      {
        "run_id": 61,
        "started_at": "2025-04-01T06:00:01+00:00",
        "finished_at": null,
        "status": "running",
        "trigger_mode": "scheduler",
        "total_calls": 0,
        "success_count": 0,
        "fail_count": 0,
        "skip_count": 0,
        "duration_seconds": null,
        "notes": null
      },
      {
        "run_id": 60,
        "started_at": "2025-04-01T01:00:02+00:00",
        "finished_at": "2025-04-01T01:47:33+00:00",
        "status": "failed",
        "trigger_mode": "scheduler",
        "total_calls": 2841,
        "success_count": 2798,
        "fail_count": 43,
        "skip_count": 210,
        "duration_seconds": 2851.2,
        "notes": null
      },
      {
        "run_id": 59,
        "started_at": "2025-03-31T01:00:01+00:00",
        "finished_at": "2025-03-31T01:38:44+00:00",
        "status": "completed",
        "trigger_mode": "scheduler",
        "total_calls": 2700,
        "success_count": 2700,
        "fail_count": 0,
        "skip_count": 185,
        "duration_seconds": 2323.0,
        "notes": null
      }
    ],
    "queue_depths": {
      "cases_pending_detail_fetch": 14,
      "pdfs_pending_fetch": 37,
      "failed_jobs_unresolved": 2
    },
    "recent_errors": [
      {
        "id": 512,
        "run_id": 60,
        "case_id": 1034,
        "endpoint": "/courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf",
        "http_status": 500,
        "error_type": "HTTP_ERROR",
        "error_message": "Server returned 500 for case 1034 date 2025-03-28",
        "retry_count": 5,
        "created_at": "2025-04-01T01:44:12+00:00"
      }
    ]
  }
}
```

**`status` field derivation on each run:**

| Value | Condition |
|-------|-----------|
| `"running"` | `finished_at` is `null` — the run has not closed its audit row yet |
| `"failed"` | `finished_at` is set AND `fail_count > 0` |
| `"completed"` | `finished_at` is set AND `fail_count == 0` |

**`queue_depths` fields:**

| Field | What it counts |
|-------|---------------|
| `cases_pending_detail_fetch` | `cases` rows where `last_fetched_at IS NULL` — cases scraped from the list API but not yet enriched with full detail |
| `pdfs_pending_fetch` | `daily_orders` rows where `pdf_fetched = false` — PDFs queued but not yet downloaded |
| `failed_jobs_unresolved` | `failed_jobs` rows where `resolved = false` — jobs that exhausted all retries and are awaiting manual review or next sweep |

**`recent_errors`** always returns the 20 most recent rows from `ingestion_errors`, regardless of the `runs` parameter.

---

## 7. Caching

---

### 7.1 Which endpoints cache

| Endpoint | Cache key | TTL | Notes |
|----------|-----------|-----|-------|
| `GET /api/commissions` | `"commissions_list"` | `EJAGRITI_CACHE_TTL_SECONDS` | Full list (~700 rows). Populated on first hit after startup or expiry. |
| `GET /api/stats` | `"stats"` | `EJAGRITI_CACHE_TTL_SECONDS` | Aggregate counts + monthly series. Stale counts are acceptable for a dashboard. |
| All other endpoints | — | — | Not cached. DB query on every request. |

`/health` and `/api/batch/status` are explicitly **not cached** because they are diagnostic endpoints that must reflect live state.

---

### 7.2 Cache backend

The backend is chosen at startup based on whether `REDIS_URL` is set:

```
REDIS_URL set     →  CACHE_TYPE = "RedisCache"   (shared across workers + replicas)
REDIS_URL absent  →  CACHE_TYPE = "SimpleCache"  (in-process dictionary, per-worker)
```

In `docker-compose.yml` the `api` service always sets `REDIS_URL=redis://redis:6379/0`, so Redis is the backend in all compose-based environments.

---

### 7.3 How to invalidate the cache manually

There is no cache-bust endpoint. To force fresh data before the TTL expires:

**Redis (production / docker-compose):**

```bash
redis-cli -u redis://localhost:6379/0 DEL commissions_list stats
```

**SimpleCache (local dev without Redis):**

Restart the Flask process — SimpleCache is in-process and does not survive restarts.

---

### 7.4 Cache usage pattern in routes

The two cached routes follow the same manual read-through pattern (Flask-Caching's `@cache.cached` decorator is not used because the `success_response()` wrapper makes the cache key ambiguous):

```python
cached = cache.get("stats")
if cached is not None:
    return success_response(cached)

data = get_stats()
cache.set("stats", data)       # uses CACHE_DEFAULT_TIMEOUT
return success_response(data)
```

The cache stores the raw `data` dict, not the full HTTP response. This means the `success` wrapper and HTTP headers are always freshly generated even on a cache hit.

---

## 8. Rate Limiting

---

### 8.1 Default limit

All endpoints share a single global rate limit set by:

```
EJAGRITI_RATE_LIMIT_PER_MINUTE  (default: 100)
```

Flask-Limiter translates this into `"100 per minute"` and applies it to every route automatically via the `Limiter` singleton initialised in `app.py`.

---

### 8.2 How limits are keyed

The limiter uses `get_remote_address` as its key function — the client's IP address from `request.remote_addr`. Each IP gets its own independent counter.

**Behind a reverse proxy (nginx, AWS ALB, Cloud Run):** `request.remote_addr` will be the proxy IP, not the real client IP. Fix this by setting Flask's `TRUSTED_PROXIES` or using `ProxyFix` middleware, otherwise all clients share a single counter.

---

### 8.3 Rate limit exceeded response

When the limit is hit Flask-Limiter triggers the `429` error handler registered in `app.py`, which returns the standard error envelope:

```json
{
  "success": false,
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Too many requests. Try again later."
  }
}
```

HTTP status: `429`.

---

### 8.4 Storage backend

Rate limit counters use the same `REDIS_URL` as the cache:

```
REDIS_URL set     →  RATELIMIT_STORAGE_URI = REDIS_URL    (shared across workers)
REDIS_URL absent  →  RATELIMIT_STORAGE_URI = "memory://"  (per-process only)
```

In development without Redis the limit is per-worker, not per-IP across the whole process group. This is acceptable locally but must not reach production.

---

## 9. Adding a New Endpoint

Follow these four steps every time. The pattern is consistent across all existing routes.

---

### Step 1 — Write the query function in `db/queries.py`

All database logic lives here. Routes must not contain SQL or ORM calls.

```python
# db/queries.py

def get_cases_by_stage(stage: str, page: int = 1, per_page: int = 20) -> dict[str, Any]:
    """
    Return paginated cases filtered by case_stage_name.

    Args:
        stage: Stage string to match (case-insensitive).
        page: 1-based page number.
        per_page: Rows per page.

    Returns:
        Dict with ``items`` (list of case dicts) and ``total`` row count.
    """
    with get_session(read_only=True) as session:
        q = select(Case).where(Case.case_stage_name.ilike(f"%{stage}%"))

        total = session.execute(
            select(func.count()).select_from(q.subquery())
        ).scalar_one()

        rows = session.execute(
            q.order_by(Case.filing_date.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).scalars().all()

    items = [{"case_id": r.id, "case_number": r.case_number, "stage": r.case_stage_name}
             for r in rows]
    return {"items": items, "total": total}
```

**Rules:**
- Always use `get_session(read_only=True)` for SELECT queries.
- Return plain Python dicts/lists — never ORM objects. Routes and tests should not need to touch SQLAlchemy.
- Add a docstring with Args and Returns.

---

### Step 2 — Create `routes/foo.py`

One Blueprint per resource group. Keep route handlers thin — validate inputs, call the query function, wrap the result.

```python
# routes/stages.py

from __future__ import annotations
from flask import Blueprint, request
from db.queries import get_cases_by_stage
from schemas.responses import error_response, success_response

stages_bp = Blueprint("stages", __name__, url_prefix="/api/cases")


@stages_bp.get("/by-stage")
def list_cases_by_stage():
    """
    Return cases filtered by stage name.

    Query params:
      - stage (str, required): Stage name substring to match.
      - page (int, default 1)
      - per_page (int, default 20, max 100)
    """
    stage = request.args.get("stage", "").strip()
    if not stage:
        return error_response("INVALID_PARAMS", "stage is required", 400)

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        return error_response("INVALID_PARAMS", "page and per_page must be integers", 400)

    result = get_cases_by_stage(stage=stage, page=page, per_page=per_page)
    return success_response(
        data=result["items"],
        page=page,
        per_page=per_page,
        total=result["total"],
    )
```

---

### Step 3 — Register the Blueprint in `app.py`

Add the import and `register_blueprint` call inside `create_app()` alongside the existing blueprints:

```python
# app.py — inside create_app(), in the Blueprints section

from routes.stages import stages_bp   # add this line

app.register_blueprint(stages_bp)     # add this line
```

The order of `register_blueprint` calls does not matter for correctness, but keep them alphabetical for readability.

---

### Step 4 — Add a Marshmallow schema (optional but recommended)

Schemas in `schemas/responses.py` serve as living documentation and can be used for outbound validation in tests. Add a schema that mirrors the dict shape returned by your query function:

```python
# schemas/responses.py

class CaseByStageSchema(Schema):
    """Lightweight case item for the by-stage endpoint."""
    case_id    = fields.Int()
    case_number = fields.Str()
    stage      = fields.Str(allow_none=True)
```

Schemas are not automatically applied to responses — they are used explicitly in tests or for documentation:

```python
# In a test
from schemas.responses import CaseByStageSchema
errors = CaseByStageSchema(many=True).validate(response.json["data"])
assert errors == {}
```

---

## 10. Running Locally

---

### 10.1 Option A — Docker Compose (recommended)

This is the closest to production and requires no manual Python environment setup. It starts Postgres, Redis, runs migrations, starts the ingestion service, and starts the API — all in one command.

```bash
# From the repo root
cp .env.example .env          # fill in any overrides (defaults work for local dev)
docker compose up --build
```

The API will be available at `http://localhost:8000`.

**Useful compose commands:**

```bash
# Rebuild only the API image after code changes
docker compose up --build api

# Tail API logs only
docker compose logs -f api

# Run migrations without starting other services
docker compose run --rm migrations

# Open a psql shell against the compose Postgres
docker compose exec postgres psql -U ejagriti ejagriti
```

**How the models shim works in Docker:**

The `api/Dockerfile` COPYs only the `api/` directory into `/app`. But `api/models.py` needs `ingestion/db/models.py`. This works because the ingestion Dockerfile (used for the `migrations` service) copies the ingestion package, and the API container relies on `models.py` appending `../ingestion` to `sys.path` at runtime. In the container `../ingestion` resolves to `/ingestion` — make sure the `api/Dockerfile` or the compose volume mounts make that path available if you customise the image.

---

### 10.2 Option B — Flask dev server (no Docker)

Use this when you want fast iteration with auto-reload, without rebuilding images.

**Prerequisites:** Postgres running locally (or the compose Postgres with `docker compose up postgres redis`).

```bash
# 1. Create and activate a virtual environment
cd api
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install ingestion dependencies (needed for the models shim)
pip install -r ../ingestion/requirements.txt

# 4. Set environment variables
cp ../.env.example .env
# Edit .env — set DATABASE_URL at minimum

# 5. Run migrations (from ingestion/ directory)
cd ../ingestion
alembic -c alembic.ini upgrade head
cd ../api

# 6. Start the dev server
FLASK_ENV=development flask --app app:create_app run --reload --port 8000
```

**PYTHONPATH for the models shim:**

The shim in `api/models.py` appends `../ingestion` to `sys.path` dynamically, so no manual `PYTHONPATH` export is needed. If you see `ModuleNotFoundError: No module named 'db.models'` it means the relative path resolution failed — verify you are running Flask from within the `api/` directory, not the repo root.

---

### 10.3 Option C — gunicorn (production-like, no Docker)

```bash
cd api
gunicorn "app:create_app()" \
  --bind 0.0.0.0:8000 \
  --workers 4 \
  --threads 2 \
  --worker-class sync \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
```

This matches the `CMD` in `api/Dockerfile` exactly.

---

### 10.4 Common pitfalls

**`RuntimeError: DATABASE_URL environment variable is not set`**

`db/session.py` raises this on the first DB operation if `DATABASE_URL` is missing. Check that your `.env` file is in the `api/` directory (not the repo root) and that `python-dotenv` is loading it. The `load_dotenv()` call is inside `create_app()`, so it only fires when the app factory runs — not on bare module import.

---

**`ModuleNotFoundError: No module named 'db'` or `No module named 'db.models'`**

This means `api/models.py` could not find the ingestion package. The shim builds the path as:

```python
_ingestion_path = os.path.join(os.path.dirname(__file__), "..", "ingestion")
```

`__file__` is the absolute path to `api/models.py`. If you moved files or are running from an unexpected working directory, print `_ingestion_path` to verify it resolves to a real directory containing `db/models.py`.

---

**`psycopg2.OperationalError: could not connect to server`**

The `DATABASE_URL` host is wrong for your environment:

| Environment | Correct host in `DATABASE_URL` |
|-------------|-------------------------------|
| Docker Compose | `postgres` (service name) |
| Local Postgres | `localhost` |
| Remote DB | hostname / IP |

---

**`sqlalchemy.exc.ProgrammingError: relation "cases" does not exist`**

Migrations have not been run yet. Run them from the `ingestion/` directory:

```bash
cd ingestion
alembic -c alembic.ini upgrade head
```

---

**Stale cached responses in development**

If you are testing code that changes aggregate data and `/api/stats` or `/api/commissions` still return old values, the cache TTL has not expired yet. Either wait for it, or if running without Redis, restart the Flask process. If running with Redis, delete the keys:

```bash
redis-cli DEL stats commissions_list
```
