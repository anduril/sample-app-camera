"""Tests for the media pipeline seam: shell commands and the MediaMTX probe.

No network and no MediaMTX: the probe takes an injectable ``opener`` and
``clock`` so its mapping, TTL cache, and failure handling are exercised with
canned API responses.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime

import pytest

from lattice_cam.camera.pipeline import (
    CommandPipeline,
    MediaMtxStatusProbe,
    PipelineError,
    PipelineStatus,
)

# --- CommandPipeline -----------------------------------------------------------


def test_command_pipeline_runs_start_and_stop(tmp_path):
    marker = tmp_path / "ran"
    pipeline = CommandPipeline(
        start_command=f"echo start >> '{marker}'", stop_command=f"echo stop >> '{marker}'"
    )
    pipeline.start("srt://x")
    pipeline.stop()
    assert marker.read_text().split() == ["start", "stop"]


def test_command_pipeline_exports_push_url(tmp_path):
    seen = tmp_path / "env"
    CommandPipeline(start_command=f"printf '%s' \"$SRT_TARGET\" > '{seen}'").start("srt://a?b=c")
    assert seen.read_text() == "srt://a?b=c"


def test_command_pipeline_failure_includes_exit_code_and_output():
    pipeline = CommandPipeline(start_command="echo oops >&2; exit 3", stop_command="exit 4")
    with pytest.raises(PipelineError, match="start command exited with 3: oops"):
        pipeline.start("srt://x")
    with pytest.raises(PipelineError, match="stop command exited with 4"):
        pipeline.stop()


def test_command_pipeline_truncates_output_to_last_500_chars():
    pipeline = CommandPipeline(start_command="python3 -c \"print('a'*600 + 'END')\"; exit 1")
    with pytest.raises(PipelineError) as excinfo:
        pipeline.start("")
    detail = str(excinfo.value).split(": ", 1)[1]
    assert len(detail) == 500 and detail.endswith("END")


def test_command_pipeline_timeout():
    pipeline = CommandPipeline(start_command="sleep 5", timeout_s=0.2)
    with pytest.raises(PipelineError, match="start command timed out after 0.2s"):
        pipeline.start("srt://x")


def test_command_pipeline_empty_command_is_a_noop():
    pipeline = CommandPipeline()
    pipeline.start("srt://x")  # nothing to run, nothing raised
    pipeline.stop()


def test_command_pipeline_status_without_probe_is_unknown():
    status = CommandPipeline().status()
    assert (status.ready, status.pushing) == (None, None)
    assert status.detail == "no status probe"
    assert status.observed_at.tzinfo is UTC


def test_command_pipeline_status_delegates_to_probe():
    probe = MediaMtxStatusProbe(opener=lambda url, timeout: _path(ready=True, readers=0))
    assert CommandPipeline(probe=probe).status().ready is True


# --- MediaMtxStatusProbe ---------------------------------------------------------


def _path(*, ready: bool, readers: int, received: int | None = 0) -> bytes:
    body = {
        "name": "cam",
        "confName": "cam",
        "ready": ready,
        "readers": [{"type": "srtConn", "id": f"r{i}"} for i in range(readers)],
    }
    if received is not None:
        body["bytesReceived"] = received
    return json.dumps(body).encode()


class FakeApi:
    """A scripted MediaMTX API: each call pops the next response (or exception)."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append((url, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _probe(*responses, ttl_s: float = 0.0, **kwargs):
    api = FakeApi(*responses)
    clock = FakeClock()
    probe = MediaMtxStatusProbe(opener=api, clock=clock, ttl_s=ttl_s, **kwargs)
    return probe, api, clock


def test_probe_requests_the_configured_path_with_the_short_timeout():
    probe, api, clock = _probe(_path(ready=True, readers=0), api_url="http://mtx:9997/", path="p")
    probe.status()
    assert api.calls == [("http://mtx:9997/v3/paths/get/p", 0.5)]


