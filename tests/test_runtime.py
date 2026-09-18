"""Runtime: on-demand publishing, offline publish, workers and log noise."""

import logging
import threading
from datetime import UTC, datetime

from lattice_cam.config import Config
from lattice_cam.entity import (
    CameraObservation,
    EntityBuilder,
    MediaContributor,
    SensorsContributor,
)
from lattice_cam.health import HealthContributor
from lattice_cam.runtime import FAILURE_LOG_EVERY, Runtime


class FakeEntities:
    def __init__(self, fail_first: int = 0) -> None:
        self.requests = []
        self.timeouts = []
        self.fail_first = fail_first
        self.calls = 0

    def publish_entity(self, request, *, timeout_ms=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError(f"boom {self.calls}")
        self.requests.append(request)
        self.timeouts.append(timeout_ms)


class FakeClient:
    def __init__(self, fail_first: int = 0) -> None:
        self.entities = FakeEntities(fail_first)


def _config() -> Config:
    return Config(lattice_endpoint="example.test", environment_token="tok", entity_id="rpi-cam-01")


def _builder(cfg: Config, video_id: str | None = "vid-1") -> EntityBuilder:
    return EntityBuilder(
        cfg,
        entity_id=cfg.entity_id,
        created_time=datetime.now(UTC),
        contributors=[
            SensorsContributor(lambda: CameraObservation(desired_on=True, ready=True)),
            MediaContributor(lambda: video_id),
            HealthContributor(lambda: None),
        ],
    )


def test_publish_now_uses_builder_and_entities_facade():
    cfg = _config()
    client = FakeClient()
    svc = Runtime(cfg, client, builder=_builder(cfg))
    svc.publish_now()
    svc.publish_now(timeout_ms=250)

    reqs = client.entities.requests
    assert len(reqs) == 2
    assert reqs[0].entity.entity_id == "rpi-cam-01"
    assert reqs[0].entity.media.media[0].item_identifier == "vid-1"
    assert reqs[0].entity.health.connection_status.name == "ONLINE"
    assert reqs[0].entity.sensors.sensors[0].operational_state.name == "OPERATIONAL"
    assert client.entities.timeouts == [1000, 250]
    # Same identity and creation time every call (also shared with the 1 Hz loop).
    assert reqs[0].entity.created_time == reqs[1].entity.created_time
    assert svc.entity_id == "rpi-cam-01"


def test_publish_now_raises_on_failure():
    cfg = _config()
    svc = Runtime(cfg, FakeClient(fail_first=1), builder=_builder(cfg))
    try:
        svc.publish_now()
    except RuntimeError:
        pass
    else:
        raise AssertionError("publish_now must propagate failures")


def test_publish_offline_marks_entity_offline():
    cfg = _config()
    client = FakeClient()
    Runtime(cfg, client, builder=_builder(cfg)).publish_offline()
    e = client.entities.requests[0].entity
    assert e.health.connection_status.name == "OFFLINE"
    assert e.sensors.sensors[0].operational_state.name == "OFF"


def test_publish_offline_swallows_failures(caplog):
    cfg = _config()
    svc = Runtime(cfg, FakeClient(fail_first=1), builder=_builder(cfg))
    with caplog.at_level(logging.WARNING, logger="lattice_cam.runtime"):
        svc.publish_offline()  # must not raise
    assert any("offline publish failed" in r.getMessage() for r in caplog.records)


def test_workers_run_on_named_threads_with_stop_event():
    cfg = _config()
    seen: dict[str, tuple[str, threading.Event]] = {}
    started = threading.Barrier(3, timeout=5)

    def make_worker(name):
        def worker(stop: threading.Event) -> None:
            seen[name] = (threading.current_thread().name, stop)
            started.wait()
            stop.wait(timeout=5)

        return worker

    svc = Runtime(
        cfg,
        FakeClient(),
        builder=_builder(cfg),
        workers=[("tasking", make_worker("tasking")), ("health", make_worker("health"))],
    )
    svc.start_workers()
    started.wait()
    assert seen["tasking"][0] == "tasking"
    assert seen["health"][0] == "health"
    assert seen["tasking"][1] is seen["health"][1] is svc._stop
    assert all(t.daemon for t in svc._worker_threads)
    assert all(t.is_alive() for t in svc._worker_threads)

    svc.stop()
    for t in svc._worker_threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in svc._worker_threads)


def test_consecutive_failures_are_logged_sparsely(caplog):
    cfg = _config()
    failures = FAILURE_LOG_EVERY * 2 + 5
    svc = Runtime(cfg, FakeClient(fail_first=failures), builder=_builder(cfg))
    with caplog.at_level(logging.DEBUG, logger="lattice_cam.runtime"):
        for _ in range(failures + 2):
            svc._tick(svc.entity_id)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    # First failure, then every FAILURE_LOG_EVERY-th one.
    assert [r.consecutive_failures for r in errors] == [
        1,
        FAILURE_LOG_EVERY,
        FAILURE_LOG_EVERY * 2,
    ]
    recovered = [r for r in caplog.records if r.getMessage() == "publish recovered"]
    assert len(recovered) == 1
    assert recovered[0].levelno == logging.INFO
    assert recovered[0].failures == failures
    # After recovery the counter is reset, so a fresh failure logs immediately.
    assert svc._consecutive_failures == 0


def test_success_without_prior_failure_does_not_log_recovery(caplog):
    cfg = _config()
    svc = Runtime(cfg, FakeClient(), builder=_builder(cfg))
    with caplog.at_level(logging.DEBUG, logger="lattice_cam.runtime"):
        svc._tick(svc.entity_id)
        svc._tick(svc.entity_id)
    assert not [r for r in caplog.records if r.getMessage() == "publish recovered"]
