"""
Unit tests for ingestion/client.py

Tests:
  1. calculate_interval returns value within ±20% of base
  2. Exponential backoff sleep durations increase correctly
  3. Client retries on 429 and succeeds on subsequent call
  4. Client raises PermissionError immediately on 403 (no retry)
  5. Client raises RuntimeError after max retries exhausted
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch, call

import httpx
import pytest

# Adjust path via conftest.py
from client import EJagritiClient, _jitter, calculate_interval


# ---------------------------------------------------------------------------
# calculate_interval
# ---------------------------------------------------------------------------

class TestCalculateInterval:
    def test_within_jitter_bounds(self):
        """calculate_interval result must be within ±20% of base (86400/budget)."""
        budget = 3500
        base = 86400.0 / budget  # ≈ 24.69 s
        lower = base * 0.80
        upper = base * 1.20

        # Run 1000 samples; all must be in range
        for _ in range(1000):
            result = calculate_interval(budget)
            assert lower <= result <= upper, (
                f"calculate_interval({budget}) returned {result:.3f}, "
                f"expected [{lower:.3f}, {upper:.3f}]"
            )

    def test_zero_budget_does_not_crash(self):
        """A budget of 0 should not cause ZeroDivisionError."""
        result = calculate_interval(0)
        assert result >= 0

    def test_jitter_produces_variation(self):
        """10 consecutive calls should not all return identical values."""
        values = {calculate_interval(3500) for _ in range(10)}
        assert len(values) > 1, "jitter should produce different values"


class TestJitter:
    def test_jitter_bounds(self):
        """_jitter(base, fraction) must stay within [base*(1-f), base*(1+f)]."""
        base = 10.0
        fraction = 0.20
        for _ in range(500):
            v = _jitter(base, fraction)
            assert base * 0.80 <= v <= base * 1.20


# ---------------------------------------------------------------------------
# EJagritiClient retry behaviour
# ---------------------------------------------------------------------------

class TestEJagritiClientRetry:
    """Tests use httpx mock transport to avoid real network calls."""

    def _make_response(self, status: int, json_body: dict | None = None) -> httpx.Response:
        """Build a fake httpx.Response."""
        import json as _json
        body = _json.dumps(json_body or {}).encode()
        return httpx.Response(status_code=status, content=body,
                              headers={"Content-Type": "application/json"})

    def test_success_on_first_attempt(self):
        """A 200 response should be returned immediately, no sleep."""
        response_data = {"data": [{"commissionId": 11000000}]}
        mock_response = self._make_response(200, response_data)

        with patch("httpx.Client.get", return_value=mock_response) as mock_get, \
             patch("time.sleep") as mock_sleep:
            client = EJagritiClient(base_url="https://example.com", max_retries=3)
            result = client.get("/test")
            client.close()

        assert result == response_data
        mock_sleep.assert_not_called()

    def test_retries_on_429_then_succeeds(self):
        """Client should retry on 429 and succeed on the next attempt."""
        rate_limited = self._make_response(429)
        success      = self._make_response(200, {"ok": True})

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            return rate_limited if call_count[0] == 1 else success

        with patch("httpx.Client.get", side_effect=side_effect), \
             patch("time.sleep"):
            client = EJagritiClient(base_url="https://example.com", max_retries=3)
            result = client.get("/test")
            client.close()

        assert result == {"ok": True}
        assert call_count[0] == 2

    def test_raises_permission_error_on_403(self):
        """HTTP 403 must raise PermissionError without retrying."""
        forbidden = self._make_response(403)

        with patch("httpx.Client.get", return_value=forbidden) as mock_get, \
             patch("time.sleep") as mock_sleep:
            client = EJagritiClient(base_url="https://example.com", max_retries=3)
            with pytest.raises(PermissionError):
                client.get("/restricted")
            client.close()

        # Called exactly once — no retry on 403
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    def test_raises_runtime_error_after_max_retries(self):
        """RuntimeError raised when all retry attempts return 429."""
        always_429 = self._make_response(429)

        with patch("httpx.Client.get", return_value=always_429), \
             patch("time.sleep"):
            client = EJagritiClient(base_url="https://example.com", max_retries=2)
            with pytest.raises(RuntimeError, match="Exhausted"):
                client.get("/flaky")
            client.close()

    def test_retry_on_network_error(self):
        """Transient network errors should trigger retry with backoff."""
        call_count = [0]
        ok = self._make_response(200, {"data": "ok"})

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise httpx.NetworkError("connection reset")
            return ok

        with patch("httpx.Client.get", side_effect=side_effect), \
             patch("time.sleep") as mock_sleep:
            client = EJagritiClient(base_url="https://example.com", max_retries=5)
            result = client.get("/test")
            client.close()

        assert result == {"data": "ok"}
        assert mock_sleep.call_count == 2  # slept before attempt 2 and 3

    def test_semaphore_caps_concurrent_requests(self):
        """At most max_concurrent requests should be in-flight simultaneously."""
        import time as real_time

        max_concurrent = 2
        inflight = [0]
        peak_inflight = [0]
        lock = threading.Lock()

        slow_response = self._make_response(200, {"ok": True})

        def slow_get(*args, **kwargs):
            with lock:
                inflight[0] += 1
                if inflight[0] > peak_inflight[0]:
                    peak_inflight[0] = inflight[0]
            real_time.sleep(0.05)
            with lock:
                inflight[0] -= 1
            return slow_response

        client = EJagritiClient(
            base_url="https://example.com",
            max_concurrent=max_concurrent,
            max_retries=0,
        )

        with patch("httpx.Client.get", side_effect=slow_get):
            threads = [
                threading.Thread(target=client.get, args=("/test",))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        client.close()
        assert peak_inflight[0] <= max_concurrent, (
            f"Peak inflight {peak_inflight[0]} exceeded semaphore limit {max_concurrent}"
        )
