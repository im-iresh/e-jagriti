# eJagriti API Service — Developer Guide

This guide covers everything you need to understand, run, and extend the API service. It assumes you are a developer but have no prior knowledge of this project.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Repository Layout](#2-repository-layout)
3. [Response Envelope](#3-response-envelope)
4. [Middleware & Auth](#4-middleware--auth)
5. [Configuration & Environment Variables](#5-configuration--environment-variables)
6. [Endpoints Reference](#6-endpoints-reference)
7. [Caching](#7-caching)
8. [Rate Limiting](#8-rate-limiting)
9. [Adding a New Endpoint](#9-adding-a-new-endpoint)
10. [Running Locally](#10-running-locally)

---

## 1. Overview

**What is this service?**

The API service is a read-only Flask HTTP API that exposes the case data collected by the ingestion service. It is the backend for the eJagriti Samsung case tracker frontend.

**Why does it exist?**

The ingestion service writes to PostgreSQL. The API service sits in front of that database and provides structured, paginated, filterable JSON endpoints so that a frontend (or any HTTP client) can query case data without touching the database directly.

**What does it expose?**

| Resource | Endpoints |
|----------|-----------|
| Cases | `GET /api/cases` (list + filters), `GET /api/cases/<id>` (detail), `GET /api/cases/alerts` (alert feed) |
| Hearings | `GET /api/cases/<id>/hearings` (hearing list for a case) |
| Orders | `GET /api/cases/<case_id>/hearings/<hearing_id>/orders` (paginated daily orders per hearing) |
| PDF | `GET /api/cases/<case_id>/hearings/<hearing_id>/orders/<order_id>/pdf` (stream PDF from NFS) |
| Stats | `GET /api/stats` (aggregate counts + monthly series) |
| Health | `GET /health` (DB liveness + last ingestion run) |
| Batch Status | `GET /api/batch/status` (ingestion pipeline status for debugging) |

**Technology stack:**

- Python 3.11+, `Flask 3.x` as the web framework
- `SQLAlchemy 2.x` ORM for database access (shared models with ingestion service)
- `Flask-Caching 2.x` for Redis-backed response caching
- `Flask-Limiter 3.x` for per-IP rate limiting
- `Flask-CORS 4.x` for CORS header management
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
│   ├── auth.py                 # require_permission() decorator + PERMISSIONS registry
│   │                           # Reads service/role/permission structure from g.user_info
│   ├── middleware.py           # SSO token resolution, service access gate,
│   │                           # request ID injection, structured request logging
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
│   │   ├── cases.py            # GET /api/cases, /api/cases/alerts, /api/cases/<id>,
│   │   │                       # GET /api/cases/<id>/hearings
│   │   ├── orders.py           # GET /api/cases/<cid>/hearings/<hid>/orders
│   │   │                       # GET /api/cases/<cid>/hearings/<hid>/orders/<oid>/pdf
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
  └─ initialises Cache, Limiter, CORS extensions
  └─ calls register_middleware(app) from middleware.py
  └─ registers 4 Blueprints from routes/

routes/*.py
  └─ each route calls one or more query functions from db/queries.py
  └─ uses @require_permission() from auth.py
  └─ wraps results with success_response() / error_response() from schemas/responses.py

middleware.py
  └─ _resolve_user: calls SSO userinfo endpoint, sets g.user_info = rsp["rsp"]["data"]
  └─ _enforce_api_auth: checks g.user_info is set + user has access to this service

auth.py
  └─ require_permission(permission_id): checks user's roles/permissions against required permission

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

Used when returning a single object or a non-paginated list (e.g. `/api/cases/<id>`).

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

Used when returning a list with pagination metadata (e.g. `GET /api/cases`, `GET /api/cases/<case_id>/hearings/<hearing_id>/orders`). Pass `page`, `per_page`, and `total` to `success_response()` and the `meta.pagination` block is added automatically.

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
| `UNAUTHORIZED` | 401 | No valid Bearer token provided |
| `FORBIDDEN` | 403 | Token valid but user lacks service access or required permission |
| `NOT_FOUND` | 404 | Resource does not exist |
| `PDF_NOT_READY` | 404 | Order exists but PDF has not been fetched yet |
| `FILE_NOT_FOUND` | 404 | `pdf_storage_path` is set but file is missing on NFS disk |
| `INVALID_PARAMS` | 400 | `page`, `per_page`, or `runs` is not an integer |
| `INVALID_STATUS` | 400 | `status` query param is not a valid enum value |
| `INVALID_COMMISSION_TYPE` | 400 | `commission_type` not in `national/state/district` |
| `METHOD_NOT_ALLOWED` | 405 | Wrong HTTP method |
| `RATE_LIMIT_EXCEEDED` | 429 | Client hit the rate limit |
| `SSO_UNAVAILABLE` | 503 | Could not connect to the SSO service (timeout or network error) |
| `CONFIGURATION_ERROR` | 500 | Server misconfiguration (e.g. `SSO_URL` or `SERVICE_ID` not set) |
| `INTERNAL_ERROR` | 500 | Unhandled exception (logged server-side) |

Python call:

```python
return error_response("NOT_FOUND", f"Case {case_id} not found", 404)
```

---

## 4. Middleware & Auth

Middleware is registered in `middleware.py` via `register_middleware(app)`, called inside `create_app()` before blueprints are loaded.

Hook execution order per request:

```
1. _resolve_user      — calls SSO, populates g.user_info
2. _enforce_api_auth  — checks auth + service membership for /api/* paths
3. _before            — assigns request ID, records start time
   ... route handler runs ...
4. _after             — logs request, injects response headers
```

---

### 4.1 SSO Token Resolution (`_resolve_user`)

Fires on every request that carries an `Authorization: Bearer <token>` header. Calls `GET {SSO_URL}/api/v1/sso/userInfo` and extracts the user object from `response["rsp"]["data"]`, storing it on `g.user_info`.

**Error cases and their responses:**

| Scenario | Response |
|----------|----------|
| No `Authorization: Bearer` header | `g.user_info = None`; `_enforce_api_auth` returns **401** |
| `EJAGRITI_SSO_URL` not configured | **500** `CONFIGURATION_ERROR` |
| Network error or timeout reaching SSO | **503** `SSO_UNAVAILABLE` |
| SSO returns non-200 (401, 403, 500, etc.) | SSO response proxied back as-is (same body + status code) |
| SSO returns 200 but `rsp.data` is null/missing | `g.user_info = None`; `_enforce_api_auth` returns **401** (logged as warning) |
| SSO returns 200 with valid `rsp.data` | `g.user_info = data`; request proceeds |

**Expected SSO user object shape** (stored in `g.user_info`):

```json
{
  "uuid": "...",
  "userID": "u123",
  "services": [
    { "id": "svc-001", "name": "eJagriti", "domain": "jagriti.example.com" }
  ],
  "roles": [
    { "roleId": "role-1", "roleName": "CaseViewer", "serviceId": "svc-001" }
  ],
  "permissions": [
    { "permissionId": "p-1", "permissionName": "cases:read", "roleIdList": ["role-1"] }
  ]
}
```

---

### 4.2 API Auth & Service Gate (`_enforce_api_auth`)

Runs for every `/api/*` path not in `_PUBLIC_PATHS` (`/health`, `/api/docs`, `/api/openapi.json`).

Two checks in sequence:

1. **Authentication** — if `g.user_info` is `None` → return **401 UNAUTHORIZED**
2. **Service access** — if `EJAGRITI_SERVICE_ID` is set, verify the user's `services[]` list contains a service with that ID → return **403 FORBIDDEN** if not found

If `EJAGRITI_SERVICE_ID` is not configured, the service check is skipped but a warning is logged on every request.

---

### 4.3 Permission Check (`require_permission` in `auth.py`)

A route decorator that enforces fine-grained permission control on top of the service gate. Applied per-route:

```python
@cases_bp.get("")
@require_permission("cases:read")
def list_cases(): ...
```

**How the check works:**

1. Collect the user's role IDs scoped to the configured `SERVICE_ID`:
   `{r["roleId"] for r in user_info["roles"] if r["serviceId"] == SERVICE_ID}`
2. Search `user_info["permissions"]` for an entry where `permissionName == permission_id`
3. Check if that permission's `roleIdList` intersects with the user's role IDs → **403** if not

**Permission registry** (defined in `auth.py`):

| Permission | Covers |
|------------|--------|
| `cases:read` | Case list, case detail, alerts, hearings |
| `orders:read` | Daily orders and PDF serving |
| `stats:read` | Aggregate statistics |
| `batch:read` | Batch run status |

---

### 4.4 Request ID

Every request is assigned a UUID stored on Flask's `g` object and echoed back in the `X-Request-ID` response header.

1. `_before`: reads `X-Request-ID` from the incoming headers. If present, reuses it (lets clients correlate their own IDs). If absent, generates a new `uuid.uuid4()`.
2. `_after`: writes the ID into `response.headers["X-Request-ID"]`.

When debugging a specific failed request you can pass your own `X-Request-ID` header and trace it through server logs by grepping for the UUID.

---

### 4.5 Structured Request Logging

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

`duration_ms` is measured with `time.monotonic()` — it covers full request processing including DB query time.

---

### 4.6 Security Headers

Injected by `_after` on every response:

| Header | Value |
|--------|-------|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains` |
| `Cache-Control` | `no-store` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | `default-src 'none'` (strict); relaxed for `/api/docs` to allow Swagger UI CDN assets |

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
| `REPLICA_DATABASE_URL` | `None` | No | If set, all `read_only=True` sessions are routed here instead of the primary. When unset, both read and write sessions use `DATABASE_URL`. |
| `SECRET_KEY` | `dev-secret-change-in-prod` | **Yes in prod** | Flask secret key used for session signing. Override with a random string in production. |
| `FLASK_ENV` | `production` | No | Controls which Config class is loaded. Set to `testing` in CI. |
| `DEBUG` | `false` | No | Enables Flask debug mode (auto-reload, detailed tracebacks). Never set `true` in production. |
| `REDIS_URL` | `redis://localhost:6379/0` | No | Redis connection URL. Used by both Flask-Caching and Flask-Limiter. Falls back to SimpleCache / memory:// if unset. |
| `EJAGRITI_SSO_URL` | `https://sso.example.com` | **Yes in prod** | Base URL of the SSO service. The API calls `{SSO_URL}/api/v1/sso/userInfo` to validate tokens. Returns 500 `CONFIGURATION_ERROR` if unset when a Bearer token is received. |
| `EJAGRITI_SERVICE_ID` | *(empty)* | **Yes in prod** | The service ID that this API is registered under in the SSO. Users must have this service in their `services[]` list to access any `/api/*` endpoint. If unset, service check is skipped (warning logged). |
| `EJAGRITI_PDF_STORAGE_ROOT` | `/mnt/pdfs` | No | NFS mount root for PDF files. When `pdf_storage_path` in the DB is a relative path, it is resolved relative to this root. |
| `EJAGRITI_CORS_ORIGINS` | `*` | No | Comma-separated list of allowed CORS origins, or `*` to allow all. e.g. `https://app.example.com,https://admin.example.com`. Defaults to `*` which is safe behind an SSO auth gate. |
| `EJAGRITI_CORS_MAX_AGE` | `600` | No | Seconds browsers may cache preflight responses (10 min default). |
| `EJAGRITI_CACHE_TTL_SECONDS` | `3600` | No | Default TTL for cached responses in seconds. Applies to `/api/stats`. |
| `EJAGRITI_RATE_LIMIT_PER_MINUTE` | `100` | No | Max requests per minute per IP address. |
| `EJAGRITI_LOG_LEVEL` | `INFO` | No | Structlog/stdlib log level. Accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `EJAGRITI_SA_POOL_SIZE` | `5` | No | SQLAlchemy connection pool size per engine (persistent connections). |
| `EJAGRITI_SA_MAX_OVERFLOW` | `10` | No | Extra connections allowed above pool size. Total max = `SA_POOL_SIZE + SA_MAX_OVERFLOW`. |
| `TEST_DATABASE_URL` | falls back to `DATABASE_URL` | No | Used by `TestingConfig` for a separate test database. |

---

### Minimal `.env` for local development

```dotenv
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ejagriti
SECRET_KEY=local-dev-secret
FLASK_ENV=development
DEBUG=true
EJAGRITI_LOG_LEVEL=DEBUG
EJAGRITI_SSO_URL=https://sso.example.com
EJAGRITI_SERVICE_ID=svc-001
# REDIS_URL=redis://localhost:6379/0   # uncomment if Redis is running locally
# REPLICA_DATABASE_URL=               # leave unset to use primary for reads
# EJAGRITI_PDF_STORAGE_ROOT=/mnt/pdfs # override if PDFs are mounted elsewhere
```

---

### How `REDIS_URL` affects caching and rate limiting

Flask-Caching and Flask-Limiter both read `REDIS_URL` at startup:

- **With Redis:** Cache is shared across all gunicorn workers and container replicas. Rate limit counters are also shared.
- **Without Redis:** `CACHE_TYPE` falls back to `SimpleCache` (per-process, in-memory). Rate limit storage falls back to `memory://`. Fine for local development or single-worker deployments but not for multi-worker setups.

---

### Connection pool sizing guide

The total number of Postgres connections the API can hold open is:

```
max_connections = (SA_POOL_SIZE + SA_MAX_OVERFLOW) × gunicorn_workers
```

With defaults (5 + 10 = 15) and 4 gunicorn workers that's 60 connections. If a read replica is configured, each engine maintains its own separate pool, doubling the total. Postgres's default `max_connections` is 100, so with both the API and ingestion service running you should either raise Postgres's limit or use PgBouncer in transaction mode.

---

## 6. Endpoints Reference

All endpoints are prefixed with no version segment (e.g. `/api/cases`, not `/v1/api/cases`). All responses use the envelope described in section 3. All `/api/*` endpoints require a valid Bearer token; `/health` is public.

---

### 6.1 `GET /api/cases` — List cases

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Auth:** `cases:read`
**Purpose:** Paginated, filterable list of all Samsung cases.

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
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/cases?status=open&commission_type=district&page=1&per_page=5"
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
    "pagination": { "page": 1, "per_page": 5, "total": 347, "total_pages": 70 }
  }
}
```

**Ordering:**

Cases are ordered by four keys in priority order:

1. Cases with a future hearing date come before cases with no upcoming hearing.
2. Within the "upcoming" group: soonest hearing date first.
3. Within the "no upcoming hearing" group: most recently filed first.
4. Final tiebreaker: `case_id DESC` (deterministic pagination).

This is a CASE expression sort — no index is used. At ~10k rows PostgreSQL sorts in memory in under 20 ms.

---

### 6.2 `GET /api/cases/alerts` — Alert cases

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Auth:** `cases:read`
**Purpose:** Open/pending cases that need attention, grouped into alert sections. Designed for notification feeds and operator dashboards.
**Caching:** Not cached — always returns live data.

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `no_voc` | string | Pass `Y` to include cases with no linked VOC complaint |
| `hearing_soon` | string | Pass `Y` to include cases with a hearing in the next 2 days |

Omitting both parameters returns all alert sections.

**Example requests:**

```bash
# All alerts
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/cases/alerts

# Only cases without VOC
curl -H "Authorization: Bearer <token>" "http://localhost:8000/api/cases/alerts?no_voc=Y"

# Only imminent hearings
curl -H "Authorization: Bearer <token>" "http://localhost:8000/api/cases/alerts?hearing_soon=Y"
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
      "items": [...]
    }
  }
}
```

When only one section is requested, only that key is present in `data`.

**Alert conditions:**

| Section | Condition |
|---------|-----------|
| `no_voc` | `cases.voc_number IS NULL` — uses partial index `idx_cases_no_voc` |
| `hearing_soon` | `date_of_next_hearing BETWEEN today AND today+2` |

Both sections only include `status = open` or `status = pending` cases.

---

### 6.3 `GET /api/cases/<case_id>` — Case detail

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Auth:** `cases:read`
**Purpose:** Full nested case object including commission, all hearings in sequence order, and daily order records.

**Example request:**

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/cases/42
```

**Example response:**

```json
{
  "success": true,
  "data": {
    "case_id": 42,
    "case_number": "DC/77/CC/104/2025",
    "filing_date": "2025-03-14",
    "status": "open",
    "case_stage": "Arguments",
    "date_of_next_hearing": "2025-08-20",
    "commission": {
      "id": 7,
      "name": "District Consumer Commission Delhi-77",
      "type": "district"
    },
    "complainant": {
      "name": "Ramesh Kumar",
      "advocate_names": ["Adv. Priya Sharma"]
    },
    "respondent": {
      "name": "Samsung India Electronics Pvt. Ltd.",
      "advocate_names": ["Adv. Rohit Mehra"]
    },
    "hearings": [
      {
        "id": 301,
        "date": "2025-04-10",
        "next_date": "2025-05-15",
        "case_stage": "Admission",
        "proceeding_text": "<p>Case admitted...</p>",
        "sequence_number": 1,
        "daily_order_available": true
      }
    ],
    "last_fetched_at": "2025-07-10T04:32:11+00:00"
  }
}
```

**Notes:**
- `hearings` are sorted by `hearing_sequence_number` ascending (chronological).
- `proceeding_text` is sanitized HTML — safe to render directly in the frontend.
- Returns `404 NOT_FOUND` if `case_id` does not exist.

---

### 6.4 `GET /api/cases/<case_id>/hearings` — Hearing list

**Blueprint:** `cases_bp` (`routes/cases.py`)
**Auth:** `cases:read`
**Purpose:** Dedicated endpoint for the full hearing list for a case, in chronological order. Use when you need only hearings without the full case detail payload.

**Example request:**

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/cases/42/hearings
```

**Example response:**

```json
{
  "success": true,
  "data": [
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
  ]
}
```

**Notes:**
- Returns `404 NOT_FOUND` if `case_id` does not exist.
- Ordered by `hearing_sequence_number ASC`.

---

### 6.5 `GET /api/cases/<case_id>/hearings/<hearing_id>/orders` — Daily orders

**Blueprint:** `orders_bp` (`routes/orders.py`)
**Auth:** `orders:read`
**Purpose:** Paginated list of daily order records for a specific hearing.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | `1` | 1-based page number |
| `per_page` | integer | `20` | Max `100` |

**Example request:**

```bash
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/cases/42/hearings/301/orders"
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
      "pdf_fetched_at": "2025-04-11T02:15:00+00:00",
      "pdf_url": "/api/cases/42/hearings/301/orders/88/pdf"
    }
  ],
  "meta": {
    "pagination": { "page": 1, "per_page": 20, "total": 1, "total_pages": 1 }
  }
}
```

**Notes:**
- `pdf_url` is the path to stream the PDF (section 6.6). It is `null` when `pdf_fetched = false`.
- `order_type_id`: `1` = daily order, `2` = judgment (final order).
- Returns `404 NOT_FOUND` if the case or hearing does not exist.

---

### 6.6 `GET /api/cases/<case_id>/hearings/<hearing_id>/orders/<order_id>/pdf` — Stream PDF

**Blueprint:** `orders_bp` (`routes/orders.py`)
**Auth:** `orders:read`
**Purpose:** Stream the daily order PDF from NFS storage. Returns the PDF inline so the browser renders it directly (suitable for `<iframe>` or `<a href=...>`).

**Example request:**

```bash
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8000/api/cases/42/hearings/301/orders/88/pdf" \
  --output order.pdf
```

**Error responses:**

| Code | Error code | Condition |
|------|-----------|-----------|
| 404 | `NOT_FOUND` | Order/hearing/case combination does not exist |
| 404 | `PDF_NOT_READY` | Order exists but `pdf_fetched = false` |
| 404 | `FILE_NOT_FOUND` | `pdf_storage_path` is set but file is missing on NFS disk |

**Path resolution:**

If `pdf_storage_path` in the DB is a relative path, it is resolved against `EJAGRITI_PDF_STORAGE_ROOT` (default `/mnt/pdfs`). If it is already absolute, it is used as-is.

---

### 6.7 `GET /api/stats` — Aggregate statistics

**Blueprint:** `stats_bp` (`routes/stats.py`)
**Auth:** `stats:read`
**Purpose:** Dashboard-level aggregate counts and a 12-month filing time series. Cached for 1 hour.

**Example request:**

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/stats
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
- `cases_per_month` covers only the last 12 calendar months; months with zero filings are absent.
- Cached under key `"stats"` with TTL `EJAGRITI_CACHE_TTL_SECONDS` (default 1 h).

---

### 6.8 `GET /health` — Health check

**Blueprint:** `stats_bp` (`routes/stats.py`)
**Auth:** Public (no token required)
**Purpose:** Liveness probe. Returns DB connectivity status. Designed for load balancer health checks.

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
      "duration_seconds": 2851.2
    }
  }
}
```

**Response — DB unreachable (HTTP 503):**

```json
{ "success": false, "data": { "db_ok": false, "last_ingestion_run": null } }
```

**Notes:**
- Not cached — always runs a live `SELECT 1` against the primary database.
- `last_ingestion_run` will be `null` on a brand-new deployment before the first ingestion run.

---

### 6.9 `GET /api/batch/status` — Ingestion pipeline status

**Blueprint:** `batch_bp` (`routes/batch.py`)
**Auth:** `batch:read`
**Purpose:** Live snapshot of the ingestion pipeline. Shows recent run history, queue depths, and recent errors. Not cached.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `runs` | integer | `10` | Number of most recent ingestion runs to include. Max `50`. |

**Example request:**

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/batch/status?runs=3
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
        "total_calls": 0,
        "success_count": 0,
        "fail_count": 0,
        "skip_count": 0,
        "duration_seconds": null
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
        "endpoint": "/courtmaster/...",
        "http_status": 500,
        "error_type": "HTTP_ERROR",
        "error_message": "Server returned 500 for case 1034",
        "retry_count": 5,
        "created_at": "2025-04-01T01:44:12+00:00"
      }
    ]
  }
}
```

**`status` field derivation:**

| Value | Condition |
|-------|-----------|
| `"running"` | `finished_at` is `null` |
| `"failed"` | `finished_at` is set AND `fail_count > 0` |
| `"completed"` | `finished_at` is set AND `fail_count == 0` |

**`queue_depths` fields:**

| Field | What it counts |
|-------|---------------|
| `cases_pending_detail_fetch` | `cases` rows where `last_fetched_at IS NULL` |
| `pdfs_pending_fetch` | `daily_orders` rows where `pdf_fetched = false` |
| `failed_jobs_unresolved` | `failed_jobs` rows where `resolved = false` |

`recent_errors` always returns the 20 most recent rows from `ingestion_errors`.

---

## 7. Caching

---

### 7.1 Which endpoints cache

| Endpoint | Cache key | TTL | Notes |
|----------|-----------|-----|-------|
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

---

### 7.3 How to invalidate the cache manually

**Redis (production / docker-compose):**

```bash
redis-cli -u redis://localhost:6379/0 DEL stats
```

**SimpleCache (local dev without Redis):**

Restart the Flask process — SimpleCache does not survive restarts.

---

### 7.4 Cache usage pattern in routes

```python
cached = cache.get("stats")
if cached is not None:
    return success_response(cached)

data = get_stats()
cache.set("stats", data)       # uses CACHE_DEFAULT_TIMEOUT
return success_response(data)
```

The cache stores the raw `data` dict, not the full HTTP response. The `success` wrapper and HTTP headers are always freshly generated even on a cache hit.

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

The limiter uses `get_remote_address` as its key function — the client's IP address from `request.remote_addr`.

**Behind a reverse proxy (nginx, AWS ALB, Cloud Run):** `request.remote_addr` will be the proxy IP, not the real client IP. Fix this by setting Flask's `TRUSTED_PROXIES` or using `ProxyFix` middleware, otherwise all clients share a single counter.

---

### 8.3 Rate limit exceeded response

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

```
REDIS_URL set     →  RATELIMIT_STORAGE_URI = REDIS_URL    (shared across workers)
REDIS_URL absent  →  RATELIMIT_STORAGE_URI = "memory://"  (per-process only)
```

---

## 9. Adding a New Endpoint

Follow these four steps every time. The pattern is consistent across all existing routes.

---

### Step 1 — Write the query function in `db/queries.py`

All database logic lives here. Routes must not contain SQL or ORM calls.

```python
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
- Return plain Python dicts/lists — never ORM objects.
- Add a docstring with Args and Returns.

---

### Step 2 — Create or extend `routes/<resource>.py`

Keep route handlers thin — validate inputs, call the query function, wrap the result. Add `@require_permission()` for every protected route.

```python
from auth import require_permission

@stages_bp.get("/by-stage")
@require_permission("cases:read")
def list_cases_by_stage():
    stage = request.args.get("stage", "").strip()
    if not stage:
        return error_response("INVALID_PARAMS", "stage is required", 400)

    try:
        page     = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        return error_response("INVALID_PARAMS", "page and per_page must be integers", 400)

    result = get_cases_by_stage(stage=stage, page=page, per_page=per_page)
    return success_response(data=result["items"], page=page, per_page=per_page, total=result["total"])
```

---

### Step 3 — Register the Blueprint in `app.py`

```python
# app.py — inside create_app(), in the Blueprints section
from routes.stages import stages_bp
api_docs.register_blueprint(stages_bp)
```

---

### Step 4 — Add a permission to `auth.py` if needed

If the new endpoint introduces a new permission scope, add it to the `PERMISSIONS` dict in `auth.py` and ensure the SSO has a matching `permissionName` value:

```python
PERMISSIONS: dict[str, str] = {
    ...
    "stages:read": "View cases by stage",
}
```

---

## 10. Running Locally

---

### 10.1 Option A — Docker Compose (recommended)

```bash
# From the repo root
cp .env.example .env          # fill in any overrides (defaults work for local dev)
docker compose up --build
```

The API will be available at `http://localhost:8000`.

**Useful compose commands:**

```bash
docker compose up --build api       # Rebuild only the API image
docker compose logs -f api          # Tail API logs
docker compose run --rm migrations  # Run migrations only
docker compose exec postgres psql -U ejagriti ejagriti  # psql shell
```

---

### 10.2 Option B — Flask dev server (no Docker)

**Prerequisites:** Postgres running locally.

```bash
cd api
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r ../ingestion/requirements.txt

cp ../.env.example .env
# Edit .env — set DATABASE_URL, EJAGRITI_SSO_URL, EJAGRITI_SERVICE_ID at minimum

# Run migrations
cd ../ingestion && alembic -c alembic.ini upgrade head && cd ../api

FLASK_ENV=development flask --app app:create_app run --reload --port 8000
```

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

---

### 10.4 Common pitfalls

**`RuntimeError: DATABASE_URL environment variable is not set`**

Check that your `.env` file is in the `api/` directory and that `python-dotenv` is loading it. `load_dotenv()` is called inside `create_app()`, not on module import.

---

**`ModuleNotFoundError: No module named 'db'` or `No module named 'db.models'`**

The shim in `api/models.py` builds the path as:

```python
_ingestion_path = os.path.join(os.path.dirname(__file__), "..", "ingestion")
```

Verify you are running Flask from within the `api/` directory and that `../ingestion/db/models.py` exists.

---

**`psycopg2.OperationalError: could not connect to server`**

| Environment | Correct host in `DATABASE_URL` |
|-------------|-------------------------------|
| Docker Compose | `postgres` (service name) |
| Local Postgres | `localhost` |
| Remote DB | hostname / IP |

---

**`sqlalchemy.exc.ProgrammingError: relation "cases" does not exist`**

Migrations have not been run:

```bash
cd ingestion && alembic -c alembic.ini upgrade head
```

---

**All requests return `403 FORBIDDEN — You do not have access to this service`**

The user's SSO object does not include the service ID configured in `EJAGRITI_SERVICE_ID`. Either the service ID is wrong, or the user has not been granted access to this service in the SSO. Check the `permission_denied` / `sso_unexpected_shape` log entries.

---

**All requests return `500 CONFIGURATION_ERROR — SSO service is not configured`**

`EJAGRITI_SSO_URL` is not set in the environment. Add it to your `.env`.

---

**Stale cached responses in development**

```bash
redis-cli DEL stats          # if running with Redis
# or restart the Flask process if using SimpleCache
```
