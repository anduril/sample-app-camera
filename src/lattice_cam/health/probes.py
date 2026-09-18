"""Health probes: each one observes one thing on the Pi and reports components.

A probe answers "how is X right now?" for one source (the SoC temperature,
the power supply, the media pipeline, the task stream, the host) and returns
plain ``ComponentReport``s plus any ``AlertReport``s. Probes are the only
place that knows *how* something is observed (a sysfs file, ``vcgencmd``, a
callable into another package), so adding telemetry means adding a probe and
listing it in ``main.py``; nothing else learns about the new source.

Two rules keep the sampler and the publish loop safe:

* **A probe never raises.** Every probe runs through ``ProbeBase.sample``,
  which turns any exception into a ``NOT_READY`` component carrying the error
  text. A broken sensor becomes a visible health state instead of a dead
  sampling thread.
* **A probe is testable without the hardware.** File paths, command runners
  and the callables into other packages are constructor arguments, so tests
  point them at ``tmp_path`` files and fakes. Nothing here is read at import
  time.

Thresholds and the interpretation of Raspberry Pi specifics (``vcgencmd
get_throttled`` bit layout, ``thermal_zone0`` in millidegrees) are documented
next to the code that uses them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..camera.pipeline import PipelineStatus
from ..logging_setup import get_logger
from ..tasking.handler import TaskStreamState
from .report import AlertLevel, AlertReport, ComponentReport, ProbeResult, Status

logger = get_logger(__name__)


class Probe(Protocol):
    """One health source. ``sample`` must be cheap and must never raise."""

    def sample(self, now: datetime) -> ProbeResult: ...


class ProbeBase:
    """Shared never-raise wrapper: subclasses implement ``_sample`` only.

    ``components`` lists the (id, name) pairs a subclass reports, so that on
    an unexpected error every one of them is published ``NOT_READY`` with the
    error message rather than silently disappearing from the entity.
    """

    components: Sequence[tuple[str, str]] = ()

    def sample(self, now: datetime) -> ProbeResult:
        try:
            return self._sample(now)
        except Exception as exc:  # the contract: a probe never raises
            logger.warning("health probe failed", probe=type(self).__name__, error=str(exc))
            return ProbeResult(
                components=tuple(
                    _report(cid, name, Status.NOT_READY, [f"probe error: {exc}"], now)
                    for cid, name in self.components
                )
            )

    def _sample(self, now: datetime) -> ProbeResult:
        raise NotImplementedError


def _report(
    component_id: str, name: str, status: Status, messages: Sequence[str], now: datetime
) -> ComponentReport:
    return ComponentReport(
        id=component_id, name=name, status=status, messages=tuple(messages), sampled_at=now
    )


class _Activation:
    """Remembers when a condition first became true.

    Lattice shows an alert's ``activated_time``; re-stamping it every sample
    would make a two-hour-old throttle look brand new. The latch clears when
    the condition does, so the next occurrence gets a fresh time.
    """

    def __init__(self) -> None:
        self._since: datetime | None = None

    def update(self, active: bool, now: datetime) -> datetime | None:
        if not active:
            self._since = None
        elif self._since is None:
            self._since = now
        return self._since


# --- vcgencmd get_throttled ------------------------------------------------------

CommandRunner = Callable[[list[str]], str]
"""``runner(argv) -> stdout``; raises on any failure. Injectable for tests."""

VCGENCMD_TIMEOUT_S = 2.0


def _subprocess_runner(argv: list[str]) -> str:
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=VCGENCMD_TIMEOUT_S, check=True
    )
    return result.stdout


@dataclass(frozen=True)
class ThrottledFlags:
    """The bits of ``vcgencmd get_throttled`` (``throttled=0xNNNNN``) by name.

    Bits 0-3 describe the state *now*; bits 16-19 are the same events latched
    since boot. Layout per the Raspberry Pi documentation:

    ======  ======================  ======  ================================
    bit 0   under-voltage now       bit 16  under-voltage has occurred
    bit 1   ARM frequency capped    bit 17  frequency capping has occurred
    bit 2   currently throttled     bit 18  throttling has occurred
    bit 3   soft temp limit active  bit 19  soft temp limit has occurred
    ======  ======================  ======  ================================
    """

    raw: int

    @classmethod
    def parse(cls, output: str) -> ThrottledFlags:
        """Parse ``throttled=0x...`` (surrounding whitespace tolerated)."""
        text = output.strip()
        prefix = "throttled="
        if not text.startswith(prefix):
            raise ValueError(f"unexpected vcgencmd output: {text!r}")
        return cls(raw=int(text[len(prefix) :], 16))

    def _bit(self, index: int) -> bool:
        return bool(self.raw & (1 << index))

    @property
    def under_voltage_now(self) -> bool:
        return self._bit(0)

    @property
    def freq_capped_now(self) -> bool:
        return self._bit(1)

    @property
    def throttled_now(self) -> bool:
        return self._bit(2)

    @property
    def soft_temp_limit_now(self) -> bool:
        return self._bit(3)

    @property
    def under_voltage_occurred(self) -> bool:
        return self._bit(16)

    @property
    def freq_capped_occurred(self) -> bool:
        return self._bit(17)

    @property
    def throttled_occurred(self) -> bool:
        return self._bit(18)

    @property
    def soft_temp_limit_occurred(self) -> bool:
        return self._bit(19)


ThrottledSource = Callable[[datetime], "ThrottledFlags | None"]
"""``source(now) -> flags``; ``None`` when ``vcgencmd`` is unavailable."""


class ThrottledFlagsReader:
    """Runs ``vcgencmd get_throttled`` at most once per sampling round.

    The thermal and power probes both need the flags. The sampler hands every
    probe the same ``now``, so memoising on it means one subprocess per round
    without the probes having to coordinate. ``None`` (command missing, not a
    Pi, permission denied, timeout) is remembered for the round too, and is
    logged only on the first failure so a non-Pi host does not spam the log.
    """

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        command: Sequence[str] = ("vcgencmd", "get_throttled"),
    ) -> None:
        self._runner = runner or _subprocess_runner
        self._command = list(command)
        self._lock = threading.Lock()
        self._round: datetime | None = None
        self._flags: ThrottledFlags | None = None
        self._warned = False

    def __call__(self, now: datetime) -> ThrottledFlags | None:
        with self._lock:
            if self._round != now:
                self._round = now
                self._flags = self._read()
            return self._flags

    def _read(self) -> ThrottledFlags | None:
        try:
            flags = ThrottledFlags.parse(self._runner(self._command))
        except Exception as exc:
            if not self._warned:
                logger.warning("vcgencmd unavailable; throttle flags unknown", error=str(exc))
                self._warned = True
            return None
        self._warned = False
        return flags


# --- SoC temperature -------------------------------------------------------------

THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"


class ThermalProbe(ProbeBase):
    """``soc-thermal``: SoC temperature plus the firmware's throttle flags.

    The temperature comes from sysfs in millidegrees. The thresholds are the
    operator's early warning; the firmware flags are the ground truth of what
    the SoC is actually doing about heat (``soft_temp_limit_now`` at 60 °C by
    default, hard throttling at 80-85 °C), so a set flag raises the status
    regardless of the reading. An unavailable ``vcgencmd`` degrades to
    "temperature only" rather than ``NOT_READY``: the number is still useful.
    """

    components = (("soc-thermal", "SoC temperature"),)

    def __init__(
        self,
        *,
        throttled: ThrottledSource,
        temp_path: str = THERMAL_ZONE_PATH,
        warn_c: float = 75.0,
        fail_c: float = 85.0,
    ) -> None:
        self._throttled = throttled
        self._temp_path = temp_path
        self._warn_c = warn_c
        self._fail_c = fail_c
        self._alert_since = _Activation()

    def _sample(self, now: datetime) -> ProbeResult:
        temp_c = self._read_temperature()
        flags = self._throttled(now)

        status = Status.HEALTHY
        messages = [f"SoC {temp_c:.1f} °C"]
        conditions: list[tuple[str, str]] = []
        if temp_c >= self._warn_c:
            status = Status.WARN
            messages.append(f"above warning threshold {self._warn_c:.0f} °C")
        if flags is None:
            messages.append("throttle flags unavailable")
        else:
            if flags.soft_temp_limit_now:
                status = Status.WARN
                messages.append("soft temperature limit active")
                conditions.append(("SOFT_TEMP_LIMIT", "soft temperature limit active"))
            if flags.throttled_now:
                status = Status.FAIL
                messages.append("CPU throttled")
                conditions.append(("THROTTLED", "CPU throttled"))
        if temp_c >= self._fail_c:
            status = Status.FAIL
            messages.append(f"above failure threshold {self._fail_c:.0f} °C")

        alerts: tuple[AlertReport, ...] = ()
        activated = self._alert_since.update(bool(conditions), now)
        if activated is not None:
            alerts = (
                AlertReport(
                    code="THERMAL_THROTTLE",
                    description="SoC is thermally throttled; video may drop frames",
                    level=AlertLevel.WARNING,
                    conditions=tuple(conditions),
                    activated_at=activated,
                ),
            )
        cid, name = self.components[0]
        return ProbeResult(components=(_report(cid, name, status, messages, now),), alerts=alerts)

    def _read_temperature(self) -> float:
        with open(self._temp_path, encoding="ascii") as handle:
            return int(handle.read().strip()) / 1000.0


# --- Power supply ----------------------------------------------------------------


class PowerProbe(ProbeBase):
    """``power``: the supply, as judged by the firmware's under-voltage detector.

    There is no voltage reading on a Pi; the firmware only says whether the
    supply dropped below 4.63 V, now or at some point since boot. A drop *now*
    is a failure (the SoC is clocking down and USB devices may reset); one
    that happened earlier is a warning that the supply or cable is marginal.
    """

    components = (("power", "Power supply"),)

    def __init__(self, *, throttled: ThrottledSource) -> None:
        self._throttled = throttled
        self._alert_since = _Activation()

    def _sample(self, now: datetime) -> ProbeResult:
        cid, name = self.components[0]
        flags = self._throttled(now)
        if flags is None:
            report = _report(cid, name, Status.NOT_READY, ["vcgencmd unavailable"], now)
            self._alert_since.update(False, now)
            return ProbeResult(components=(report,))

        status = Status.HEALTHY
        messages: list[str] = []
        if flags.under_voltage_now:
            status = Status.FAIL
            messages.append("under-voltage now")
        else:
            if flags.under_voltage_occurred:
                status = Status.WARN
                messages.append("under-voltage since boot")
            if flags.freq_capped_occurred:
                status = Status.WARN
                messages.append("frequency capped since boot")
            if flags.throttled_occurred:
                status = Status.WARN
                messages.append("throttled since boot")
        if not messages:
            messages.append("supply OK")

        alerts: tuple[AlertReport, ...] = ()
        activated = self._alert_since.update(flags.under_voltage_now, now)
        if activated is not None:
            alerts = (
                AlertReport(
                    code="UNDER_VOLTAGE",
                    description="Power supply voltage is too low",
                    level=AlertLevel.CAUTION,
                    conditions=(("UNDER_VOLTAGE", "supply below 4.63 V"),),
                    activated_at=activated,
                ),
            )
        return ProbeResult(components=(_report(cid, name, status, messages, now),), alerts=alerts)


# --- Media pipeline --------------------------------------------------------------


class PipelineProbe(ProbeBase):
    """``camera`` and ``stream``: is the pipeline doing what the operator asked?

    Both components are judged against the operator's intent: while the
    stream is stopped on purpose they are ``NOT_READY`` ("stopped by
    operator"), not failed. While it should be on, ``ready`` (frames arriving
    from the camera) and ``pushing`` (SRT bytes flowing to Lattice) map to
    HEALTHY/FAIL, and an unobservable pipeline (MediaMTX API down) to
    ``NOT_READY`` with the pipeline's own detail line.

    The observed ``PipelineStatus`` is returned on the result so the entity's
    sensor state can be derived from the cached snapshot instead of asking
    the pipeline on every publish tick.
    """

    components = (("camera", "Camera capture"), ("stream", "SRT push"))

    def __init__(
        self,
        *,
        status: Callable[[], PipelineStatus],
        desired_on: Callable[[], bool],
    ) -> None:
        self._status = status
        self._desired_on = desired_on

    def _sample(self, now: datetime) -> ProbeResult:
        (camera_id, camera_name), (stream_id, stream_name) = self.components
        status = self._status()
        if not self._desired_on():
            stopped = ["stopped by operator"]
            return ProbeResult(
                components=(
                    _report(camera_id, camera_name, Status.NOT_READY, stopped, now),
                    _report(stream_id, stream_name, Status.NOT_READY, stopped, now),
                ),
                pipeline=status,
            )
        return ProbeResult(
            components=(
                self._judge(camera_id, camera_name, status.ready, status, "camera path", now),
                self._judge(stream_id, stream_name, status.pushing, status, "SRT push", now),
            ),
            pipeline=status,
        )

    @staticmethod
    def _judge(
        component_id: str,
        name: str,
        observed: bool | None,
        status: PipelineStatus,
        what: str,
        now: datetime,
    ) -> ComponentReport:
        if observed is None:
            # Two different "unknown"s: the API could not be reached at all
            # (ready is None too), or it answered but this signal needs a
            # second sample (the pusher's byte counter has no baseline yet).
            if status.ready is None:
                message = f"MediaMTX API unavailable: {status.detail}"
            else:
                message = f"{what} not yet confirmed, awaiting next sample: {status.detail}"
            return _report(component_id, name, Status.NOT_READY, [message], now)
        if observed:
            return _report(component_id, name, Status.HEALTHY, [status.detail], now)
        return _report(component_id, name, Status.FAIL, [f"{what} down: {status.detail}"], now)


# --- Lattice task stream ---------------------------------------------------------


class TaskStreamProbe(ProbeBase):
    """``tasking``: the ``ListenAsAgent`` stream that delivers Start/Stop tasks.

    Disconnected is ``OFFLINE`` (the camera cannot be tasked). Connected but
    with a heartbeat older than twice the requested interval is ``WARN``: the
    socket may be open while the server side is gone, and the handler will
    only notice on the next read. With heartbeats disabled there is nothing
    to age, so only the connection flag counts.
    """

    components = (("tasking", "Lattice task stream"),)
    STALE_FACTOR = 2.0

    def __init__(
        self,
        *,
        stream_state: Callable[[], TaskStreamState],
        heartbeat_interval_ms: int,
    ) -> None:
        self._stream_state = stream_state
        self._stale_after_s = (
            heartbeat_interval_ms / 1000.0 * self.STALE_FACTOR if heartbeat_interval_ms > 0 else 0
        )

    def _sample(self, now: datetime) -> ProbeResult:
        cid, name = self.components[0]
        state = self._stream_state()
        messages: list[str] = []
        if not state.connected:
            status = Status.OFFLINE
            messages.append("task stream disconnected")
            if state.last_error:
                messages.append(f"last error: {state.last_error}")
        elif state.last_heartbeat is None:
            status = Status.HEALTHY
            messages.append("connected, awaiting first heartbeat")
        else:
            age_s = (now - state.last_heartbeat).total_seconds()
            if self._stale_after_s and age_s > self._stale_after_s:
                status = Status.WARN
                messages.append(
                    f"last heartbeat {age_s:.0f} s ago (stale after {self._stale_after_s:.0f} s)"
                )
            else:
                status = Status.HEALTHY
                messages.append(f"connected, last heartbeat {age_s:.0f} s ago")
        messages.append(f"reconnects: {state.reconnects}")
        return ProbeResult(components=(_report(cid, name, status, messages, now),))


# --- Host system -----------------------------------------------------------------

DiskUsage = Callable[[str], tuple[int, int, int]]
"""``disk_usage(path) -> (total, used, free)`` in bytes; ``shutil.disk_usage`` fits."""


class SystemProbe(ProbeBase):
    """``system``: load, memory and disk of the host, with generous thresholds.

    These catch the slow failures a camera box suffers: a runaway encoder
    pegging the CPUs, a log filling the SD card, memory pressure from a leak.
    They warn early rather than fail, because the video usually keeps flowing
    for a while; the specific numbers go in the messages so an operator can
    judge the trend.
    """

    components = (("system", "Pi system"),)
    LOAD_PER_CPU_WARN = 1.5
    MEM_AVAILABLE_WARN_FRACTION = 0.10
    DISK_FREE_WARN_FRACTION = 0.10

    def __init__(
        self,
        *,
        loadavg_path: str = "/proc/loadavg",
        meminfo_path: str = "/proc/meminfo",
        disk_path: str = "/",
        cpu_count: int | None = None,
        disk_usage: DiskUsage = shutil.disk_usage,
    ) -> None:
        self._loadavg_path = loadavg_path
        self._meminfo_path = meminfo_path
        self._disk_path = disk_path
        self._cpu_count = cpu_count or os.cpu_count() or 1
        self._disk_usage = disk_usage

    def _sample(self, now: datetime) -> ProbeResult:
        cid, name = self.components[0]
        status = Status.HEALTHY
        messages: list[str] = []

        load1 = self._read_load1()
        per_cpu = load1 / self._cpu_count
        messages.append(f"load {load1:.2f} ({per_cpu:.2f} per CPU, {self._cpu_count} CPUs)")
        if per_cpu > self.LOAD_PER_CPU_WARN:
            status = Status.WARN
            messages.append("CPU overloaded")

        total_kb, available_kb = self._read_meminfo()
        mem_fraction = available_kb / total_kb if total_kb else 0.0
        messages.append(
            f"memory {available_kb / 1024:.0f} MiB available of {total_kb / 1024:.0f} MiB "
            f"({mem_fraction:.0%})"
        )
        if mem_fraction < self.MEM_AVAILABLE_WARN_FRACTION:
            status = Status.WARN
            messages.append("memory low")

        total, _used, free = self._disk_usage(self._disk_path)
        disk_fraction = free / total if total else 0.0
        gib = 1024**3
        messages.append(
            f"disk {free / gib:.1f} GiB free of {total / gib:.1f} GiB ({disk_fraction:.0%})"
        )
        if disk_fraction < self.DISK_FREE_WARN_FRACTION:
            status = Status.WARN
            messages.append("disk space low")

        return ProbeResult(components=(_report(cid, name, status, messages, now),))

    def _read_load1(self) -> float:
        with open(self._loadavg_path, encoding="ascii") as handle:
            return float(handle.read().split()[0])

    def _read_meminfo(self) -> tuple[int, int]:
        """(MemTotal, MemAvailable) in kB, as ``/proc/meminfo`` reports them."""
        values: dict[str, int] = {}
        with open(self._meminfo_path, encoding="ascii") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    values[key] = int(rest.split()[0])
        return values["MemTotal"], values["MemAvailable"]
