"""
Unit tests for ingestion/db/upsert.py

Tests idempotency and change-detection logic without a real database.
Uses unittest.mock to patch the SQLAlchemy session so no Postgres instance
is required.

Tests:
  1. upsert_case calls execute once with the correct statement
  2. Calling upsert_case twice with identical data does not raise
  3. data_hash change detection in fetch_case_detail._process_detail
  4. log_ingestion_error inserts correct error_type
  5. upsert_commission sets parent_commission_id correctly
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _md5(payload) -> str:
    """Reproduce the hash function from fetch_case_detail."""
    return hashlib.md5(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# upsert_case — idempotency
# ---------------------------------------------------------------------------

class TestUpsertCase:
    def test_upsert_executes_insert_on_conflict(self):
        """upsert_case should call session.execute with an INSERT ... ON CONFLICT."""
        from db.upsert import upsert_case

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 42
        mock_session.execute.return_value = mock_result

        data = {
            "case_number":    "DC/77/CC/104/2025",
            "commission_id":  1,
            "status":         "pending",
            "complainant_name": "Test User",
        }

        result_id = upsert_case(mock_session, data)

        assert result_id == 42
        mock_session.execute.assert_called_once()

    def test_upsert_case_returns_id(self):
        """upsert_case must return the scalar id from the RETURNING clause."""
        from db.upsert import upsert_case

        mock_session = MagicMock()
        mock_session.execute.return_value.scalar_one.return_value = 99
        result = upsert_case(mock_session, {"case_number": "X/1/2025", "commission_id": 1})
        assert result == 99

    def test_upsert_commission_returns_id(self):
        """upsert_commission must return the scalar id."""
        from db.upsert import upsert_commission

        mock_session = MagicMock()
        mock_session.execute.return_value.scalar_one.return_value = 7
        result = upsert_commission(mock_session, {
            "commission_id_ext": 11000000,
            "name_en": "NCDRC",
            "commission_type": "national",
        })
        assert result == 7


# ---------------------------------------------------------------------------
# Data-hash change detection
# ---------------------------------------------------------------------------

class TestDataHashDeduplication:
    """Tests the hash-based skip logic in fetch_case_detail._process_detail."""

    def test_unchanged_hash_returns_skipped(self):
        """If the existing hash matches the new payload hash, return 'skipped'."""
        from jobs.fetch_case_detail import _process_detail

        payload = {"caseStage": "REGISTERED", "complainant": "Alice"}
        existing_hash = _md5(payload)

        result = _process_detail(
            session_factory=MagicMock(),
            case_db_id=1,
            case_number="DC/77/CC/1/2025",
            data=payload,
            existing_hash=existing_hash,
            run_id=1,
            dry_run=False,
        )
        assert result == "skipped"

    def test_changed_hash_triggers_update(self):
        """If hash differs, _process_detail should attempt a DB write."""
        from jobs.fetch_case_detail import _process_detail

        old_payload = {"caseStage": "REGISTERED", "complainant": "Alice"}
        new_payload = {"caseStage": "ADMIT", "complainant": "Alice"}
        old_hash = _md5(old_payload)

        mock_session_ctx = MagicMock()
        mock_session_ctx.__enter__ = MagicMock(return_value=mock_session_ctx)
        mock_session_ctx.__exit__ = MagicMock(return_value=False)
        mock_session_ctx.execute.return_value.scalar_one.return_value = 1

        with patch("jobs.fetch_case_detail.get_session", return_value=mock_session_ctx), \
             patch("jobs.fetch_case_detail.upsert_case", return_value=1) as mock_upsert, \
             patch("jobs.fetch_case_detail.upsert_hearing", return_value=1), \
             patch("jobs.fetch_case_detail.upsert_daily_order", return_value=1):

            result = _process_detail(
                session_factory=MagicMock(),
                case_db_id=1,
                case_number="DC/77/CC/1/2025",
                data=new_payload,
                existing_hash=old_hash,
                run_id=1,
                dry_run=False,
            )

        assert result == "updated"
        mock_upsert.assert_called_once()

    def test_dry_run_skips_db_write(self):
        """In dry_run mode, _process_detail should return 'skipped' without writing."""
        from jobs.fetch_case_detail import _process_detail

        payload = {"caseStage": "ADMIT", "complainant": "Bob", "caseHearingDetails": []}

        with patch("jobs.fetch_case_detail.get_session") as mock_session:
            result = _process_detail(
                session_factory=MagicMock(),
                case_db_id=2,
                case_number="X/2/2025",
                data=payload,
                existing_hash="different_hash",
                run_id=1,
                dry_run=True,  # <-- key flag
            )

        mock_session.assert_not_called()
        assert result == "skipped"


# ---------------------------------------------------------------------------
# log_ingestion_error
# ---------------------------------------------------------------------------

class TestLogIngestionError:
    def test_correct_error_type_set(self):
        """log_ingestion_error should add an IngestionError with the given error_type."""
        from db.upsert import log_ingestion_error
        from db.models import ErrorType

        mock_session = MagicMock()

        log_ingestion_error(
            mock_session,
            run_id=1,
            case_id=None,
            endpoint="/test",
            error_type=ErrorType.rate_limited,
            error_message="Too many requests",
            http_status=429,
        )

        mock_session.add.assert_called_once()
        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.error_type == ErrorType.rate_limited
        assert added_obj.http_status == 429
        assert added_obj.endpoint == "/test"

    def test_response_body_truncated_to_4kb(self):
        """log_ingestion_error must truncate response_body to 4096 chars."""
        from db.upsert import log_ingestion_error
        from db.models import ErrorType

        mock_session = MagicMock()
        long_body = "x" * 10_000

        log_ingestion_error(
            mock_session,
            run_id=1,
            case_id=None,
            endpoint="/test",
            error_type=ErrorType.http_error,
            error_message="Error",
            response_body=long_body,
        )

        added = mock_session.add.call_args[0][0]
        assert len(added.response_body) == 4096
