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
_ingestion_path = os.path.join(os.path.dirname(__file__), "..", "ingestion")
if _ingestion_path not in sys.path:
    sys.path.insert(0, os.path.abspath(_ingestion_path))

from db.models import (  # noqa: E402 — must come after sys.path update
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
)

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
]
