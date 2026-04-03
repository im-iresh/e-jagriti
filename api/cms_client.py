"""
HTTP client for the complaint management system (CMS).

The CMS is the Samsung internal complaints portal that issues VOC numbers.
This client validates a VOC number exists and fetches its details, which are
then stored as raw_payload on the voc_complaints row.

Authentication:
  The caller's SSO bearer token is forwarded to the CMS unchanged.
  Both services trust the same central SSO — no separate CMS API key is needed.

Usage:
  from app import cms_client
  payload = cms_client.get_voc(voc_number=310256328, token=request.headers["Authorization"])

NOTE: Update _VOC_PATH when the real CMS endpoint path is confirmed.
"""

from __future__ import annotations

import structlog
import httpx

logger = structlog.get_logger(__name__)

# Placeholder — update when the real CMS endpoint path is confirmed.
_VOC_PATH = "/api/voc/complaints/{voc_number}"


class CMSClient:
    """Thin synchronous HTTP client for the complaint management system."""

    def __init__(self, base_url: str = "") -> None:
        self._base_url = base_url

    def configure(self, base_url: str) -> None:
        """Reconfigure the base URL after construction (called from create_app)."""
        self._base_url = base_url

    def get_voc(self, voc_number: int, token: str) -> dict:
        """
        Fetch VOC details from the CMS, forwarding the caller's SSO token.

        Args:
            voc_number: The VOC complaint number to look up.
            token: The caller's Authorization header value (e.g. "Bearer <jwt>").

        Returns:
            Parsed JSON response body from the CMS.

        Raises:
            LookupError: CMS returned 404 — VOC number does not exist.
            httpx.HTTPStatusError: Any other non-2xx response from the CMS.
            httpx.RequestError: Network-level failure (timeout, DNS, etc.).
        """
        url = _VOC_PATH.format(voc_number=voc_number)
        log = logger.bind(voc_number=voc_number)

        log.debug("cms_get_voc_start", url=url)
        with httpx.Client(base_url=self._base_url, timeout=10) as client:
            resp = client.get(url, headers={"Authorization": token})

        if resp.status_code == 404:
            log.info("cms_voc_not_found")
            raise LookupError(f"VOC {voc_number} not found in complaint management system")

        resp.raise_for_status()
        log.debug("cms_get_voc_ok", status=resp.status_code)
        return resp.json()
