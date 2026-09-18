"""Health telemetry: report types, probes with fixtures, sampler, contributor, wiring.

No test here touches the Pi: sysfs and /proc files come from ``tmp_path``,
``vcgencmd`` is a fake runner, the pipeline and task stream are callables.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from anduril.entitymanager.v1.entity_pub_pb import Entity
from anduril.entitymanager.v1.health_status_pub_pb import (
    AlertLevel as SdkAlertLevel,
)
from anduril.entitymanager.v1.health_status_pub_pb import (
    ConnectionStatus,
    HealthStatus,
)

from lattice_cam import main as main_module
from lattice_cam.camera.control import CameraControl
from lattice_cam.camera.pipeline import CommandPipeline, PipelineStatus
from lattice_cam.config import Config
from lattice_cam.entity import BuildContext, EntityBuilder
from lattice_cam.health import (
    AlertLevel,
    AlertReport,
    ComponentReport,
    HealthContributor,
    HealthSampler,
    HealthSnapshot,
    PipelineProbe,
    PowerProbe,
    ProbeBase,
    ProbeResult,
    Status,
    SystemProbe,
    TaskStreamProbe,
    ThermalProbe,
    ThrottledFlags,
    ThrottledFlagsReader,
)
from lattice_cam.health.probes import _Activation
from lattice_cam.tasking.handler import TaskStreamState

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=5)

# Observed on this Pi while encoding: soft temp limit active now; frequency
# capping, throttling and the soft limit have all occurred since boot.
PI_THROTTLED_OUTPUT = "throttled=0xe0008\n"


def _flags(raw: int) -> ThrottledFlags:
    return ThrottledFlags(raw=raw)


def _flag_source(flags: ThrottledFlags | None):
    return lambda now: flags


def _component(
    component_id: str = "x",
    status: Status = Status.HEALTHY,
    messages: tuple[str, ...] = (),
    name: str = "X",
) -> ComponentReport:
    return ComponentReport(
        id=component_id, name=name, status=status, messages=messages, sampled_at=NOW
    )


def _pipeline_status(
    ready: bool | None, pushing: bool | None, detail: str = "detail"
) -> PipelineStatus:
    return PipelineStatus(ready=ready, pushing=pushing, detail=detail, observed_at=NOW)


# -- report types ------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), Status.NOT_READY),
        ((Status.HEALTHY,), Status.HEALTHY),
        ((Status.HEALTHY, Status.NOT_READY), Status.NOT_READY),
        ((Status.NOT_READY, Status.WARN, Status.HEALTHY), Status.WARN),
        ((Status.WARN, Status.OFFLINE), Status.OFFLINE),
        ((Status.OFFLINE, Status.FAIL, Status.WARN), Status.FAIL),
        ((Status.FAIL, Status.HEALTHY), Status.FAIL),
    ],
)
def test_overall_is_worst_of(statuses, expected):
    snapshot = HealthSnapshot(
        components=tuple(_component(f"c{i}", s) for i, s in enumerate(statuses)),
        alerts=(),
        sampled_at=NOW,
    )
    assert snapshot.overall is expected


def test_severity_order():
    order = [Status.HEALTHY, Status.NOT_READY, Status.WARN, Status.OFFLINE, Status.FAIL]
    assert sorted(Status, key=lambda s: s.severity) == order


def test_component_lookup():
    snapshot = HealthSnapshot(
        components=(_component("a"), _component("b", Status.WARN)), alerts=(), sampled_at=NOW
    )
    assert snapshot.component("b").status is Status.WARN
    assert snapshot.component("nope") is None


@pytest.mark.parametrize(
    ("thermal", "power", "expected"),
    [
        (Status.HEALTHY, Status.HEALTHY, False),
        (Status.WARN, Status.HEALTHY, True),
        (Status.HEALTHY, Status.FAIL, True),
        # Unknown is not degraded: off the Pi vcgencmd is simply absent.
        (Status.HEALTHY, Status.NOT_READY, False),
        (Status.NOT_READY, Status.NOT_READY, False),
    ],
)
def test_is_degraded_follows_thermal_and_power_only(thermal, power, expected):
    snapshot = HealthSnapshot(
        components=(
            _component("soc-thermal", thermal),
            _component("power", power),
            _component("stream", Status.FAIL),  # never degrades the platform
        ),
        alerts=(),
        sampled_at=NOW,
    )
    assert snapshot.is_degraded is expected


def test_reports_are_immutable():
    with pytest.raises(AttributeError):
        _component().status = Status.FAIL  # type: ignore[misc]


# -- throttle flags ----------------------------------------------------------


def test_parse_flags_observed_on_this_pi():
    flags = ThrottledFlags.parse(PI_THROTTLED_OUTPUT)
    assert flags.raw == 0xE0008
    assert flags.soft_temp_limit_now is True
    assert flags.under_voltage_now is False
    assert flags.freq_capped_now is False
    assert flags.throttled_now is False
    assert flags.under_voltage_occurred is False
    assert flags.freq_capped_occurred is True
    assert flags.throttled_occurred is True
    assert flags.soft_temp_limit_occurred is True


def test_parse_flags_each_bit():
    assert ThrottledFlags.parse("throttled=0x0").raw == 0
    assert ThrottledFlags.parse("throttled=0x1").under_voltage_now
    assert ThrottledFlags.parse("throttled=0x2").freq_capped_now
    assert ThrottledFlags.parse("throttled=0x4").throttled_now
    assert ThrottledFlags.parse("throttled=0x8").soft_temp_limit_now
    assert ThrottledFlags.parse("throttled=0x10000").under_voltage_occurred
    assert ThrottledFlags.parse("throttled=0x20000").freq_capped_occurred
    assert ThrottledFlags.parse("throttled=0x40000").throttled_occurred
    assert ThrottledFlags.parse("throttled=0x80000").soft_temp_limit_occurred


@pytest.mark.parametrize("output", ["", "error", "throttled=", "throttled=zz", "0xe0008"])
def test_parse_flags_rejects_garbage(output):
    with pytest.raises(ValueError):
        ThrottledFlags.parse(output)


def test_reader_runs_command_once_per_round():
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(argv)
        return PI_THROTTLED_OUTPUT

    reader = ThrottledFlagsReader(runner)
    first = reader(NOW)
    assert reader(NOW) is first  # thermal and power probes share one read
    assert calls == [["vcgencmd", "get_throttled"]]
    assert reader(LATER).raw == 0xE0008
    assert len(calls) == 2


def test_reader_returns_none_when_command_fails(caplog):
    def runner(argv):
        raise FileNotFoundError("vcgencmd")

    reader = ThrottledFlagsReader(runner)
    with caplog.at_level(logging.WARNING, logger="lattice_cam.health.probes"):
        assert reader(NOW) is None
        assert reader(LATER) is None
    warnings = [r for r in caplog.records if "vcgencmd unavailable" in r.getMessage()]
    assert len(warnings) == 1  # logged once, not every round


def test_reader_returns_none_on_bad_output():
    assert ThrottledFlagsReader(lambda argv: "garbage")(NOW) is None


# -- thermal probe -----------------------------------------------------------


def _temp_file(tmp_path, millidegrees: str):
    path = tmp_path / "temp"
    path.write_text(millidegrees)
    return str(path)


def test_thermal_healthy(tmp_path):
    probe = ThermalProbe(
        throttled=_flag_source(_flags(0)), temp_path=_temp_file(tmp_path, "51234\n")
    )
    result = probe.sample(NOW)
    (component,) = result.components
    assert component.id == "soc-thermal"
    assert component.name == "SoC temperature"
    assert component.status is Status.HEALTHY
    assert component.messages == ("SoC 51.2 °C",)
    assert component.sampled_at == NOW
    assert result.alerts == ()
    assert result.pipeline is None


def test_thermal_warn_at_threshold(tmp_path):
    probe = ThermalProbe(
        throttled=_flag_source(_flags(0)), temp_path=_temp_file(tmp_path, "75000"), warn_c=75.0
    )
    (component,) = probe.sample(NOW).components
    assert component.status is Status.WARN
    assert "SoC 75.0 °C" in component.messages
    assert any("warning threshold" in m for m in component.messages)


def test_thermal_fail_at_threshold(tmp_path):
    probe = ThermalProbe(
        throttled=_flag_source(_flags(0)), temp_path=_temp_file(tmp_path, "85100"), fail_c=85.0
    )
    result = probe.sample(NOW)
    assert result.components[0].status is Status.FAIL
    assert "SoC 85.1 °C" in result.components[0].messages
    assert result.alerts == ()  # thresholds alone raise no alert; the firmware flags do


def test_thermal_soft_limit_flag_warns_and_alerts(tmp_path):
    probe = ThermalProbe(
        throttled=_flag_source(ThrottledFlags.parse(PI_THROTTLED_OUTPUT)),
        temp_path=_temp_file(tmp_path, "60000"),
    )
    result = probe.sample(NOW)
    (component,) = result.components
    assert component.status is Status.WARN
    assert "soft temperature limit active" in component.messages
    (alert,) = result.alerts
    assert alert.code == "THERMAL_THROTTLE"
    assert alert.level is AlertLevel.WARNING
    assert alert.conditions == (("SOFT_TEMP_LIMIT", "soft temperature limit active"),)
    assert alert.activated_at == NOW


def test_thermal_throttled_flag_fails(tmp_path):
    probe = ThermalProbe(
        throttled=_flag_source(_flags(0x4 | 0x8)), temp_path=_temp_file(tmp_path, "60000")
    )
    result = probe.sample(NOW)
    assert result.components[0].status is Status.FAIL
    assert "CPU throttled" in result.components[0].messages
    assert result.alerts[0].conditions == (
        ("SOFT_TEMP_LIMIT", "soft temperature limit active"),
        ("THROTTLED", "CPU throttled"),
    )


def test_thermal_alert_keeps_first_activation_time(tmp_path):
    probe = ThermalProbe(
        throttled=_flag_source(_flags(0x8)), temp_path=_temp_file(tmp_path, "60000")
    )
    probe.sample(NOW)
    assert probe.sample(LATER).alerts[0].activated_at == NOW


def test_thermal_alert_clears_and_reactivates(tmp_path):
    flags = {"value": _flags(0x8)}
    probe = ThermalProbe(
        throttled=lambda now: flags["value"], temp_path=_temp_file(tmp_path, "60000")
    )
    probe.sample(NOW)
    flags["value"] = _flags(0)
    assert probe.sample(LATER).alerts == ()
    flags["value"] = _flags(0x8)
    much_later = LATER + timedelta(minutes=1)
    assert probe.sample(much_later).alerts[0].activated_at == much_later


def test_thermal_without_flags_still_evaluates_temperature(tmp_path):
    probe = ThermalProbe(throttled=_flag_source(None), temp_path=_temp_file(tmp_path, "80000"))
    (component,) = probe.sample(NOW).components
    assert component.status is Status.WARN
    assert "throttle flags unavailable" in component.messages
    assert "SoC 80.0 °C" in component.messages


def test_thermal_unreadable_file_is_not_ready(tmp_path):
    probe = ThermalProbe(throttled=_flag_source(_flags(0)), temp_path=str(tmp_path / "missing"))
    (component,) = probe.sample(NOW).components
    assert component.status is Status.NOT_READY
    assert component.messages[0].startswith("probe error:")
    assert "missing" in component.messages[0]


def test_thermal_garbage_file_is_not_ready(tmp_path):
    probe = ThermalProbe(throttled=_flag_source(_flags(0)), temp_path=_temp_file(tmp_path, "hot"))
    assert probe.sample(NOW).components[0].status is Status.NOT_READY


# -- power probe -------------------------------------------------------------


def test_power_healthy():
    result = PowerProbe(throttled=_flag_source(_flags(0))).sample(NOW)
    (component,) = result.components
    assert (component.id, component.name) == ("power", "Power supply")
    assert component.status is Status.HEALTHY
    assert component.messages == ("supply OK",)
    assert result.alerts == ()


def test_power_under_voltage_now_fails_with_alert():
    result = PowerProbe(throttled=_flag_source(_flags(0x1))).sample(NOW)
    assert result.components[0].status is Status.FAIL
    assert result.components[0].messages == ("under-voltage now",)
    (alert,) = result.alerts
    assert alert.code == "UNDER_VOLTAGE"
    assert alert.level is AlertLevel.CAUTION
    assert alert.conditions[0][0] == "UNDER_VOLTAGE"
    assert alert.activated_at == NOW


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (0x10000, "under-voltage since boot"),
        (0x20000, "frequency capped since boot"),
        (0x40000, "throttled since boot"),
    ],
)
def test_power_occurred_flags_warn(raw, message):
    result = PowerProbe(throttled=_flag_source(_flags(raw))).sample(NOW)
    assert result.components[0].status is Status.WARN
    assert result.components[0].messages == (message,)
    assert result.alerts == ()


def test_power_on_this_pi_warns_on_history():
    flags = ThrottledFlags.parse(PI_THROTTLED_OUTPUT)
    (component,) = PowerProbe(throttled=_flag_source(flags)).sample(NOW).components
    assert component.status is Status.WARN
    assert component.messages == ("frequency capped since boot", "throttled since boot")


def test_power_without_vcgencmd_is_not_ready():
    (component,) = PowerProbe(throttled=_flag_source(None)).sample(NOW).components
    assert component.status is Status.NOT_READY
    assert component.messages == ("vcgencmd unavailable",)


def test_power_alert_activation_is_sticky():
    flags = {"value": _flags(0x1)}
    probe = PowerProbe(throttled=lambda now: flags["value"])
    probe.sample(NOW)
    assert probe.sample(LATER).alerts[0].activated_at == NOW
    flags["value"] = None
    assert probe.sample(LATER).alerts == ()


def test_activation_latch():
    latch = _Activation()
    assert latch.update(False, NOW) is None
    assert latch.update(True, NOW) == NOW
    assert latch.update(True, LATER) == NOW
    assert latch.update(False, LATER) is None
    assert latch.update(True, LATER) == LATER


# -- pipeline probe ----------------------------------------------------------


def test_pipeline_stopped_by_operator():
    status = _pipeline_status(False, False, "mediamtx path 'cam' is not active")
    result = PipelineProbe(status=lambda: status, desired_on=lambda: False).sample(NOW)
    camera, stream = result.components
    assert (camera.id, camera.name) == ("camera", "Camera capture")
    assert (stream.id, stream.name) == ("stream", "SRT push")
    assert camera.status is stream.status is Status.NOT_READY
    assert camera.messages == stream.messages == ("stopped by operator",)
    assert result.pipeline is status  # still exposed for the publish loop


def test_pipeline_healthy_when_ready_and_pushing():
    status = _pipeline_status(True, True, "ready, readers=1, bytes flowing")
    result = PipelineProbe(status=lambda: status, desired_on=lambda: True).sample(NOW)
    camera, stream = result.components
    assert camera.status is stream.status is Status.HEALTHY
    assert camera.messages == ("ready, readers=1, bytes flowing",)
    assert result.pipeline is status


def test_pipeline_fail_when_observed_down():
    status = _pipeline_status(False, False, "not ready, no readers")
    camera, stream = (
        PipelineProbe(status=lambda: status, desired_on=lambda: True).sample(NOW).components
    )
    assert camera.status is stream.status is Status.FAIL
    assert camera.messages == ("camera path down: not ready, no readers",)
    assert stream.messages == ("SRT push down: not ready, no readers",)


def test_pipeline_not_ready_when_unobservable():
    status = PipelineStatus.unknown("mediamtx api unavailable: connection refused")
    camera, stream = (
        PipelineProbe(status=lambda: status, desired_on=lambda: True).sample(NOW).components
    )
    assert camera.status is stream.status is Status.NOT_READY
    assert camera.messages == (
        "MediaMTX API unavailable: mediamtx api unavailable: connection refused",
    )


def test_pipeline_mixed_ready_but_not_pushing():
    status = _pipeline_status(True, None, "ready, readers=1, no byte baseline yet")
    camera, stream = (
        PipelineProbe(status=lambda: status, desired_on=lambda: True).sample(NOW).components
    )
    assert camera.status is Status.HEALTHY
    assert stream.status is Status.NOT_READY
    # The API answered (ready is known), so this is not an API outage.
    assert stream.messages == (
        "SRT push not yet confirmed, awaiting next sample: ready, readers=1, no byte baseline yet",
    )


def test_pipeline_probe_error_marks_both_not_ready():
    def boom() -> PipelineStatus:
        raise RuntimeError("kaboom")

    result = PipelineProbe(status=boom, desired_on=lambda: True).sample(NOW)
    assert [c.id for c in result.components] == ["camera", "stream"]
    assert all(c.status is Status.NOT_READY for c in result.components)
    assert all("kaboom" in c.messages[0] for c in result.components)
    assert result.pipeline is None


# -- task stream probe -------------------------------------------------------


def _stream(connected=True, heartbeat=None, error=None, reconnects=0) -> TaskStreamState:
    return TaskStreamState(
        connected=connected, last_heartbeat=heartbeat, last_error=error, reconnects=reconnects
    )


def test_task_stream_offline_when_disconnected():
    state = _stream(connected=False, error="unavailable", reconnects=3)
    (component,) = (
        TaskStreamProbe(stream_state=lambda: state, heartbeat_interval_ms=30000)
        .sample(NOW)
        .components
    )
    assert (component.id, component.name) == ("tasking", "Lattice task stream")
    assert component.status is Status.OFFLINE
    assert component.messages == (
        "task stream disconnected",
        "last error: unavailable",
        "reconnects: 3",
    )


def test_task_stream_awaiting_first_heartbeat():
    (component,) = (
        TaskStreamProbe(stream_state=lambda: _stream(), heartbeat_interval_ms=30000)
        .sample(NOW)
        .components
    )
    assert component.status is Status.HEALTHY
    assert component.messages == ("connected, awaiting first heartbeat", "reconnects: 0")


def test_task_stream_fresh_heartbeat_is_healthy():
    state = _stream(heartbeat=NOW - timedelta(seconds=59))
    (component,) = (
        TaskStreamProbe(stream_state=lambda: state, heartbeat_interval_ms=30000)
        .sample(NOW)
        .components
    )
    assert component.status is Status.HEALTHY
    assert component.messages[0] == "connected, last heartbeat 59 s ago"


def test_task_stream_stale_heartbeat_warns():
    state = _stream(heartbeat=NOW - timedelta(seconds=61))
    (component,) = (
        TaskStreamProbe(stream_state=lambda: state, heartbeat_interval_ms=30000)
        .sample(NOW)
        .components
    )
    assert component.status is Status.WARN
    assert component.messages[0] == "last heartbeat 61 s ago (stale after 60 s)"


def test_task_stream_no_staleness_when_heartbeats_disabled():
    state = _stream(heartbeat=NOW - timedelta(hours=1))
    (component,) = (
        TaskStreamProbe(stream_state=lambda: state, heartbeat_interval_ms=0).sample(NOW).components
    )
    assert component.status is Status.HEALTHY


def test_task_stream_probe_error_is_not_ready():
    def boom() -> TaskStreamState:
        raise RuntimeError("no handler")

    (component,) = (
        TaskStreamProbe(stream_state=boom, heartbeat_interval_ms=30000).sample(NOW).components
    )
    assert component.status is Status.NOT_READY
    assert "no handler" in component.messages[0]


# -- system probe ------------------------------------------------------------


def _system_files(
    tmp_path, load="0.50 0.40 0.30 1/200 12345", total_kb=8_000_000, avail_kb=4_000_000
):
    loadavg = tmp_path / "loadavg"
    loadavg.write_text(f"{load}\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        f"MemTotal:       {total_kb} kB\nMemFree:        1000 kB\n"
        f"MemAvailable:   {avail_kb} kB\nBuffers:        10 kB\n"
    )
    return str(loadavg), str(meminfo)


def _disk(total: int, free: int):
    return lambda path: (total, total - free, free)


GIB = 1024**3


def test_system_healthy(tmp_path):
    loadavg, meminfo = _system_files(tmp_path)
    probe = SystemProbe(
        loadavg_path=loadavg,
        meminfo_path=meminfo,
        disk_path="/data",
        cpu_count=4,
        disk_usage=_disk(total=64 * GIB, free=32 * GIB),
    )
    (component,) = probe.sample(NOW).components
    assert (component.id, component.name) == ("system", "Pi system")
    assert component.status is Status.HEALTHY
    assert component.messages == (
        "load 0.50 (0.12 per CPU, 4 CPUs)",
        "memory 3906 MiB available of 7812 MiB (50%)",
        "disk 32.0 GiB free of 64.0 GiB (50%)",
    )


def test_system_warns_on_high_load(tmp_path):
    loadavg, meminfo = _system_files(tmp_path, load="6.10 5.00 4.00 3/200 1")
    probe = SystemProbe(
        loadavg_path=loadavg,
        meminfo_path=meminfo,
        cpu_count=4,
        disk_usage=_disk(64 * GIB, 32 * GIB),
    )
    (component,) = probe.sample(NOW).components
    assert component.status is Status.WARN
    assert "CPU overloaded" in component.messages


def test_system_warns_on_low_memory(tmp_path):
    loadavg, meminfo = _system_files(tmp_path, total_kb=8_000_000, avail_kb=700_000)
    probe = SystemProbe(
        loadavg_path=loadavg,
        meminfo_path=meminfo,
        cpu_count=4,
        disk_usage=_disk(64 * GIB, 32 * GIB),
    )
    (component,) = probe.sample(NOW).components
    assert component.status is Status.WARN
    assert "memory low" in component.messages


def test_system_warns_on_low_disk(tmp_path):
    loadavg, meminfo = _system_files(tmp_path)
    probe = SystemProbe(
        loadavg_path=loadavg,
        meminfo_path=meminfo,
        cpu_count=4,
        disk_usage=_disk(64 * GIB, 5 * GIB),
    )
    (component,) = probe.sample(NOW).components
    assert component.status is Status.WARN
    assert "disk space low" in component.messages


def test_system_passes_disk_path_to_disk_usage(tmp_path):
    loadavg, meminfo = _system_files(tmp_path)
    seen: list[str] = []

    def disk_usage(path: str):
        seen.append(path)
        return (10 * GIB, 5 * GIB, 5 * GIB)

    SystemProbe(
        loadavg_path=loadavg, meminfo_path=meminfo, disk_path="/mnt/x", disk_usage=disk_usage
    ).sample(NOW)
    assert seen == ["/mnt/x"]


def test_system_unreadable_is_not_ready(tmp_path):
    _, meminfo = _system_files(tmp_path)
    probe = SystemProbe(
        loadavg_path=str(tmp_path / "nope"), meminfo_path=meminfo, disk_usage=_disk(1, 1)
    )
    (component,) = probe.sample(NOW).components
    assert component.status is Status.NOT_READY
    assert "nope" in component.messages[0]


def test_system_meminfo_without_available_is_not_ready(tmp_path):
    loadavg, _ = _system_files(tmp_path)
    meminfo = tmp_path / "meminfo2"
    meminfo.write_text("MemTotal: 100 kB\n")
    probe = SystemProbe(loadavg_path=loadavg, meminfo_path=str(meminfo), disk_usage=_disk(1, 1))
    assert probe.sample(NOW).components[0].status is Status.NOT_READY


# -- ProbeBase contract ------------------------------------------------------


class _Fixed(ProbeBase):
    components = (("fixed", "Fixed"),)

    def __init__(self, status: Status | None = Status.HEALTHY, alert: AlertReport | None = None):
        self.status = status
        self.alert = alert
        self.calls = 0

    def _sample(self, now: datetime) -> ProbeResult:
        self.calls += 1
        if self.status is None:
            raise RuntimeError("sensor gone")
        return ProbeResult(
            components=(_component("fixed", self.status, name="Fixed"),),
            alerts=(self.alert,) if self.alert else (),
        )


def test_probe_base_never_raises(caplog):
    with caplog.at_level(logging.WARNING, logger="lattice_cam.health.probes"):
        (component,) = _Fixed(status=None).sample(NOW).components
    assert component.status is Status.NOT_READY
    assert component.messages == ("probe error: sensor gone",)
    assert component.sampled_at == NOW
    assert any("health probe failed" in r.getMessage() for r in caplog.records)


# -- sampler -----------------------------------------------------------------


class _Pipeline(ProbeBase):
    components = (("camera", "Camera capture"),)

    def __init__(self, status: PipelineStatus) -> None:
        self._status = status

    def _sample(self, now: datetime) -> ProbeResult:
        return ProbeResult(components=(_component("camera"),), pipeline=self._status)


def test_sample_once_collects_everything():
    alert = AlertReport("A", "alert", AlertLevel.ADVISORY, (), NOW)
    status = _pipeline_status(True, True)
    sampler = HealthSampler(
        [_Fixed(Status.WARN, alert), _Pipeline(status)], interval_s=5.0, clock=lambda: NOW
    )
    assert sampler.snapshot() is None
    snapshot = sampler.sample_once()
    assert snapshot.sampled_at == NOW
    assert [c.id for c in snapshot.components] == ["fixed", "camera"]
    assert snapshot.alerts == (alert,)
    assert snapshot.pipeline is status
    assert snapshot.overall is Status.WARN
    assert sampler.snapshot() is snapshot


def test_sample_once_accepts_explicit_now():
    sampler = HealthSampler([_Fixed()], interval_s=5.0, clock=lambda: NOW)
    assert sampler.sample_once(LATER).sampled_at == LATER


def test_sampler_survives_a_probe_that_breaks_the_contract(caplog):
    class Rogue:
        def sample(self, now):
            raise RuntimeError("contract broken")

    sampler = HealthSampler([Rogue(), _Fixed()], interval_s=5.0, clock=lambda: NOW)
    with caplog.at_level(logging.ERROR, logger="lattice_cam.health.sampler"):
        snapshot = sampler.sample_once()
    assert [c.id for c in snapshot.components] == ["fixed"]
    assert any("health probe raised" in r.getMessage() for r in caplog.records)


def test_sampler_logs_only_status_changes(caplog):
    probe = _Fixed(Status.HEALTHY)
    sampler = HealthSampler([probe], interval_s=5.0, clock=lambda: NOW)
    with caplog.at_level(logging.INFO, logger="lattice_cam.health.sampler"):
        sampler.sample_once()  # first sighting: logged
        sampler.sample_once()  # unchanged: quiet
        probe.status = Status.FAIL
        sampler.sample_once()  # change: logged
        sampler.sample_once()  # unchanged: quiet
    changes = [r for r in caplog.records if r.getMessage() == "health component changed"]
    assert [(r.component, r.status, r.previous) for r in changes] == [
        ("fixed", "HEALTHY", None),
        ("fixed", "FAIL", "HEALTHY"),
    ]


def test_run_samples_until_stopped():
    probe = _Fixed()
    stop = threading.Event()
    sampler = HealthSampler([probe], interval_s=0.01, clock=lambda: NOW)

    class _Stopper(ProbeBase):
        components = ()

        def _sample(self, now):
            if probe.calls >= 3:
                stop.set()
            return ProbeResult(components=())

    sampler = HealthSampler([probe, _Stopper()], interval_s=0.01, clock=lambda: NOW)
    thread = threading.Thread(target=sampler.run, args=(stop,), daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert probe.calls == 3
    assert sampler.snapshot() is not None


def test_run_returns_promptly_when_already_stopped():
    probe = _Fixed()
    stop = threading.Event()
    stop.set()
    HealthSampler([probe], interval_s=60.0).run(stop)
    assert probe.calls == 0


# -- contributor -------------------------------------------------------------


def _snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        components=(
            ComponentReport(
                id="soc-thermal",
                name="SoC temperature",
                status=Status.WARN,
                messages=("SoC 76.0 °C", "soft temperature limit active"),
                sampled_at=NOW - timedelta(seconds=2),
            ),
            ComponentReport(
                id="power",
                name="Power supply",
                status=Status.HEALTHY,
                messages=("supply OK",),
                sampled_at=NOW - timedelta(seconds=2),
            ),
        ),
        alerts=(
            AlertReport(
                code="THERMAL_THROTTLE",
                description="SoC is thermally throttled",
                level=AlertLevel.WARNING,
                conditions=(("SOFT_TEMP_LIMIT", "soft temperature limit active"),),
                activated_at=NOW - timedelta(minutes=10),
            ),
        ),
        sampled_at=NOW - timedelta(seconds=2),
    )


def test_contributor_maps_snapshot_to_sdk_health():
    entity = Entity()
    HealthContributor(_snapshot).apply(entity, BuildContext(now=NOW))
    health = entity.health
    assert health.connection_status == ConnectionStatus.ONLINE
    assert health.health_status == HealthStatus.WARN
    assert health.update_time.to_datetime() == NOW

    thermal, power = health.components
    assert (thermal.id, thermal.name, thermal.health) == (
        "soc-thermal",
        "SoC temperature",
        HealthStatus.WARN,
    )
    assert [(m.status, m.message) for m in thermal.messages] == [
        (HealthStatus.WARN, "SoC 76.0 °C"),
        (HealthStatus.WARN, "soft temperature limit active"),
    ]
    assert thermal.update_time.to_datetime() == NOW - timedelta(seconds=2)
    assert power.health == HealthStatus.HEALTHY

    (alert,) = health.active_alerts
    assert alert.alert_code == "THERMAL_THROTTLE"
    assert alert.description == "SoC is thermally throttled"
    assert alert.level == SdkAlertLevel.WARNING
    assert alert.activated_time.to_datetime() == NOW - timedelta(minutes=10)
    (condition,) = alert.active_conditions
    assert (condition.condition_code, condition.description) == (
        "SOFT_TEMP_LIMIT",
        "soft temperature limit active",
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (Status.HEALTHY, HealthStatus.HEALTHY),
        (Status.WARN, HealthStatus.WARN),
        (Status.FAIL, HealthStatus.FAIL),
        (Status.OFFLINE, HealthStatus.OFFLINE),
        (Status.NOT_READY, HealthStatus.NOT_READY),
    ],
)
def test_contributor_status_mapping(status, expected):
    entity = Entity()
    snapshot = HealthSnapshot(components=(_component("c", status),), alerts=(), sampled_at=NOW)
    HealthContributor(lambda: snapshot).apply(entity, BuildContext(now=NOW))
    assert entity.health.health_status == expected
    assert entity.health.components[0].health == expected


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (AlertLevel.ADVISORY, SdkAlertLevel.ADVISORY),
        (AlertLevel.CAUTION, SdkAlertLevel.CAUTION),
        (AlertLevel.WARNING, SdkAlertLevel.WARNING),
    ],
)
def test_contributor_alert_level_mapping(level, expected):
    entity = Entity()
    snapshot = HealthSnapshot(
        components=(_component(),),
        alerts=(AlertReport("X", "x", level, (), NOW),),
        sampled_at=NOW,
    )
    HealthContributor(lambda: snapshot).apply(entity, BuildContext(now=NOW))
    assert entity.health.active_alerts[0].level == expected


def test_contributor_offline_on_shutdown_publish():
    entity = Entity()
    HealthContributor(_snapshot).apply(entity, BuildContext(now=NOW, offline=True))
    assert entity.health.connection_status == ConnectionStatus.OFFLINE
    assert entity.health.health_status == HealthStatus.WARN  # components still reported


def test_contributor_not_ready_without_snapshot():
    entity = Entity()
    HealthContributor(lambda: None).apply(entity, BuildContext(now=NOW))
    assert entity.health.connection_status == ConnectionStatus.ONLINE
    assert entity.health.health_status == HealthStatus.NOT_READY
    assert list(entity.health.components) == []
    assert list(entity.health.active_alerts) == []
    assert entity.health.update_time.to_datetime() == NOW


def test_built_entity_with_health_serialises():
    cfg = Config(lattice_endpoint="example.test", environment_token="tok")
    builder = EntityBuilder(
        cfg, entity_id="rpi-cam-01", created_time=NOW, contributors=[HealthContributor(_snapshot)]
    )
    request = builder.build(now=NOW)
    assert len(request.to_binary()) > 0
    assert request.entity.health.components[0].id == "soc-thermal"


# -- wiring (main._build_health) ---------------------------------------------


def _wiring_config(**overrides) -> Config:
    cfg = Config(lattice_endpoint="example.test", environment_token="tok")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_build_health_disabled_returns_none():
    cfg = _wiring_config(health_enabled=False)
    control = CameraControl()
    assert main_module._build_health(cfg, control, CommandPipeline(), None) is None


def test_build_health_wires_expected_probes():
    cfg = _wiring_config(health_temp_warn_c=70.0, health_temp_fail_c=80.0)
    control = CameraControl()
    sampler = main_module._build_health(cfg, control, CommandPipeline(), None)
    assert sampler is not None
    assert [type(p).__name__ for p in sampler.probes] == [
        "ThermalProbe",
        "PowerProbe",
        "PipelineProbe",
        "SystemProbe",
    ]
    thermal = sampler.probes[0]
    assert isinstance(thermal, ThermalProbe)
    assert (thermal._warn_c, thermal._fail_c) == (70.0, 80.0)
    # Both Pi probes share one flags reader so vcgencmd runs once per round.
    assert isinstance(thermal._throttled, ThrottledFlagsReader)
    assert sampler.probes[1]._throttled is thermal._throttled  # type: ignore[attr-defined]


def test_build_health_adds_task_stream_probe_with_handler():
    class FakeHandler:
        def stream_state(self):
            return _stream(connected=False)

    cfg = _wiring_config()
    sampler = main_module._build_health(cfg, CameraControl(), CommandPipeline(), FakeHandler())
    assert sampler is not None
    assert type(sampler.probes[-1]).__name__ == "TaskStreamProbe"


def test_build_health_sampling_is_hermetic_off_the_pi(tmp_path, monkeypatch):
    # Off the Pi: no vcgencmd, no MediaMTX. Point the thermal/system probes at
    # fixtures and make the flags reader fail instead of running a subprocess.
    def no_subprocess(argv):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("lattice_cam.health.probes._subprocess_runner", no_subprocess)
    sampler = main_module._build_health(_wiring_config(), CameraControl(), CommandPipeline(), None)
    assert sampler is not None
    thermal = sampler.probes[0]
    assert isinstance(thermal, ThermalProbe)
    thermal._temp_path = str(tmp_path / "temp")
    (tmp_path / "temp").write_text("50000")
    system = sampler.probes[3]
    assert isinstance(system, SystemProbe)
    loadavg, meminfo = _system_files(tmp_path)
    system._loadavg_path, system._meminfo_path = loadavg, meminfo
    system._disk_usage = _disk(64 * GIB, 32 * GIB)

    snapshot = sampler.sample_once(NOW)
    assert snapshot.component("soc-thermal").status is Status.HEALTHY
    assert snapshot.component("power").status is Status.NOT_READY
    assert snapshot.component("camera").messages == ("stopped by operator",)
    assert snapshot.component("system").status is Status.HEALTHY
    assert snapshot.is_degraded is False
