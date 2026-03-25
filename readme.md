Code Flow
1. System Architecture

┌──────────────────────────────────────────────────────────┐
│                    Docker / Cloud                         │
│                                                           │
│  ┌─────────────────┐    ┌───────────────────────────┐    │
│  │ Ingestion Service│    │       API Service          │    │
│  │  (batch job)    │    │   (Flask + gunicorn)       │    │
│  │                 │    │                            │    │
│  │ APScheduler OR  │    │  GET /api/cases            │    │
│  │ RUN_ONCE=true   │    │  GET /api/cases/:id        │    │
│  │                 │    │  GET /api/cases/:id/orders │    │
│  │  writes only    │    │  GET /api/cases/:id/judgment│   │
│  └────────┬────────┘    │  GET /api/commissions      │    │
│           │             │  GET /api/stats             │    │
│           │             │  GET /health                │    │
│           ▼             └─────────────┬───────────────┘    │
│  ┌──────────────────────────────────┐ │                    │
│  │         PostgreSQL               │◄┘                    │
│  │  commissions, cases, hearings    │                      │
│  │  daily_orders, ingestion_runs... │                      │
│  └──────────────────────────────────┘                      │
│                                                           │
│  ┌──────────────────┐                                     │
│  │      Redis       │◄── Flask-Caching + Flask-Limiter    │
│  └──────────────────┘                                     │
└──────────────────────────────────────────────────────────┘
2. Ingestion Pipeline (daily, in order)

Step 1 — fetch_commissions.py
  GET /master/master/v2/getAllCommission
    → upsert national + state rows into commissions

  for each stateId:
    GET /master/master/v2/getCommissionDetailsByStateId?stateId=N
    → upsert state + district rows with parent_commission_id linkage

Step 2 — fetch_cases.py
  for each commission in DB:
    GET /report/report/getCauseTitleListByCompany
      ?commissionTypeId=N&commissionId=N
      &filingDate1=2015-01-01&filingDate2=TODAY
      &complainant_respondent_name_en=samsung
    → upsert lightweight case rows (no filing_reference_number yet)
    → status derived from case_stage_name string

Step 3 — fetch_case_detail.py
  for each case WHERE last_fetched_at IS NULL (batch of 50):
    GET /case/caseFilingService/v2/getCaseStatus?caseNumber=X
    → compute MD5(response) → skip if hash unchanged
    → update case row (filing_reference_number, stage, advocates as JSON)
    → upsert hearings[] rows (upsert key: case_id + court_room_hearing_id)
    → for each hearing where daily_order_availability_status=2:
         create daily_orders stub row (pdf_fetched=false)

Step 4 — fetch_orders.py
  for each daily_orders WHERE pdf_fetched=false:
    GET /courtmaster/courtRoom/judgement/v1/getDailyOrderJudgementPdf
      ?filingReferenceNumber=N&dateOfHearing=YYYY-MM-DD&orderTypeId=N
    → base64 decode → write to PDF_STORAGE_DIR or S3
    → update daily_orders SET pdf_fetched=true, pdf_storage_path=...

Step 5 — fetch_judgments.py
  for each case WHERE status='closed' AND no daily_order with order_type_id=2:
    → create daily_orders stub row (order_type_id=2, pdf_fetched=false)
    → fetch_orders picks it up in the next cycle
Rate limiting across all steps:


sleep_secs = (86400 / DAILY_CALL_BUDGET) ± 20% jitter   # ~24.7s ± 5s
threading.Semaphore(MAX_CONCURRENT_REQUESTS=2)            # max 2 in-flight
On 429/503: exponential backoff = 2^attempt + jitter, max 5 retries
On 403:     log to failed_jobs, skip (no crash)
3. API Request Flow

Client → gunicorn → Flask
           │
           ├── middleware.py: inject X-Request-ID, record start_time
           │
           ├── Flask-Limiter: check 100 req/min per IP (Redis)
           │
           ├── Route handler (routes/*.py)
           │     └── calls query function (db/queries.py)
           │           └── get_session(read_only=True)
           │                 → replica engine if REPLICA_DATABASE_URL set
           │                 → primary engine otherwise
           │
           ├── Flask-Caching: /api/commissions and /api/stats cached 1h in Redis
           │
           └── middleware.py: log method+path+status+duration_ms, add header
All responses: { "success": true, "data": ..., "meta": { "pagination": ... } }

All errors: { "success": false, "error": { "code": "NOT_FOUND", "message": "..." } }

How to Run
Local (no Docker)
Prerequisites: Python 3.11, PostgreSQL 15 running locally, Redis (optional — caching degrades gracefully to in-memory)


# 1. Clone and set up env
cp .env.example .env
# Edit .env:
#   DATABASE_URL=postgresql://youruser:yourpass@localhost:5432/ejagriti
#   REDIS_URL=redis://localhost:6379/0  (or omit for SimpleCache)
#   EJAGRITI_BASE_URL=https://e-jagriti.gov.in
#   SEARCH_KEYWORD=samsung

# 2. Create the database
createdb ejagriti

# 3. Run migrations (from repo root)
cd ingestion
pip install -r requirements.txt
DATABASE_URL=postgresql://youruser:yourpass@localhost:5432/ejagriti \
  alembic -c alembic.ini upgrade head

# 4. Run ingestion (one-shot, no DB writes)
cd ingestion
DRY_RUN=true RUN_ONCE=true python main.py

# 5. Run ingestion (one-shot, writes to DB)
RUN_ONCE=true python main.py

# 6. Run ingestion (always-on scheduler mode)
python main.py

# 7. Run the API (separate terminal)
cd api
pip install -r requirements.txt
FLASK_ENV=development flask --app "app:create_app()" run --port 8000

# 8. Run tests (no DB needed — all DB calls are mocked)
cd tests
pip install -r requirements.txt
pytest -v
Production (Docker)

# 1. Configure secrets
cp .env.example .env
# Fill in real values: DB password, SECRET_KEY (64-char random), etc.

# 2. Build and start all services
docker-compose up --build -d

# Services started:
#   postgres     — port 5432
#   redis        — port 6379
#   migrations   — runs alembic upgrade head, then exits
#   ingestion    — waits for migrations, then starts APScheduler
#   api          — waits for migrations, serves on port 8000

# 3. Check health
curl http://localhost:8000/health

# 4. View logs
docker-compose logs -f ingestion
docker-compose logs -f api
Cloud Run / ECS Scheduled Task (stateless ingestion):


# Deploy only the ingestion image, set these env vars on the job:
RUN_ONCE=true
DATABASE_URL=<cloud-db-url>
EJAGRITI_BASE_URL=https://e-jagriti.gov.in

# Schedule the job daily via Cloud Scheduler / EventBridge
# The container runs, completes the full pipeline, and exits 0
Smoke test without side effects:


DRY_RUN=true RUN_ONCE=true docker-compose run --rm ingestion
# Fetches from eJagriti API but writes nothing to DB
Add a read replica (zero code change):


# In .env, add:
REPLICA_DATABASE_URL=postgresql://user:pass@replica-host:5432/ejagriti
# All SELECT queries in the API automatically route there