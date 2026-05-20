# Plan: API Request Logging Middleware

## Context
The API currently logs requests to stdout via `structlog` in `_after()` ([middleware.py:153–161](api/middleware.py#L153-L161)), but there is no persistent DB record of API calls. The requirement is to write an opt-in logging layer: a `_LOGGED_PATHS` frozenset (mirroring the existing `_PUBLIC_PATHS` pattern in [middleware.py:27–32](api/middleware.py#L27-L32)) controls which endpoints are persisted to a new `api_request_logs` table. No route handler needs to change — all work happens in the existing `_after()` hook.

---

## Files to Change

| File | Action |
|------|--------|
| `migrations/versions/0004_add_api_request_logs.py` | **CREATE** — Alembic migration |
| `ingestion/db/models.py` | **MODIFY** — append `ApiRequestLog` ORM model |
| `api/models.py` | **MODIFY** — re-export `ApiRequestLog` |
| `api/db/queries.py` | **MODIFY** — add `insert_api_request_log()` |
| `api/middleware.py` | **MODIFY** — add `_LOGGED_PATHS` + DB insert in `_after()` |

---

## Step 1 — Migration `migrations/versions/0004_add_api_request_logs.py`

Follow the exact syntax of [0002_add_voc_complaints.py](migrations/versions/0002_add_voc_complaints.py). No enums needed; no `updated_at` trigger (logs are immutable write-once rows).

```python
"""Add api_request_logs table for API call auditing.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-20 00:00:00.000000

New table:
  api_request_logs — persistent record of inbound HTTP requests for
                     paths listed in middleware._LOGGED_PATHS.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    op.create_table(
        "api_request_logs",
        sa.Column("id",           sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("request_id",   sa.String(36),   nullable=True),   # UUID from X-Request-ID
        sa.Column("user_id",      sa.String(255),  nullable=True),   # SSO user_id; NULL on public paths
        sa.Column("user_email",   sa.String(255),  nullable=True),
        sa.Column("method",       sa.String(10),   nullable=False),
        sa.Column("path",         sa.String(500),  nullable=False),
        sa.Column("query_string", sa.Text(),       nullable=True),
        sa.Column("status_code",  sa.Integer(),    nullable=False),
        sa.Column("duration_ms",  sa.Integer(),    nullable=False),
        sa.Column("remote_addr",  sa.String(45),   nullable=True),   # IPv4 or IPv6
        sa.Column("created_at",   sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_api_request_logs"),
    )

    op.create_index("idx_arl_path",        "api_request_logs", ["path"])
    op.create_index("idx_arl_user_id",     "api_request_logs", ["user_id"])
    op.create_index("idx_arl_status_code", "api_request_logs", ["status_code"])
    op.create_index("idx_arl_created_at",  "api_request_logs", ["created_at"])


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    op.drop_index("idx_arl_created_at",  table_name="api_request_logs")
    op.drop_index("idx_arl_status_code", table_name="api_request_logs")
    op.drop_index("idx_arl_user_id",     table_name="api_request_logs")
    op.drop_index("idx_arl_path",        table_name="api_request_logs")
    op.drop_table("api_request_logs")
```

---

## Step 2 — ORM Model in `ingestion/db/models.py`

Append `ApiRequestLog` after the last model class. No FK, no enum, no trigger.

```python
class ApiRequestLog(Base):
    __tablename__ = "api_request_logs"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    request_id   = Column(String(36),  nullable=True)
    user_id      = Column(String(255), nullable=True)
    user_email   = Column(String(255), nullable=True)
    method       = Column(String(10),  nullable=False)
    path         = Column(String(500), nullable=False)
    query_string = Column(Text,        nullable=True)
    status_code  = Column(Integer,     nullable=False)
    duration_ms  = Column(Integer,     nullable=False)
    remote_addr  = Column(String(45),  nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
```

---

## Step 3 — Re-export in `api/models.py`

Add `ApiRequestLog` to the existing import that re-exports ingestion models:

```python
from ingestion.db.models import (
    ...,          # existing models
    ApiRequestLog,
)
```

---

## Step 4 — Query function in `api/db/queries.py`

Add at the bottom of the file. Uses the primary (write) session — same `get_session()` pattern already used throughout the file.

```python
# ---------------------------------------------------------------------------
# API Request Logging
# ---------------------------------------------------------------------------

def insert_api_request_log(
    *,
    request_id:   str | None,
    user_id:      str | None,
    user_email:   str | None,
    method:       str,
    path:         str,
    query_string: str | None,
    status_code:  int,
    duration_ms:  int,
    remote_addr:  str | None,
) -> None:
    with get_session() as session:
        session.add(ApiRequestLog(
            request_id=request_id,
            user_id=user_id,
            user_email=user_email,
            method=method,
            path=path,
            query_string=query_string or None,
            status_code=status_code,
            duration_ms=duration_ms,
            remote_addr=remote_addr,
        ))
```

---

## Step 5 — Middleware changes in `api/middleware.py`

### 5a — Add `_LOGGED_PATHS` frozenset directly below `_PUBLIC_PATHS` (line 32)

```python
# Paths whose inbound requests are persisted to api_request_logs.
# Only routes listed here are logged — add/remove as needed.
_LOGGED_PATHS: frozenset[str] = frozenset({
    "/api/cases",
    "/api/stats",
    "/api/batch",
})
```

### 5b — Add lazy import inside `_after()` (avoids module-level circular import)

```python
from db.queries import insert_api_request_log
```

### 5c — Append DB insert at the end of `_after()`, before `return response`

```python
if request.path in _LOGGED_PATHS:
    try:
        from db.queries import insert_api_request_log
        user_info = g.get("user_info") or {}
        qs = request.query_string.decode("utf-8", errors="replace") or None
        insert_api_request_log(
            request_id   = g.get("request_id"),
            user_id      = user_info.get("user_id"),
            user_email   = user_info.get("email"),
            method       = request.method,
            path         = request.path,
            query_string = qs,
            status_code  = response.status_code,
            duration_ms  = duration_ms,
            remote_addr  = request.remote_addr,
        )
    except Exception:
        logger.warning("api_log_insert_failed", path=request.path)
```

The `try/except` guarantees a DB failure never disrupts the HTTP response.

---

## Verification

1. Run `alembic upgrade head` — confirm `api_request_logs` table and 4 indexes exist via `\d api_request_logs` in psql.
2. `GET /api/cases` with a valid Bearer token → `SELECT * FROM api_request_logs ORDER BY id DESC LIMIT 1;` returns a row with correct `user_id`, `status_code`, `duration_ms`.
3. `GET /api/stats` if **not** in `_LOGGED_PATHS` → no new row inserted.
4. Simulate a DB failure (e.g. temporarily wrong `DATABASE_URL`) while hitting a logged path → response still returns `200` / correct payload; `WARNING api_log_insert_failed` appears in logs.
5. Hit a public path (e.g. `/health`) while it's in `_LOGGED_PATHS` → row inserted with `user_id = NULL`.
