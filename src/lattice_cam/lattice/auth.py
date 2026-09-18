"""Authentication for the Connect client.

Supports both authentication modes and chooses between them dynamically:

* **Static bearer token** (``ENVIRONMENT_TOKEN``) — preferred when present.
* **OAuth2 client-credentials** (``CLIENT_ID`` + ``CLIENT_SECRET``) — exchanged
  for a short-lived access token that we fetch, cache, and refresh ourselves.

If both are configured, the static bearer token wins.

Unlike the REST SDK, gRPC/Connect auth is manual: we attach the credentials as
lowercase request metadata on every call. For a Sandbox the account-level
``SANDBOXES_TOKEN`` is additive and sent alongside the primary credential.

The token cache follows the repo rule "never hold a lock across an RPC": the
lock guards only the cached token and its expiry, and the token POST runs
outside it, so the 1 Hz publisher is never blocked behind a refresh.
"""

from __future__ import annotations

import json
import threading
import time

from ..config import Config
from ..logging_setup import get_logger

logger = get_logger(__name__)

# Refresh an OAuth token this many seconds before it actually expires.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0


class AuthError(Exception):
    """Raised when credentials cannot be obtained."""


class AuthProvider:
    """Produces per-call auth metadata, managing OAuth token lifecycle."""

    def __init__(self, config: Config, http_client) -> None:
        self._config = config
        self._http = http_client
        self._lock = threading.Lock()
        self._access_token: str = ""
        self._expiry_monotonic: float = 0.0

    def headers(self) -> dict[str, str]:
        """Return the auth metadata to attach to a Connect request."""
        headers = {"authorization": f"Bearer {self._bearer_token()}"}
        if self._config.sandboxes_token:
            headers["anduril-sandbox-authorization"] = f"Bearer {self._config.sandboxes_token}"
        return headers

    def _bearer_token(self) -> str:
        # Static token is preferred when present.
        if self._config.environment_token:
            return self._config.environment_token
        return self._oauth_access_token()

    def _oauth_access_token(self) -> str:
        with self._lock:
            if self._access_token and time.monotonic() < self._expiry_monotonic:
                return self._access_token

        # Refresh outside the lock. Two threads that both find the cache
        # expired may both fetch; the second result simply replaces the first,
        # which is harmless and far cheaper than stalling every caller.
        token, expires_in = self._fetch_token()
        with self._lock:
            self._access_token = token
            self._expiry_monotonic = time.monotonic() + max(
                0.0, expires_in - _TOKEN_REFRESH_MARGIN_SECONDS
            )
        logger.debug("obtained OAuth access token", expires_in=expires_in)
        return token

    def _fetch_token(self) -> tuple[str, float]:
        """Exchange the client credentials for an access token and its lifetime."""
        url = f"https://{self._config.lattice_endpoint}/api/v1/oauth/token"
        body = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        }
        try:
            resp = self._http.post(url, content=body, timeout=30.0)
        except Exception as exc:  # network / TLS failures
            raise AuthError(f"OAuth token request failed: {exc}") from exc

        if resp.status != 200:
            # Never log the response body: it may echo credentials.
            raise AuthError(f"OAuth token endpoint returned HTTP {resp.status}")

        data = _parse_json(resp)
        token = data.get("access_token")
        if not token:
            raise AuthError("OAuth response did not contain an access_token")
        return token, float(data.get("expires_in", 3600))


def _parse_json(resp) -> dict:
    # FullResponse exposes .json() on newer pyqwest; fall back to raw content.
    try:
        result = resp.json()
        return result() if callable(result) else result
    except Exception:
        return json.loads(bytes(resp.content))