def test_probe_maps_ready_and_no_readers():
    probe, api, clock = _probe(_path(ready=True, readers=0), _path(ready=False, readers=0))
    first = probe.status()
    assert (first.ready, first.pushing) == (True, False)
    assert first.detail == "ready, no readers"
    second = probe.status()
    assert (second.ready, second.pushing) == (False, False)
    assert second.detail.startswith("not ready")


def test_probe_pushing_requires_readers_and_increasing_bytes():
    probe, api, clock = _probe(
        _path(ready=True, readers=1, received=100),  # first sample: no baseline
        _path(ready=True, readers=1, received=250),  # grew: pushing
        _path(ready=True, readers=1, received=250),  # stalled: not pushing
        _path(ready=True, readers=0, received=900),  # grew but nobody reads: not pushing
        _path(ready=True, readers=1, received=50),  # counter reset (restart): unknown
        _path(ready=True, readers=1, received=60),  # grew again: pushing
    )
    observed = [probe.status().pushing for _ in range(6)]
    assert observed == [None, True, False, False, None, True]


def test_probe_pushing_is_unknown_without_a_byte_counter():
    probe, api, clock = _probe(
        _path(ready=True, readers=1, received=None), _path(ready=True, readers=1, received=None)
    )
    assert probe.status().pushing is None
    assert probe.status().pushing is None


def test_probe_caches_for_ttl():
    probe, api, clock = _probe(
        _path(ready=True, readers=1, received=1),
        _path(ready=True, readers=1, received=2),
        ttl_s=2.0,
    )
    first = probe.status()
    clock.now += 1.9
    assert probe.status() is first
    assert len(api.calls) == 1
    clock.now += 0.2
    second = probe.status()
    assert second is not first and second.pushing is True
    assert len(api.calls) == 2


def test_probe_reports_unavailable_api_as_unknown():
    probe, api, clock = _probe(OSError("connection refused"))
    status = probe.status()
    assert (status.ready, status.pushing) == (None, None)
    assert status.detail == "mediamtx api unavailable: connection refused"


def test_probe_reports_timeout_as_unknown():
    probe, api, clock = _probe(TimeoutError("timed out"))
    assert probe.status().ready is None


def test_probe_reports_bad_json_as_unknown():
    probe, api, clock = _probe(b"<html>nope</html>", b'["not", "an", "object"]')
    first = probe.status()
    assert (first.ready, first.pushing) == (None, None)
    assert first.detail.startswith("mediamtx api unavailable: invalid JSON")
    second = probe.status()
    assert second.ready is None and "unexpected JSON shape" in second.detail


def test_probe_reports_http_404_as_not_ready():
    error = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)  # type: ignore[arg-type]
    probe, api, clock = _probe(error)
    status = probe.status()
    assert (status.ready, status.pushing) == (False, False)
    assert "not active" in status.detail


def test_probe_reports_other_http_errors_as_unknown():
    error = urllib.error.HTTPError("http://x", 500, "Boom", {}, None)  # type: ignore[arg-type]
    probe, api, clock = _probe(error)
    status = probe.status()
    assert status.ready is None and "HTTP 500" in status.detail


def test_probe_never_raises_even_on_unexpected_exceptions():
    probe, api, clock = _probe(RuntimeError("weird"))
    status = probe.status()
    assert status.ready is None and "weird" in status.detail


def test_probe_caches_failures_too():
    probe, api, clock = _probe(OSError("down"), _path(ready=True, readers=0), ttl_s=2.0)
    assert probe.status().ready is None
    assert probe.status().ready is None
    assert len(api.calls) == 1
    clock.now += 2.0
    assert probe.status().ready is True


def test_probe_keeps_the_byte_baseline_across_an_outage():
    probe, api, clock = _probe(
        _path(ready=True, readers=1, received=10),
        OSError("blip"),
        _path(ready=True, readers=1, received=20),
    )
    assert probe.status().pushing is None
    assert probe.status().pushing is None
    assert probe.status().pushing is True


def test_pipeline_status_is_immutable():
    status = PipelineStatus(ready=True, pushing=True, detail="x", observed_at=datetime.now(UTC))
    with pytest.raises(AttributeError):
        status.ready = False  # type: ignore[misc]
