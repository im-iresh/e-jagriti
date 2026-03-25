"""
Unit tests for Flask API routes.

Uses the Flask test client from conftest.py.
All DB queries are mocked so no database is required.

Tests:
  1. GET /api/cases returns 200 with paginated envelope
  2. GET /api/cases?status=invalid returns 400
  3. GET /api/cases/:id returns 200 with nested case object
  4. GET /api/cases/:id returns 404 when case not found
  5. GET /api/cases/:id/orders returns 200 with paginated orders
  6. GET /api/cases/:id/orders returns 404 when case not found
  7. GET /api/cases/:id/judgment returns 200
  8. GET /api/commissions returns 200 (cached)
  9. GET /api/stats returns 200 with expected fields
  10. GET /health returns 200 when DB is healthy
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# /api/cases — list
# ---------------------------------------------------------------------------

class TestCasesList:
    def test_returns_200_paginated_envelope(self, client):
        """GET /api/cases should return success envelope with pagination meta."""
        mock_data = {
            "items": [
                {
                    "case_id": 1, "case_number": "DC/77/CC/1/2025",
                    "complainant_name": "Alice", "commission_name": "Central Delhi",
                    "commission_type": "district", "filing_date": "2025-01-01",
                    "date_of_next_hearing": None, "status": "pending",
                    "case_stage": "REGISTERED", "last_updated": "2025-01-02T00:00:00",
                }
            ],
            "total": 1,
        }
        with patch("routes.cases.get_cases_paginated", return_value=mock_data):
            resp = client.get("/api/cases")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert isinstance(body["data"], list)
        assert body["meta"]["pagination"]["total"] == 1
        assert body["meta"]["pagination"]["page"] == 1

    def test_invalid_status_returns_400(self, client):
        """An unrecognised status param must return 400."""
        resp = client.get("/api/cases?status=unknown_status")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert body["error"]["code"] == "INVALID_STATUS"

    def test_invalid_page_param_returns_400(self, client):
        """Non-integer page param must return 400."""
        resp = client.get("/api/cases?page=abc")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False

    def test_per_page_capped_at_100(self, client):
        """per_page > 100 should be silently capped at 100."""
        mock_data = {"items": [], "total": 0}
        with patch("routes.cases.get_cases_paginated", return_value=mock_data) as mock_q:
            resp = client.get("/api/cases?per_page=999")
        assert resp.status_code == 200
        # Verify the query was called with per_page=100
        _, kwargs = mock_q.call_args
        assert mock_q.call_args[1]["per_page"] == 100 or mock_q.call_args[0][1] == 100


# ---------------------------------------------------------------------------
# /api/cases/:id — detail
# ---------------------------------------------------------------------------

class TestCaseDetail:
    _MOCK_CASE = {
        "case_id": 42,
        "case_number": "DC/77/CC/42/2025",
        "filing_date": "2025-03-01",
        "date_of_cause": None,
        "status": "open",
        "case_stage": "REGISTERED",
        "case_category": "ELECTRONICS",
        "date_of_next_hearing": "2026-01-01",
        "commission": {"id": 1, "ext_id": 11070077, "name": "Central Delhi", "type": "district", "state_id": 7},
        "complainant": {"name": "Bob", "advocate_names": []},
        "respondent": {"name": "SAMSUNG INDIA ELECTRONICS", "advocate_names": []},
        "hearings": [],
        "daily_orders": [],
        "last_fetched_at": "2025-03-02T00:00:00",
    }

    def test_returns_200_nested_case(self, client):
        """GET /api/cases/<id> should return the full nested case object."""
        with patch("routes.cases.get_case_by_id", return_value=self._MOCK_CASE):
            resp = client.get("/api/cases/42")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["data"]["case_number"] == "DC/77/CC/42/2025"
        assert "commission" in body["data"]
        assert "hearings" in body["data"]

    def test_returns_404_when_not_found(self, client):
        """GET /api/cases/<id> should return 404 when case does not exist."""
        with patch("routes.cases.get_case_by_id", return_value=None):
            resp = client.get("/api/cases/9999")

        assert resp.status_code == 404
        body = resp.get_json()
        assert body["success"] is False
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# /api/cases/:id/orders
# ---------------------------------------------------------------------------

class TestCaseOrders:
    def test_returns_200_with_orders(self, client):
        """GET /api/cases/<id>/orders should return paginated order list."""
        mock_data = {
            "items": [
                {
                    "id": 1, "date": "2025-07-01", "order_type_id": 1,
                    "pdf_fetched": True, "pdf_storage_path": "/data/1.pdf",
                    "pdf_fetched_at": "2025-07-02T00:00:00", "pdf_fetch_error": None,
                }
            ],
            "total": 1,
        }
        with patch("routes.orders.get_orders_for_case", return_value=mock_data):
            resp = client.get("/api/cases/1/orders")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert len(body["data"]) == 1

    def test_returns_404_case_not_found(self, client):
        """GET /api/cases/<id>/orders returns 404 when case absent."""
        with patch("routes.orders.get_orders_for_case", return_value=None):
            resp = client.get("/api/cases/9999/orders")

        assert resp.status_code == 404

    def test_invalid_from_date_returns_400(self, client):
        """A malformed from_date param should return 400."""
        resp = client.get("/api/cases/1/orders?from_date=not-a-date")
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"]["code"] == "INVALID_DATE"


# ---------------------------------------------------------------------------
# /api/cases/:id/judgment
# ---------------------------------------------------------------------------

class TestCaseJudgment:
    def test_returns_200_with_judgment(self, client):
        """GET /api/cases/<id>/judgment returns judgment dict."""
        mock_judgment = {
            "id": 5, "date": "2025-12-01",
            "pdf_fetched": True, "pdf_storage_path": "/data/j.pdf",
            "pdf_fetched_at": "2025-12-02T00:00:00",
        }
        with patch("routes.judgments.get_judgment_for_case", return_value=mock_judgment):
            resp = client.get("/api/cases/1/judgment")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["data"]["pdf_fetched"] is True

    def test_returns_404_when_case_absent(self, client):
        """Judgment endpoint returns 404 when case does not exist."""
        with patch("routes.judgments.get_judgment_for_case", return_value=None):
            resp = client.get("/api/cases/9999/judgment")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/commissions
# ---------------------------------------------------------------------------

class TestCommissions:
    def test_returns_200_list(self, client):
        """GET /api/commissions should return list of commissions."""
        mock_commissions = [
            {"id": 1, "commission_id_ext": 11000000, "name": "NCDRC", "type": "national",
             "state_id": 0, "district_id": None, "case_prefix_text": None, "parent_commission_id": None},
        ]

        # Patch both cache.get (miss) and get_all_commissions
        with patch("routes.commissions.cache") as mock_cache, \
             patch("routes.commissions.get_all_commissions", return_value=mock_commissions):
            mock_cache.get.return_value = None  # Cache miss
            resp = client.get("/api/commissions")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert isinstance(body["data"], list)


# ---------------------------------------------------------------------------
# /api/stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_returns_200_with_expected_fields(self, client):
        """GET /api/stats returns aggregate stats with required top-level keys."""
        mock_stats = {
            "total_cases": 500,
            "open_cases": 300,
            "closed_cases": 150,
            "pending_cases": 50,
            "by_commission_type": {"national": 10, "state": 100, "district": 390},
            "cases_per_month": [{"month": "2025-01", "count": 20}],
            "last_ingestion_run": None,
        }
        with patch("routes.stats.cache") as mock_cache, \
             patch("routes.stats.get_stats", return_value=mock_stats):
            mock_cache.get.return_value = None
            resp = client.get("/api/stats")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert "total_cases" in body["data"]
        assert "by_commission_type" in body["data"]
        assert "cases_per_month" in body["data"]


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_healthy_returns_200(self, client):
        """GET /health returns 200 when DB is reachable."""
        with patch("routes.stats.get_health_data",
                   return_value={"db_ok": True, "last_ingestion_run": None}):
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["data"]["db_ok"] is True

    def test_unhealthy_returns_503(self, client):
        """GET /health returns 503 when DB is unreachable."""
        with patch("routes.stats.get_health_data",
                   return_value={"db_ok": False, "last_ingestion_run": None}):
            resp = client.get("/health")
        assert resp.status_code == 503
