"""The media pipeline: how the stream is started, stopped, and observed.

``CameraControl`` decides *whether* the stream should be running; a
``MediaPipeline`` is *how* that is done on this machine, behind a three-method
seam so the control logic and its tests never touch systemd or MediaMTX:

* ``start(push_url)`` / ``stop()`` make the pipeline push to (or stop pushing
  to) the given SRT ingress. They may take a while and may fail; failures are
  ``PipelineError`` with a message fit for a task's ``error_message``.
* ``status()`` reports what is **observed** (is the camera path producing
  frames, is the pusher attached and moving bytes), never what was intended.
  It must be cheap and must never raise, because the 1 Hz publish loop and the
  health sampler call it and neither may stall.

``CommandPipeline`` is the deployment shipped with this repo: the ``Start`` /
``Stop`` tasks run shell commands (``TASK_START_COMMAND`` /
``TASK_STOP_COMMAND``, typically ``sudo systemctl restart|stop mediamtx-srt``;
``restart`` rather than ``start`` so MediaMTX re-reads the SRT target on every
Start) and a ``MediaMtxStatusProbe`` reads MediaMTX's control API for the
observed state. An empty command makes the transition state-only, which is how
the service is developed off the Pi. The push URL is also exported as
``SRT_TARGET`` into the command's environment: harmless for the systemd case
(the unit reads it from the ``EnvironmentFile`` the ingress writes) and handy
for ad-hoc commands such as a bare ``ffmpeg`` line.

The probe is read-only. Driving MediaMTX through its API (patching
``runOnReady`` and friends) is deliberately not done here: systemd owns the
pusher process, the daemon only asks it to start or stop.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ..logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PipelineStatus:
    """What the pipeline looks like right now; ``None`` means "not observable"."""

    ready: bool | None  # the camera path is producing frames
    pushing: bool | None  # the SRT pusher is attached and bytes are flowing
    detail: str  # one human-readable line, for logs and health messages
    observed_at: datetime  # UTC time the underlying sample was taken

    @classmethod
    def unknown(cls, detail: str) -> PipelineStatus:
        return cls(ready=None, pushing=None, detail=detail, observed_at=_now())


class PipelineError(Exception):
    """Raised when the pipeline could not be started or stopped."""


class MediaPipeline(Protocol):
    """The seam between camera control and the machine-specific media stack."""

    def start(self, push_url: str) -> None:
        """Bring the pipeline up pushing to ``push_url``. Raises ``PipelineError``."""

    def stop(self) -> None:
        """Bring the pipeline down. Raises ``PipelineError``."""

    def status(self) -> PipelineStatus:
        """Observed state; cheap (cached) and never raises."""


# --- shell-command pipeline ----------------------------------------------------

Opener = Callable[[str, float], bytes]
"""``opener(url, timeout_s) -> response body``; injectable so tests need no network."""


class CommandPipeline:
    """Start/stop through shell commands; status through an optional probe."""

    def __init__(
        self,
        *,
        start_command: str = "",
        stop_command: str = "",
        probe: MediaMtxStatusProbe | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._start_command = start_command.strip()
        self._stop_command = stop_command.strip()
        self._probe = probe
        self._timeout_s = timeout_s

    def start(self, push_url: str) -> None:
        env = None
        if push_url:
            env = {**os.environ, "SRT_TARGET": push_url}
        self._run(self._start_command, "start", env=env)

    def stop(self) -> None:
        self._run(self._stop_command, "stop")

    def status(self) -> PipelineStatus:
        if self._probe is None:
            return PipelineStatus.unknown("no status probe")
        return self._probe.status()

    def _run(self, command: str, action: str, *, env: dict[str, str] | None = None) -> None:
        if not command:
            logger.info("no command configured; state-only transition", action=action)
            return
        logger.info("running command", action=action, command=command)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(f"{action} command timed out after {self._timeout_s:g}s") from exc
        except OSError as exc:
            raise PipelineError(f"{action} command could not run: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise PipelineError(f"{action} command exited with {result.returncode}: {detail}")


# --- MediaMTX status probe -----------------------------------------------------


def _urllib_opener(url: str, timeout_s: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        return response.read()


def _now() -> datetime:
    return datetime.now(UTC)


class MediaMtxStatusProbe:
    """Observed camera/pusher state from MediaMTX's control API.

    ``GET {api_url}/v3/paths/get/{path}`` describes one MediaMTX path: whether
    its source is ``ready`` (frames arriving from the camera), how many
    ``bytesReceived`` from that source so far, and the ``readers`` attached to
    it (the SRT pusher launched by ``runOnReady`` shows up as one).

    Mapping:

    * ``ready`` is the path's ``ready`` flag.
    * ``pushing`` is ``True`` when there is at least one reader **and**
      ``bytesReceived`` grew since the previous successful sample. A reader
      alone is not enough: a wedged pusher stays attached while nothing flows.
      With readers but no earlier sample to compare against (first sample, or
      the counter went backwards because MediaMTX restarted) it is ``None``;
      with no readers it is ``False`` regardless of the counter.
    * Anything that stops us reading the path (connection refused, timeout,
      malformed JSON) yields ``ready=None, pushing=None`` with the reason in
      ``detail``. The one exception is an HTTP 404, which MediaMTX returns for
      a path that is not active at all: that is a definite ``ready=False``.

    Samples are cached for ``ttl_s`` so several 1 Hz callers share one request,
    and the request itself has a short ``timeout_s`` so a hung API cannot stall
    the publish loop. ``status()`` never raises.
    """

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:9997",
        path: str = "cam",
        *,
        timeout_s: float = 0.5,
        ttl_s: float = 2.0,
        opener: Opener | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._url = f"{api_url.rstrip('/')}/v3/paths/get/{path}"
        self._path = path
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s
        self._opener = opener or _urllib_opener
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._cached: PipelineStatus | None = None
        self._cached_at: float = 0.0
        self._last_bytes: int | None = None

    def status(self) -> PipelineStatus:
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached_at < self._ttl_s:
                return self._cached
            try:
                status = self._sample()
            except Exception as exc:  # never let the probe take the caller down
                status = PipelineStatus.unknown(f"mediamtx api unavailable: {exc}")
            self._cached = status
            self._cached_at = now
            return status

    def _sample(self) -> PipelineStatus:
        try:
            body = self._opener(self._url, self._timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return PipelineStatus(
                    ready=False,
                    pushing=False,
                    detail=f"mediamtx path '{self._path}' is not active",
                    observed_at=_now(),
                )
            return PipelineStatus.unknown(f"mediamtx api unavailable: HTTP {exc.code}")
        except Exception as exc:
            return PipelineStatus.unknown(f"mediamtx api unavailable: {exc}")

        try:
            data = json.loads(body)
        except ValueError as exc:
            return PipelineStatus.unknown(f"mediamtx api unavailable: invalid JSON ({exc})")
        if not isinstance(data, dict):
            return PipelineStatus.unknown("mediamtx api unavailable: unexpected JSON shape")
        return self._interpret(data)

    def _interpret(self, data: dict[str, Any]) -> PipelineStatus:
        ready = bool(data.get("ready", False))
        readers = data.get("readers") or []
        reader_count = len(readers) if isinstance(readers, list) else 0
        raw_bytes = data.get("bytesReceived")
        received = raw_bytes if isinstance(raw_bytes, int) else None

        previous, self._last_bytes = self._last_bytes, received
        pushing: bool | None
        if reader_count == 0:
            pushing = False
            flow = "no readers"
        elif received is None or previous is None:
            pushing = None
            flow = f"readers={reader_count}, no byte baseline yet"
        elif received > previous:
            pushing = True
            flow = f"readers={reader_count}, bytes flowing"
        elif received == previous:
            pushing = False
            flow = f"readers={reader_count}, no bytes since last sample"
        else:
            pushing = None  # counter went backwards: MediaMTX restarted
            flow = f"readers={reader_count}, byte counter reset"

        detail = f"{'ready' if ready else 'not ready'}, {flow}"
        return PipelineStatus(ready=ready, pushing=pushing, detail=detail, observed_at=_now())
