"""
Re-export all ORM models for the API service.

The canonical model definitions live in ingestion/db/models.py.
This shim allows the API container to import from a consistent local path
(``from models import Case``) without duplicating the model code.

The ingestion package is made available by adding it to sys.path in the
Docker image or via PYTHONPATH.
"""

from __future__ import annotations

import os
import sys

# Ensure the ingestion package is importable.
# In Docker this is guaranteed by COPY in the Dockerfile; this fallback
# handles local development and test runs from the repo root.
_ingestion_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ingestion"))
if _ingestion_path not in sys.path:
    sys.path.insert(0, _ingestion_path)

# api/db/ may already be registered in sys.modules as the 'db' package because
# api/db/queries.py is imported before this shim runs. Temporarily clear that
# entry so that 'from db.models import ...' resolves against ingestion/db/,
# then restore api/db so that subsequent api imports are unaffected.
_saved_db = {k: v for k, v in sys.modules.items() if k == "db" or k.startswith("db.")}
for _k in _saved_db:
    del sys.modules[_k]

from db.models import (  # noqa: E402 — must come after sys.path and sys.modules fixup
    ApiCallLog,
    Base,
    Case,
    CaseStatus,
    Commission,
    CommissionType,
    DailyOrder,
    ErrorType,
    FailedJob,
    Hearing,
    IngestionError,
    IngestionRun,
    JobType,
    TriggerMode,
    VocComplaint,
    VocMatchStatus,
)

# Restore api/db so that api/db/queries.py, api/db/session.py, etc. continue
# to be importable as 'db.*' from the rest of the API codebase.
sys.modules.update(_saved_db)

__all__ = [
    "ApiCallLog",
    "Base",
    "Case",
    "CaseStatus",
    "Commission",
    "CommissionType",
    "DailyOrder",
    "ErrorType",
    "FailedJob",
    "Hearing",
    "IngestionError",
    "IngestionRun",
    "JobType",
    "TriggerMode",
    "VocComplaint",
    "VocMatchStatus",
]
