"""Runtime lifecycle: publish loop, background workers, signals, graceful shutdown.

Signals are handled on the main thread (a Python requirement); the 1 Hz
publish loop and every registered worker run on their own daemon thread and
share one stop event. The service knows nothing about what the workers do
(the task stream, a health sampler): a worker is any callable that runs until
the stop event is set.

Configuration is read once at startup; changing it requires a restart. There
is deliberately no SIGHUP reload: the client, entity identity and workers are
all built from the config before ``run()``, so swapping the ``Config`` object
in place would never take effect.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .config import Config
from .entity.builder import EntityBuilder
from .logging_setup import get_logger

if TYPE_CHECKING:
    from .lattice import LatticeClient

logger = get_logger(__name__)

PUBLISH_INTERVAL_SECONDS = 1.0
PUBLISH_TIMEOUT_MS = 1000
SHUTDOWN_TIMEOUT_SECONDS = 30.0
# After the first failed publish, repeat the ERROR only every Nth consecutive
# failure so a long outage does not flood the journal at 1 Hz.
FAILURE_LOG_EVERY = 30

# A background worker: runs on its own daemon thread until the stop event is set.
Worker = Callable[[threading.Event], None]


class Runtime:
    """The long-running runtime: publish loop, workers, signals, shutdown."""

    def __init__(
        self,
        config: Config,
        client: LatticeClient,
        *,
        builder: EntityBuilder,
        workers: Sequence[tuple[str, Worker]] = (),
    ) -> None:
        self._config = config
        self._client = client
        self._builder = builder
        self._workers = list(workers)

        self._stop = threading.Event()
        self._publisher_thread: threading.Thread | None = None
        self._worker_threads: list[threading.Thread] = []
        self._start_time = 0.0
        self._consecutive_failures = 0

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> int:
        """Start the publisher and workers, block until a shutdown signal arrives."""
        self._start_time = time.monotonic()
        logger.info(
            "service starting",
            endpoint=self._config.lattice_endpoint,
            start_time=datetime.now(UTC).isoformat(),
        )

        self._install_signal_handlers()

        self._publisher_thread = threading.Thread(
            target=self._publisher, name="publisher", daemon=True
        )
        self._publisher_thread.start()
        self.start_workers()
        logger.info("service started, waiting for signals")

        while not self._stop.is_set():
            time.sleep(0.2)

        return self._shutdown()

    def start_workers(self) -> None:
        """Start every registered worker on its own named daemon thread.

        Daemon: a worker blocked in a stream read cannot be interrupted, so
        its thread is abandoned at exit rather than joined.
        """
        for name, worker in self._workers:
            thread = threading.Thread(target=worker, args=(self._stop,), name=name, daemon=True)
            thread.start()
            self._worker_threads.append(thread)
            logger.info("worker started", worker=name)

    def stop(self) -> None:
        """Ask the publisher and every worker to stop (idempotent)."""
        self._stop.set()

    def _shutdown(self) -> int:
        logger.info("initiating shutdown")
        self._stop.set()

        if self._publisher_thread is not None:
            self._publisher_thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            if self._publisher_thread.is_alive():
                logger.error("shutdown timeout exceeded")
                return 1

        uptime = time.monotonic() - self._start_time
        logger.info("shutdown completed", uptime_seconds=round(uptime, 1))
        return 0

    # -- signals -------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._on_terminate)
        signal.signal(signal.SIGTERM, self._on_terminate)

    def _on_terminate(self, signum, _frame) -> None:
        logger.info("received signal", signal=signal.Signals(signum).name)
        self._stop.set()

    # -- publishing ----------------------------------------------------------

    @property
    def entity_id(self) -> str:
        return self._builder.entity_id

    def publish_now(self, *, timeout_ms: int = PUBLISH_TIMEOUT_MS) -> None:
        """Publish the entity immediately from the current observed state.

        Used by Start/Stop transitions so the asset's Media item, sensor
        state and task catalog (the task just executed is withdrawn, its
        counterpart offered) change as part of the task rather than on the
        next tick. Raises on failure; the periodic loop keeps republishing
        regardless.
        """
        request = self._builder.build()
        self._client.entities.publish_entity(request, timeout_ms=timeout_ms)

    def publish_offline(self, *, timeout_ms: int = PUBLISH_TIMEOUT_MS) -> None:
        """Best-effort final publish marking the camera off/offline at shutdown.

        Operators then see the asset go dark deliberately rather than expire.
        Never raises: at this point there is nothing left to do about a
        failure but log it.
        """
        try:
            request = self._builder.build(offline=True)
            self._client.entities.publish_entity(request, timeout_ms=timeout_ms)
            logger.info("published offline entity", entity_id=self.entity_id)
        except Exception as exc:
            logger.warning("offline publish failed", entity_id=self.entity_id, error=str(exc))

    # -- publisher loop ------------------------------------------------------

    def _publisher(self) -> None:
        entity_id = self.entity_id
        logger.info(
            "publisher initialized",
            entity_id=entity_id,
            created_at=self._builder.created_time.isoformat(),
        )

        # Tick every PUBLISH_INTERVAL_SECONDS; wait() returns True when stopped.
        while not self._stop.wait(PUBLISH_INTERVAL_SECONDS):
            self._tick(entity_id)

        logger.info("publisher stopping")

    def _tick(self, entity_id: str) -> None:
        """One publish attempt with failure-count-aware logging."""
        try:
            self.publish_now()
        except Exception as exc:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if count == 1 or count % FAILURE_LOG_EVERY == 0:
                logger.error(
                    "publish failed",
                    entity_id=entity_id,
                    consecutive_failures=count,
                    error=str(exc),
                )
            return

        if self._consecutive_failures:
            logger.info(
                "publish recovered",
                entity_id=entity_id,
                failures=self._consecutive_failures,
            )
            self._consecutive_failures = 0
        logger.debug("publish completed", entity_id=entity_id)
