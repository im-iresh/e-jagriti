# Batch Behavior Analysis, Status Criteria & Verification Plan

## Context

The ingestion pipeline has been running continuously for ~5-6 days. Before a regular daily run, a "special batch" was executed to backfill ~1100 cases from the last year. After it completed, the DB showed far fewer records with a recently updated `last_fetched_at` than the expected ~1100. This document explains why, documents the status classification logic, and provides a step-by-step verification plan.

---

## How Case Status Is Decided

Status is a **derived field** — it is never read directly from the API; it is computed by the ingestion code from a raw API string field called `case_stage_name`.

The mapping function `_map_status()` exists in **two files** with identical logic:
- [ingestion/jobs/fetch_cases.py](ingestion/jobs/fetch_cases.py)
- [ingestion/jobs/fetch_case_detail.py](ingestion/jobs/fetch_case_detail.py)

### Status Derivation Rules (`_map_status`)

```
Input: case_stage_name  (raw string from API, e.g. "DISPOSED OFF", "Admit", "Notice Issued")

Step 1 — Uppercase the input.

Step 2 — Check for CLOSED keywords (substring match):
    DISPOSED, DISMISSED, WITHDRAWN, CLOSED, DECIDED, ALLOWED, REJECTED
    → status = "closed"

Step 3 — Check for OPEN keywords (exact match):
    REGISTERED, ADMIT, NOTICE ISSUED
    → status = "open"

Step 4 — Everything else (unknown stage, blank, or None):
    → status = "pending"
```

### Status Values

| Status    | Meaning                                                                 | Picked up by `fetch_case_detail`? |
|-----------|-------------------------------------------------------------------------|-----------------------------------|
| `open`    | Actively registered/admitted; hearings ongoing                          | YES                               |
| `pending` | Stage name is unknown, blank, or doesn't match any keyword              | YES                               |
| `closed`  | Disposed/dismissed/decided/withdrawn — case is resolved                 | NO (skipped forever)              |

**Key risk:** Any stage name not in the closed or open keyword lists defaults to `"pending"`. This is safe (it will be re-checked daily), but means the DB may show `pending` for cases that are logically open.

### Which API Source Sets the Status?

| Job               | API Endpoint                     | Accuracy     | When status is written |
|-------------------|----------------------------------|--------------|------------------------|
| `fetch_cases`     | `getCauseTitleListByCompany`     | Lower — lightweight list, coarser stage data | Daily (on every upsert) |
| `fetch_case_detail` | `getCaseStatus`               | Higher — full detail, richer stage info | Daily (only for open/pending cases) |

**Critical design flaw:** `fetch_cases` runs before `fetch_case_detail` and upserts `status` from the lighter API. This means a case's authoritative status (set by `fetch_case_detail`) can be **overwritten daily** by a less accurate value from the list endpoint — potentially marking a genuinely open case as `closed` and silently removing it from the daily refresh cycle.

---

## What Is Actually Happening in the Batch

### Regular Daily Flow (APScheduler)

```
00:00  fetch_commissions   — refresh commission registry
01:00  fetch_cases         — discover new Samsung cases (default: yesterday's date range)
                             → upserts case_number, status, stage, dates, etc.
                             → does NOT set last_fetched_at
03:00  fetch_voc           — match VOC complaints to cases
06:00  fetch_case_detail   — for all cases WHERE status IN ('open','pending'):
                               call getCaseStatus, update all fields, stamp last_fetched_at
12:00  fetch_orders        — download PDFs for ready hearings
18:00  fetch_judgments     — queue judgment PDFs for closed cases
```

### Special 1-Year Backfill Batch

The "special batch" was `fetch_cases.py` (or `run_once_batch`) run with `EJAGRITI_FETCH_CASES_FROM_DATE` set to ~1 year ago. This queried the lightweight list API for all ~1100 cases in that window.

**Step-by-step what happened:**

1. `fetch_cases` upserted 1100 cases with status derived from the **lightweight list API**
   - Cases filed a year ago and since resolved → `case_stage_name` = "DISPOSED OFF", "DECIDED", etc. → `status = 'closed'`
   - Result: the majority of 1100 historical cases were written to the DB as `closed`

2. `fetch_case_detail` ran and queried `WHERE status IN ('open', 'pending')`
   - **All closed cases were skipped** — no `last_fetched_at` update
   - Only the open/pending minority received a detail fetch and got `last_fetched_at` stamped

