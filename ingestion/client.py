"""
HTTP client for the eJagriti portal.

Features:
- User-Agent rotation (realistic Chrome/Firefox strings)
- Exponential backoff with jitter on 429 / 503 / network errors
- threading.Semaphore cap on max concurrent requests
- Structured logging of every call (endpoint, status, duration, retries)
- Raises PermissionError on 403 (caller should log to failed_jobs and skip)
- Raises RuntimeError after retries are exhausted
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# User-Agent pool — realistic browser strings to rotate across requests
# ---------------------------------------------------------------------------

_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
]

# Statuses that warrant a retry with exponential backoff
_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 503, 502, 504})


def _jitter(base_seconds: float, jitter_fraction: float = 0.2) -> float:
    """Return base_seconds ± jitter_fraction as a random float.

    Args:
        base_seconds: Nominal wait time.
        jitter_fraction: Maximum fractional deviation (default ±20%).

    Returns:
        A float in [base * (1 - jitter_fraction), base * (1 + jitter_fraction)].
    """
    delta = base_seconds * jitter_fraction
    return base_seconds + random.uniform(-delta, delta)


class EJagritiClient:
    """
    Synchronous HTTP client for the eJagriti portal.

    Uses httpx.Client under the hood. Thread-safe: multiple threads can
    share one instance; the semaphore caps concurrency.

    Args:
        base_url: Root URL (e.g. ``https://e-jagriti.gov.in/services``).
        max_concurrent: Maximum simultaneous in-flight requests.
        max_retries: Maximum retry attempts per request before raising.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        max_concurrent: int = 2,
        max_retries: int = 5,
        timeout: float = 30.0,
    ) -> None:
        """Initialise client with rate-limiting semaphore and httpx session."""
        self._base_url = base_url.rstrip("/")
        self._semaphore = threading.Semaphore(max_concurrent)
        self._max_retries = max_retries
        self._timeout = timeout
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _pick_user_agent(self) -> str:
        """Return a random User-Agent string from the pool."""
        return random.choice(_USER_AGENTS)

    def _build_headers(self) -> dict[str, str]:
        """Build realistic browser-like request headers."""
        ua = self._pick_user_agent()
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "DNT": "1",
            "Referer": "https://e-jagriti.gov.in/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": ua,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        Make a GET request, returning the parsed JSON body.

        Retries on retryable HTTP statuses and network errors using
        exponential backoff with ±20% jitter.  Raises immediately on
        HTTP 403 (permanent access denial).

        Args:
            path: URL path relative to base_url (e.g. ``/master/master/v2/getAllCommission``).
            params: Optional query parameters dict.

        Returns:
            Parsed JSON response (dict or list).

        Raises:
            PermissionError: On HTTP 403.
            RuntimeError: After all retries exhausted.
        """
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        headers: dict[str, str] = {}

        for attempt in range(self._max_retries + 1):
            headers = self._build_headers()
            start = time.monotonic()

            with self._semaphore:
                try:
                    response = self._http.get(url, params=params, headers=headers)
                    duration_ms = int((time.monotonic() - start) * 1000)

                    logger.info(
                        "http_call",
                        endpoint=path,
                        response_code=response.status_code,
                        duration_ms=duration_ms,
                        attempt=attempt,
                        user_agent=headers.get("User-Agent", "")[:60],
                    )

                    if response.status_code == 403:
                        logger.error("http_403_forbidden", endpoint=path)
                        raise PermissionError(f"403 Forbidden: {url}")

                    if response.status_code in _RETRYABLE_STATUSES:
                        wait = _jitter(2.0 ** attempt)
                        logger.warning(
                            "http_retryable",
                            endpoint=path,
                            status=response.status_code,
                            attempt=attempt,
                            wait_seconds=round(wait, 2),
                        )
                        time.sleep(wait)
                        continue

                    response.raise_for_status()
                    return response.json()

                except PermissionError:
                    raise

                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    wait = _jitter(2.0 ** attempt)
                    logger.warning(
                        "http_network_error",
                        endpoint=path,
                        error=str(exc),
                        attempt=attempt,
                        wait_seconds=round(wait, 2),
                    )
                    last_exc = exc
                    time.sleep(wait)
                    continue

                except httpx.HTTPStatusError as exc:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    body_preview = exc.response.text[:1000] if exc.response.content else ""
                    logger.error(
                        "http_status_error",
                        endpoint=path,
                        status=exc.response.status_code,
                        attempt=attempt,
                        response_body=body_preview,
                    )
                    last_exc = exc
                    # Non-retryable status — do not retry
                    break

        if last_exc is not None:
            raise RuntimeError(
                f"Non-retryable HTTP error for {url}"
            ) from last_exc
        raise RuntimeError(
            f"Exhausted {self._max_retries} retries for {url}"
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying httpx client and release connections."""
        self._http.close()

    def __enter__(self) -> "EJagritiClient":
        """Support use as a context manager."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Close client on context manager exit."""
        self.close()


# ---------------------------------------------------------------------------
# Module-level helper for call-interval calculation
# ---------------------------------------------------------------------------

def calculate_interval(daily_budget: int, jitter_fraction: float = 0.20) -> float:
    """
    Calculate sleep duration between API calls to stay within a daily budget.

    Applies ±jitter_fraction random deviation to avoid fingerprinting.

    Args:
        daily_budget: Target number of calls in a 24-hour window.
        jitter_fraction: Fractional deviation to add (default ±20%).

    Returns:
        Sleep duration in seconds for the current call interval.
    """
    base = 86400.0 / max(daily_budget, 1)
    return _jitter(base, jitter_fraction)
