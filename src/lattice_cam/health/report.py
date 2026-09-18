"""Plain-Python health types: what a probe reports and what the sampler caches.

These are deliberately *not* the SDK's ``Health`` protobuf messages. Probes
and the sampler speak in these small frozen dataclasses so they can be unit
tested with plain equality, so the sampler's cache is immutable, and so the
translation to Lattice's wire types happens in exactly one place
(``contributor.py``). If the SDK changes shape, only that one file changes.

The vocabulary mirrors Lattice's health model: a set of *components*, each
with a status and human-readable messages, plus *alerts* (a critical change an
operator must know about, with the conditions that raised it), rolled up into
one overall status.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from ..camera.pipeline import PipelineStatus


class Status(enum.Enum):
    """Health of one component (or the roll-up). Same vocabulary as Lattice."""

    HEALTHY = "healthy"
    WARN = "warn"
    FAIL = "fail"
    OFFLINE = "offline"
    NOT_READY = "not_ready"

    @property
    def severity(self) -> int:
        """Rank for worst-of roll-ups; higher is worse."""
        return _SEVERITY[self]


# Worst-of order for the roll-up: a failed component outranks an offline one
# (something is broken vs. something is not talking), a warning outranks a
# component that simply has not reported yet, and HEALTHY only wins when
# everything is healthy.
_SEVERITY: dict[Status, int] = {
    Status.HEALTHY: 0,
    Status.NOT_READY: 1,
    Status.WARN: 2,
    Status.OFFLINE: 3,
    Status.FAIL: 4,
}


class AlertLevel(enum.Enum):
    """Lattice's three alert levels, least to most urgent."""

    ADVISORY = "advisory"
    CAUTION = "caution"
    WARNING = "warning"


@dataclass(frozen=True)
class ComponentReport:
    """One component's health at one instant, in operator-readable terms."""

    id: str  # stable machine id, e.g. ``soc-thermal``
    name: str  # display name, e.g. ``SoC temperature``
    status: Status
    messages: tuple[str, ...]  # short lines; every one is shown to an operator
    sampled_at: datetime


@dataclass(frozen=True)
class AlertReport:
    """An active alert: a critical condition an operator must be told about."""

    code: str  # machine-readable, e.g. ``THERMAL_THROTTLE``
    description: str
    level: AlertLevel
    conditions: tuple[tuple[str, str], ...]  # (condition code, description) pairs
    activated_at: datetime  # when the alert first became active, not the last sample


@dataclass(frozen=True)
class ProbeResult:
    """Everything one probe learned in one sampling round.

    ``pipeline`` is set only by the probe that observes the media pipeline. It
    rides along here so the sampler can expose the observed ``PipelineStatus``
    on the snapshot and the publish loop can read it from the cache instead of
    asking the pipeline itself.
    """

    components: tuple[ComponentReport, ...]
    alerts: tuple[AlertReport, ...] = ()
    pipeline: PipelineStatus | None = None


@dataclass(frozen=True)
class HealthSnapshot:
    """The result of one full sampling round; what the entity publishes."""

    components: tuple[ComponentReport, ...]
    alerts: tuple[AlertReport, ...]
    sampled_at: datetime
    pipeline: PipelineStatus | None = None

    @property
    def overall(self) -> Status:
        """Worst component status; ``NOT_READY`` when nothing has reported."""
        if not self.components:
            return Status.NOT_READY
        return max((c.status for c in self.components), key=lambda s: s.severity)

    def component(self, component_id: str) -> ComponentReport | None:
        return next((c for c in self.components if c.id == component_id), None)

    @property
    def is_degraded(self) -> bool:
        """True when the platform (thermal or power) is not fully healthy.

        A throttled or under-powered Pi still captures video, but at a reduced
        frame rate or with dropped frames; the sensor is reported DEGRADED
        rather than OPERATIONAL so operators know the picture may suffer.
        Only WARN and FAIL count: NOT_READY means the source is unavailable
        (no ``vcgencmd`` off the Pi), which is "unknown", not "degraded".
        """
        return any(
            c.status in DEGRADING_STATUSES for c in self.components if c.id in DEGRADING_COMPONENTS
        )


# Components whose WARN/FAIL status marks the camera sensor as DEGRADED.
DEGRADING_COMPONENTS = frozenset({"soc-thermal", "power"})
DEGRADING_STATUSES = frozenset({Status.WARN, Status.FAIL})
