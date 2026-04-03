"""
CMS HTTP client for the ingestion service.

Wraps CMSTokenManager to add an Authorization header to every request and
automatically refreshes the token + retries once when the CMS responds with
a status code in REFRESH_ON_STATUSES (401 or 404).

Usage:
  from services.cms_client import CMSIngestionClient

  client = CMSIngestionClient(base_url=os.environ["EJAGRITI_CMS_BASE_URL"])
  records = client.get_voc_list()

NOTE: Endpoint paths below are placeholders. Update them when the real CMS
API routes are confirmed — the client logic itself does not need to change.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

from services.cms_token_manager import REFRESH_ON_STATUSES, CMSTokenManager

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Endpoint paths — update when real CMS routes are confirmed
# ---------------------------------------------------------------------------

_VOC_LIST_PATH = "/api/voc/complaints"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CMSIngestionClient:
    """
    HTTP client for the complaint management system, used by ingestion jobs.

    Auth is handled by CMSTokenManager (service-account credentials, not a
    user token). On 401 or 404 the token is refreshed and the request is
    retried exactly once.

    Args:
        base_url: CMS base URL, e.g. ``https://cms.example.com``.
                  Defaults to the EJAGRITI_CMS_BASE_URL env var.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or os.environ.get("EJAGRITI_CMS_BASE_URL", "")).rstrip("/")
        self._token_mgr = CMSTokenManager.get_instance()

    # ------------------------------------------------------------------
    # Public convenience methods
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET request to the CMS with automatic token refresh on expiry."""
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        """POST request to the CMS with automatic token refresh on expiry."""
        return self._request("POST", path, json=body)

    def get_voc_list(self) -> list[dict[str, Any]]:
        """
        Fetch all VOC complaints from the CMS.

        Returns:
            List of VOC complaint dicts.

        NOTE: Replace _VOC_LIST_PATH when the real endpoint path is confirmed.
        When the real API is ready, also replace _fetch_voc_data() in
        ingestion/jobs/fetch_voc.py to call this method instead of returning
        dummy data.
        """
        data = self.get(_VOC_LIST_PATH)
        if isinstance(data, list):
            return data
        # Common envelope pattern: { "data": [...] }
        return data.get("data", []) if isinstance(data, dict) else []

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """
        Execute an HTTP request, refreshing the token and retrying once on
        REFRESH_ON_STATUSES (401 or 404).

        Args:
            method: HTTP method string (GET, POST, etc.).
            path:   URL path relative to base_url.
            **kwargs: Passed directly to httpx.Client.request().

        Returns:
            Parsed JSON response body.

        Raises:
            httpx.HTTPStatusError: Non-2xx response after retry.
            httpx.RequestError:    Network-level failure.
        """
        url = f"{self._base_url}{path}"

        resp = self._do_request(method, url, self._token_mgr.get_token(), **kwargs)

        if resp.status_code in REFRESH_ON_STATUSES:
            logger.warning(
                "cms_token_expired_refreshing",
                status=resp.status_code,
                method=method,
                path=path,
            )
            new_token = self._token_mgr.refresh()
            resp = self._do_request(method, url, new_token, **kwargs)

        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _do_request(
        method: str,
        url: str,
        token: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue a single HTTP request with the given bearer token."""
        with httpx.Client(timeout=15) as client:
            return client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
