# Fix: `api_call_log` Population

## Answers to Your Three Questions

### 1. What API calls are/should be logged there?
Every outbound HTTP GET made by `EJagritiClient` in `ingestion/client.py`:
- `GET /master/master/v2/getAllCommission`
- `GET /master/master/v2/getCommissionDetailsByStateId`
- `GET /report/report/getCauseTitleListByCompany` (fetch_cases)
- `GET /case/caseFilingService/v2/getCaseStatus` (fetch_case_detail)
- `GET /case/caseFilingService/v2/getHearingOrderById` (fetch_orders / fetch_judgments)

**Every attempt** should be recorded — including retried 429/503 responses — not just the final one. The CMS client (`cms_client.py`) is excluded from scope since `fetch_voc` currently returns dummy data.

---

### 2. Why is the table empty?
`log_api_call()` in `ingestion/db/upsert.py` (line 399) is defined but **never invoked**. `EJagritiClient.get()` logs every call via `structlog` (JSON stdout/file), but has no DB-write path. The wiring was simply never completed.

---

### 3. Fix Plan

## Approach: Inject a `call_logger` callback into `EJagritiClient`

Rather than changing `get()`'s return type (which would require updating all 5+ job callers), we add an optional `call_logger` callable to the client. The scheduler injects a DB-writing closure when constructing the client. **Job files need zero changes.**

---

## Step 1 — Modify `EJagritiClient` in `ingestion/client.py`

Add `call_logger` parameter to `__init__`:
```python
from typing import Callable

CallLoggerFn = Callable[[str, str, int | None, int, int, str | None], None]
# signature: (endpoint, method, response_code_or_None, duration_ms, attempt_number, user_agent_or_None)

def __init__(self, ..., call_logger: CallLoggerFn | None = None) -> None:
    ...
    self._call_logger = call_logger
```

In `get()`, call `self._call_logger` at **every outcome** (after `duration_ms` is known):

| Outcome | Where to insert | response_code |
|---|---|---|
| Successful response (2xx) | after `logger.info("http_call", ...)` ~line 158 | `response.status_code` |
| Retryable (429/503/502/504) | inside `if response.status_code in _RETRYABLE_STATUSES` block | `response.status_code` |
| 403 PermissionError | after `logger.error("http_403_forbidden", ...)` | `403` |
| Network/timeout error | inside `except (httpx.TimeoutException, httpx.NetworkError)` | `None` |
| Non-retryable HTTPStatusError | inside `except httpx.HTTPStatusError` | `exc.response.status_code` |

Helper call pattern (add at each site):
```python
if self._call_logger:
    self._call_logger(path, "GET", response_code, duration_ms, attempt, headers.get("User-Agent"))
```

---

## Step 2 — Modify `ingestion/scheduler.py`

Add import:
```python
from db.upsert import close_ingestion_run, create_ingestion_run, log_api_call
```

Change `_make_client()` to accept `run_id` and build the callback closure:
```python
def _make_client(run_id: int | None = None) -> EJagritiClient:
    def _log_call(endpoint, method, code, ms, attempt, ua):
        with get_session() as session:
            log_api_call(
                session,
                run_id=run_id,
                endpoint=endpoint,
                method=method,
                response_code=code,
                duration_ms=ms,
                retry_count=attempt,
                user_agent=ua,
            )

    return EJagritiClient(
        base_url=_BASE_URL,
        max_concurrent=_MAX_CONCURRENT,
        max_retries=_MAX_RETRIES,
        call_logger=_log_call,
    )
```

In `_run_job()`, pass `run_id` to `_make_client()` (run_id is available at this point):
```python
# Change:
with _make_client() as client:
# To:
with _make_client(run_id=run_id) as client:
```

---

## Files Changed

| File | Change |
|---|---|
| `ingestion/client.py` | Add `call_logger` param + 5 call sites inside `get()` |
| `ingestion/scheduler.py` | Import `log_api_call`, update `_make_client()` + `_run_job()` |

No changes to any job files (`fetch_cases.py`, `fetch_case_detail.py`, etc.).

---

## Verification

After deploying:
1. Trigger a manual run via `EJAGRITI_RUN_ONCE=true`
2. Query: `SELECT count(*), response_code FROM api_call_log GROUP BY response_code;`
3. Confirm rows appear with correct `run_id`, `endpoint`, `duration_ms`, and `retry_count > 0` for retried calls
4. Cross-check: `api_call_log` row count should approximately match `ingestion_runs.total_calls` for the same `run_id`