3. User checks DB and sees only N << 1100 records with a recent `last_fetched_at`

### Why This Is a Problem Beyond the Backfill

Every daily run of `fetch_cases` also upserts `status` from the lightweight API. If any currently-open case has a stage name the lightweight API shows as a closed keyword (e.g., "ALLOWED IN PART" for an interim order), `fetch_cases` will reclassify it as `closed`. `fetch_case_detail` will then skip it forever — **silently, with no error or alert**.

---

## Verification Plan

### Step 1 — Understand the Current State

Run these SQL queries on your Postgres DB to confirm the diagnosis:

```sql
-- Distribution of all cases by status
SELECT status, COUNT(*) AS total
FROM cases
GROUP BY status
ORDER BY total DESC;

-- How many open/pending cases are stale (not refreshed in >25 hours)
SELECT COUNT(*) AS stale_open_pending
FROM cases
WHERE status IN ('open', 'pending')
  AND (last_fetched_at IS NULL OR last_fetched_at < NOW() - INTERVAL '25 hours');

-- How many closed cases were NEVER detail-fetched (stuck from the backfill)
SELECT COUNT(*) AS closed_never_fetched
FROM cases
WHERE status = 'closed'
  AND last_fetched_at IS NULL;

-- Cases filed in the last year, broken down by status
SELECT status, COUNT(*) AS total
FROM cases
WHERE filing_date >= CURRENT_DATE - INTERVAL '1 year'
GROUP BY status;
```

### Step 2 — Fix the Status Clobber Bug

**File:** [ingestion/db/upsert.py](ingestion/db/upsert.py), `upsert_case()` function (~line 112)

Exclude `status` from the `ON CONFLICT DO UPDATE SET` clause so only `fetch_case_detail` (which uses the accurate full detail API) can set the status of existing cases:

```python
# Current (broken):
set_={k: pg_insert(Case).excluded[k]
      for k in data
      if k not in ("id", "case_number", "created_at")},

# Fixed:
set_={k: pg_insert(Case).excluded[k]
      for k in data
      if k not in ("id", "case_number", "created_at", "status")},
```

**Why this is safe:** New cases are inserted with `status = 'pending'` (the DB column `server_default`). `fetch_case_detail` picks them up on the next run and sets the correct status from the full detail API. Cases that are genuinely closed will be reclassified to `'closed'` at that point and correctly dropped from future daily refreshes.

### Step 3 — One-Off Force-Refresh of Historical Cases

To backfill `last_fetched_at` for the 1100 historical cases stuck as `closed` (without ever having been detail-fetched), run this SQL once before the next `fetch_case_detail` run:

```sql
-- Re-queue historical cases classified closed by the lightweight API
-- but never verified by the full detail API
UPDATE cases
SET status = 'pending'
WHERE status = 'closed'
  AND last_fetched_at IS NULL
  AND filing_date >= CURRENT_DATE - INTERVAL '1 year';
```

After the next `fetch_case_detail` run (06:00 or next `run_once_batch`), these cases will be fetched and their status correctly set from the full detail API. Cases that are genuinely closed will return to `'closed'`; cases that are still open will become `'open'` or `'pending'` and enter the daily refresh cycle.

### Step 4 — Confirm Daily Open-Case Refresh Is Working

After one full daily cycle post-fix, run:

```sql
-- Should return 0 if all open/pending cases were refreshed within the last 25 hours
SELECT COUNT(*) AS not_refreshed_today
FROM cases
WHERE status IN ('open', 'pending')
  AND (last_fetched_at IS NULL OR last_fetched_at < NOW() - INTERVAL '25 hours');
```

Also check the structured log line `fetch_case_detail_complete` — the `updated + skipped` count should equal the total count of open+pending cases in the DB.

---

## Summary of Changes

| Action | File / Location | Why |
|--------|-----------------|-----|
| Code fix | [ingestion/db/upsert.py](ingestion/db/upsert.py) ~line 112 — add `"status"` to exclusion list | Prevent lightweight list API from overwriting accurate status |
| One-off SQL | Run against DB before next `fetch_case_detail` | Force-refresh historical cases stuck as `closed` without detail |

No changes needed to [ingestion/jobs/fetch_case_detail.py](ingestion/jobs/fetch_case_detail.py) or [ingestion/jobs/fetch_cases.py](ingestion/jobs/fetch_cases.py) — their logic is correct once the upsert is fixed.
