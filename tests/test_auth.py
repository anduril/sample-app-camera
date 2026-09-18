"""AuthProvider: static token precedence and an OAuth refresh that never blocks
callers holding a valid cached token (the lock is not held across the POST)."""

from __future__ import annotations

import threading
import time

from lattice_cam.config import Config
from lattice_cam.lattice.auth import AuthProvider


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


class BlockingHttp:
    """post() blocks until released, so a refresh can be observed in flight."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.posts = 0

    def post(self, url, *, content, timeout):
        self.posts += 1
        self.entered.set()
        assert self.release.wait(5.0), "test never released the token POST"
        return FakeResponse(200, {"access_token": "fresh", "expires_in": 3600})


def _oauth_config() -> Config:
    return Config(
        lattice_endpoint="example.test",
        client_id="id",
        client_secret="secret",
        sandboxes_token="sbx",
    )


def test_static_token_wins_and_sandbox_header_is_additive():
    cfg = _oauth_config()
    cfg.environment_token = "static"
    http = BlockingHttp()
    headers = AuthProvider(cfg, http).headers()
    assert headers == {
        "authorization": "Bearer static",
        "anduril-sandbox-authorization": "Bearer sbx",
    }
    assert http.posts == 0


def test_oauth_refresh_stores_the_token():
    http = BlockingHttp()
    http.release.set()
    provider = AuthProvider(_oauth_config(), http)
    assert provider.headers()["authorization"] == "Bearer fresh"
    assert provider.headers()["authorization"] == "Bearer fresh"
    assert http.posts == 1  # cached on the second call


def test_refresh_in_flight_does_not_block_callers_with_a_valid_cache():
    http = BlockingHttp()
    provider = AuthProvider(_oauth_config(), http)

    results: dict[str, str] = {}

    def refresher():
        results["refresher"] = provider.headers()["authorization"]

    refresh_thread = threading.Thread(target=refresher)
    refresh_thread.start()
    assert http.entered.wait(5.0)  # the POST is in flight and must not hold the lock

    # Simulate the cache having been filled meanwhile (e.g. by another refresh).
    with provider._lock:
        provider._access_token = "cached"
        provider._expiry_monotonic = time.monotonic() + 1000

    def reader():
        results["reader"] = provider.headers()["authorization"]

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    reader_thread.join(1.0)
    assert not reader_thread.is_alive(), "headers() blocked behind the in-flight refresh"
    assert results["reader"] == "Bearer cached"

    http.release.set()
    refresh_thread.join(5.0)
    assert results["refresher"] == "Bearer fresh"
