"""
CMS service-account token manager.

Manages a bearer token for authenticating with the complaint management
system (CMS) from the ingestion service. Unlike the API service (which
forwards the end-user's SSO token), the ingestion service authenticates
with a dedicated service account whose credentials are stored in env vars.

Thread-safety:
  A double-checked locking singleton ensures exactly one CMSTokenManager
  exists per process. An instance-level lock serialises token fetches so
  two threads that simultaneously find the token missing or expired will
  not both hit the SSO endpoint — only the first will; the second waits
  and reuses the freshly obtained token.

Usage:
  from services.cms_token_manager import CMSTokenManager

  token = CMSTokenManager.get_instance().get_token()

  # After a CMS call returns 401 or 404:
  token = CMSTokenManager.get_instance().refresh()
"""

from __future__ import annotations

import os
import threading
from typing import ClassVar

import httpx
import structlog

logger = structlog.get_logger(__name__)

# HTTP status codes from the CMS that indicate the token is expired/invalid.
# 401 is the standard; 404 is included per CMS behaviour observed in this project.
REFRESH_ON_STATUSES: frozenset[int] = frozenset({401, 404})


class CMSTokenManager:
    """
    Thread-safe singleton that manages a CMS service-account bearer token.

    Token is fetched lazily on the first ``get_token()`` call and cached
    for the lifetime of the process. Call ``refresh()`` to force a new
    fetch (e.g. after receiving 401 or 404 from the CMS).

    Env vars consumed:
      EJAGRITI_CMS_SSO_URL   — SSO login endpoint (POST, credentials in JSON body)
      EJAGRITI_CMS_USERNAME  — service account username
      EJAGRITI_CMS_PASSWORD  — service account password

    The response is expected to contain a ``token`` or ``access_token`` key.
    Update ``_fetch()`` if the real SSO response shape differs.
    """

    _instance: ClassVar[CMSTokenManager | None] = None
    _class_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_lock = threading.Lock()
        # Read credentials at construction time so they can be set in .env
        # before the first get_instance() call.
        self._sso_url  = os.environ.get("EJAGRITI_CMS_SSO_URL", "")
        self._username = os.environ.get("EJAGRITI_CMS_USERNAME", "")
        self._password = os.environ.get("EJAGRITI_CMS_PASSWORD", "")

    # ------------------------------------------------------------------
    # Singleton factory
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> CMSTokenManager:
        """Return the process-wide singleton, creating it on first call."""
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:   # double-checked locking
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """
        Return the cached bearer token, fetching from SSO if not yet obtained.

        Thread-safe: concurrent callers on a cold cache will block until
        the first fetch completes, then all share the same token.

        Returns:
            Bearer token string (without the ``Bearer `` prefix).

        Raises:
            RuntimeError: SSO login failed or response did not contain a token.
        """
        with self._token_lock:
            if self._token is None:
                self._token = self._fetch()
            return self._token

    def refresh(self) -> str:
        """
        Force a new token fetch and cache the result.

        Call this after a CMS response returns a status in
        REFRESH_ON_STATUSES (401 or 404), then retry the CMS request.

        Returns:
            Newly obtained bearer token string.

        Raises:
            RuntimeError: SSO login failed or response did not contain a token.
        """
        with self._token_lock:
            logger.info("cms_token_refresh")
            self._token = self._fetch()
            return self._token

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch(self) -> str:
        """
        POST credentials to the SSO login endpoint and extract the token.

        Must be called with ``_token_lock`` held.

        Returns:
            Raw token string from the SSO response.

        Raises:
            RuntimeError: HTTP error or token key not found in response.
        """
        logger.info("cms_token_fetch", sso_url=self._sso_url)
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    self._sso_url,
                    json={"username": self._username, "password": self._password},
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"CMS SSO login failed: {exc}") from exc

        data = resp.json()
        # Support both common token key names; update if the real SSO differs.
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RuntimeError(
                f"CMS SSO response missing token key. Got keys: {list(data.keys())}"
            )

        logger.info("cms_token_fetched_ok")
        return str(token)
