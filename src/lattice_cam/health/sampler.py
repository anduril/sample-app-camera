"""``HealthSampler``: runs the probes on its own cadence and caches the result.

Why a worker thread and a cache instead of probing when the entity is built?
The publish loop runs at 1 Hz and the entity expires 10 s after its last
publish, so the publish path must be near-instant, and probes are not (a
``vcgencmd`` subprocess, an HTTP call to MediaMTX). Sampling every few
seconds on a worker thread and publishing the last snapshot keeps the
heartbeat safe, and the snapshot's ``sampled_at`` tells consumers how fresh
it is.

The sampler is a ``Runtime`` worker: ``run(stop)`` loops until the stop event
is set. It is also usable synchronously (``sample_once``) so ``main.py`` can
take a first sample before the service starts and tests need no thread.

Logging is change-driven: a component's status is logged at INFO only when it
differs from the previous round, so a steady state is silent and the journal
reads as a timeline of transitions.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from ..camera.pipeline import PipelineStatus
from ..logging_setup import get_logger
from .probes import Probe
from .report import AlertReport, ComponentReport, HealthSnapshot, Status

logger = get_logger(__name__)


class HealthSampler:
    """Samples every probe on an interval and caches the latest ``HealthSnapshot``."""

    def __init__(
        self,
        probes: Sequence[Probe],
        *,
        interval_s: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._probes = list(probes)
        self._interval_s = interval_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._snapshot: HealthSnapshot | None = None
        self._last_status: dict[str, Status] = {}

    @property
    def probes(self) -> list[Probe]:
        return list(self._probes)

    def snapshot(self) -> HealthSnapshot | None:
        """The latest snapshot, or ``None`` before the first round. Never blocks on I/O."""
        with self._lock:
            return self._snapshot

    def sample_once(self, now: datetime | None = None) -> HealthSnapshot:
        """Run every probe once, cache and return the snapshot.

        Probes are contractually non-raising; one that breaks the contract is
        logged and skipped so the remaining components are still reported and
        the worker thread survives.
        """
        now = now or self._clock()
        components: list[ComponentReport] = []
        alerts: list[AlertReport] = []
        pipeline: PipelineStatus | None = None
        for probe in self._probes:
            try:
                result = probe.sample(now)
            except Exception as exc:
                logger.error("health probe raised", probe=type(probe).__name__, error=str(exc))
                continue
            components.extend(result.components)
            alerts.extend(result.alerts)
            if result.pipeline is not None:
                pipeline = result.pipeline

        snapshot = HealthSnapshot(
            components=tuple(components),
            alerts=tuple(alerts),
            sampled_at=now,
            pipeline=pipeline,
        )
        with self._lock:
            self._snapshot = snapshot
        self._log_changes(snapshot)
        return snapshot

    def run(self, stop: threading.Event) -> None:
        """Worker loop: sample, wait ``interval_s`` (or until stopped), repeat."""
        logger.info("health sampler starting", interval_seconds=self._interval_s)
        while not stop.is_set():
            self.sample_once()
            stop.wait(self._interval_s)
        logger.info("health sampler stopping")

    def _log_changes(self, snapshot: HealthSnapshot) -> None:
        for component in snapshot.components:
            previous = self._last_status.get(component.id)
            if previous is component.status:
                continue
            self._last_status[component.id] = component.status
            logger.info(
                "health component changed",
                component=component.id,
                status=component.status.name,
                previous=previous.name if previous is not None else None,
                messages=list(component.messages),
            )
