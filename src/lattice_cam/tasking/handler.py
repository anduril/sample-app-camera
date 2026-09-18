"""Lattice task handling for the camera agent.

The camera entity advertises a ``task_catalog`` listing the custom task types
it accepts (see ``task-def/``): only the one that would change the stream, so
``Start`` disappears from the operator's menu once the stream is on and
``Stop`` once it is off. This module opens the ``ListenAsAgent`` stream
for that entity, dispatches each ``ExecuteRequest`` on the *name* of the task
message carried in the specification's type URL, runs the matching action on
the camera control, and drives the task through its status lifecycle:

    SENT -> EXECUTING -> DONE_OK | DONE_NOT_OK

Lifecycle invariants (see the Lattice "Integrate an agent" guide):

* Report ``EXECUTING`` as soon as a task is picked up.
* Every status update carries a strictly increasing ``status_version``. The
  counter starts from the version delivered with the task, is advanced to the
  version Lattice returns after every update, and is re-read (``GetTask``)
  when a cancel or complete request arrives, because the server bumps the
  version for those and the request carries only the task id.
* Terminal states are final; a task never stays in ``EXECUTING``.
* Start/Stop are not interruptible. A cancel that arrives while the action
  runs is honoured afterwards: the task ends ``DONE_NOT_OK`` with
  ``CANCELLED`` even though the action completed, so the operator's cancel
  always reaches a terminal state. A complete request marks the task terminal
  locally; the server already considers it done, so nothing more is sent.
* The stream is long-lived and reconnected on any error, with exponential
  backoff plus jitter (``reconnect_base_seconds`` doubling up to
  ``reconnect_cap_seconds``) so a flapping endpoint is not hammered. The
  delay resets to the base once a message arrives on the stream.
* Task execution runs on a worker thread so the stream loop never blocks.

The handler runs as a ``Runtime`` worker (``run(stop)`` on a daemon thread)
and depends only on ``client.tasks`` and an object with ``start()``/``stop()``
(the ``CameraControl``), so it can be hosted by a separate entry point later
if isolation becomes worth the cost. ``stream_state()`` gives the health
probes a thread-safe view of the connection. The task names and type-URL
helpers live in ``definitions.py``.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from anduril.taskmanager.v1.task_manager_api_pub_pb import ListenAsAgentResponse
from anduril.taskmanager.v1.task_pub_pb import ErrorCode, Status, Task

from ..logging_setup import get_logger
from .definitions import TASK_START, TASK_STOP, task_name_from_type_url, task_type_url

if TYPE_CHECKING:
    from ..lattice import LatticeClient

logger = get_logger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000
DEFAULT_RECONNECT_BASE_SECONDS = 1.0
DEFAULT_RECONNECT_CAP_SECONDS = 30.0
UPDATE_STATUS_TIMEOUT_MS = 10_000


class CameraActions(Protocol):
    """What the handler needs from the camera control: the two task actions."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class TaskStreamState:
    """Thread-safe snapshot of the agent stream, for health reporting."""

    connected: bool
    last_heartbeat: datetime | None
    last_error: str | None
    reconnects: int


class _ActiveTask:
    """Bookkeeping for one in-flight task (owned by its worker thread)."""

    def __init__(self, task: Task) -> None:
        self.task = task
        self.task_id = task.version.task_id
        self.definition_version = task.version.definition_version
        # Continue from the version the task arrived with
        # and track every higher version the server reports back.
        self.status_version = task.version.status_version
        self.cancel = threading.Event()
        self.terminal = False
        # Guards status_version and terminal: the worker thread updates them,
        # the stream thread resynchronises them on cancel/complete.
        self.lock = threading.Lock()

    def observe_version(self, status_version: int | None) -> None:
        """Advance the counter to a version the server reported, never back."""
        if status_version is None:
            return
        with self.lock:
            if status_version > self.status_version:
                self.status_version = status_version


class TaskHandler:
    """Streams tasks routed to the camera entity and executes them."""

    def __init__(
        self,
        client: LatticeClient,
        *,
        agent_entity_id: str,
        task_package: str,
        control: CameraActions,
        heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_cap_seconds: float = DEFAULT_RECONNECT_CAP_SECONDS,
    ) -> None:
        self._client = client
        self._entity_id = agent_entity_id
        self._package = task_package
        self._control = control
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._reconnect_base = max(0.0, reconnect_base_seconds)
        self._reconnect_cap = max(self._reconnect_base, reconnect_cap_seconds)

        self._actions: dict[str, Callable[[], None]] = {
            task_type_url(task_package, TASK_START): control.start,
            task_type_url(task_package, TASK_STOP): control.stop,
        }
        self._active: dict[str, _ActiveTask] = {}
        self._active_lock = threading.Lock()

        # Stream state, read by other threads via stream_state().
        self._state_lock = threading.Lock()
        self._connected = False
        self._last_heartbeat: datetime | None = None
        self._last_error: str | None = None
        self._reconnects = 0

    # -- catalog -------------------------------------------------------------

    @property
    def task_specification_urls(self) -> list[str]:
        """Every type URL the handler can execute."""
        return list(self._actions)

    def offered_task_urls(self, desired_on: bool) -> list[str]:
        """The ``task_catalog`` for the current stream state.

        Only the transition that changes the state is offered: ``Stop`` while
        the stream is on, ``Start`` while it is off. The entity is republished
        right after each transition (``CameraControl.on_change``), so the task
        just executed drops out of the operator's menu and its counterpart
        appears. A failed transition leaves the state, and so the catalog,
        unchanged: the same task stays offered as the retry.
        """
        name = TASK_STOP if desired_on else TASK_START
        return [task_type_url(self._package, name)]

    # -- stream state --------------------------------------------------------

    @property
    def last_heartbeat(self) -> datetime | None:
        return self.stream_state().last_heartbeat

    def stream_state(self) -> TaskStreamState:
        """Snapshot of the agent stream for health/verification consumers."""
        with self._state_lock:
            return TaskStreamState(
                connected=self._connected,
                last_heartbeat=self._last_heartbeat,
                last_error=self._last_error,
                reconnects=self._reconnects,
            )

    def _set_state(
        self,
        *,
        connected: bool | None = None,
        heartbeat: datetime | None = None,
        error: str | None = None,
        reconnect: bool = False,
    ) -> None:
        with self._state_lock:
            if connected is not None:
                self._connected = connected
            if heartbeat is not None:
                self._last_heartbeat = heartbeat
            if error is not None:
                self._last_error = error
            if reconnect:
                self._reconnects += 1

    # -- stream loop ---------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Consume the agent stream until ``stop`` is set, reconnecting on errors.

        A blocking read cannot be interrupted, so a shutdown is only noticed
        between messages or after a disconnect; the caller runs this on a
        daemon thread.
        """
        logger.info(
            "task handler starting",
            entity_id=self._entity_id,
            tasks=self.task_specification_urls,
        )
        delay = self._reconnect_base
        attempts = 0
        while not stop.is_set():
            if attempts:
                self._set_state(reconnect=True)
            attempts += 1
            try:
                stream = self._client.tasks.listen_as_agent(
                    self._entity_id, heartbeat_interval_ms=self._heartbeat_interval_ms
                )
                logger.info("task stream opened", entity_id=self._entity_id)
                # The request is only sent once iteration starts; the first
                # message (typically a heartbeat) proves the stream is live.
                first = True
                for response in stream:
                    if first:
                        logger.info("task stream connected", entity_id=self._entity_id)
                        self._set_state(connected=True)
                        first = False
                    # A live stream resets the backoff for the next outage.
                    delay = self._reconnect_base
                    if stop.is_set():
                        break
                    self.handle_response(response)
                self._set_state(connected=False)
                if stop.is_set():
                    break
                logger.warning("task stream ended; reconnecting", retry_in_seconds=delay)
            except Exception as exc:
                self._set_state(connected=False, error=str(exc))
                if stop.is_set():
                    break
                logger.warning(
                    "task stream error; reconnecting",
                    error=str(exc),
                    retry_in_seconds=delay,
                )
            stop.wait(self._jitter(delay))
            delay = min(delay * 2, self._reconnect_cap)
        self._set_state(connected=False)
        logger.info("task handler stopping")

    @staticmethod
    def _jitter(delay: float) -> float:
        """Randomise a backoff delay within [delay/2, delay] to spread reconnects."""
        return delay * random.uniform(0.5, 1.0)

    def handle_response(self, response: ListenAsAgentResponse) -> None:
        """Dispatch one stream message (heartbeat, execute, cancel, complete)."""
        if response.has_field("heartbeat"):
            self._set_state(heartbeat=datetime.now(UTC))
            logger.debug("task stream heartbeat")
            return

        request = response.request
        if request is None:
            return
        if request.field == "execute_request":
            task = request.value.task
            if task is None or task.version is None or not task.version.task_id:
                logger.warning("execute request without a task id; ignoring")
                return
            self._start_worker(task)
        elif request.field == "cancel_request":
            self._on_cancel(request.value.task_id)
        elif request.field == "complete_request":
            self._on_complete(request.value.task_id)
        else:
            logger.warning("unknown task stream request", field=request.field)

    # -- execution -----------------------------------------------------------

    def _start_worker(self, task: Task) -> None:
        active = _ActiveTask(task)
        with self._active_lock:
            if active.task_id in self._active:
                logger.info("task already in progress; ignoring duplicate", task_id=active.task_id)
                return
            self._active[active.task_id] = active
        threading.Thread(
            target=self.execute,
            args=(active,),
            name=f"task-{active.task_id[:8]}",
            daemon=True,
        ).start()

    def execute(self, active: _ActiveTask) -> None:
        """Run one task to a terminal state (called on a worker thread)."""
        task = active.task
        spec = task.specification
        type_url = spec.type_url if spec is not None else ""
        name = task_name_from_type_url(type_url) if type_url else "<none>"
        logger.info(
            "task received",
            task_id=active.task_id,
            task=name,
            type_url=type_url,
            display_name=task.display_name,
            status_version=active.status_version,
        )
        try:
            action = self._actions.get(type_url)
            if action is None:
                self._update(
                    active,
                    Status.DONE_NOT_OK,
                    error=f"unsupported task type: {type_url or '<none>'}",
                )
                return

            self._update(active, Status.EXECUTING)
            if active.cancel.is_set():
                self._update(
                    active,
                    Status.DONE_NOT_OK,
                    error="cancelled before execution",
                    error_code=ErrorCode.CANCELLED,
                )
                return

            action()
            if active.cancel.is_set():
                # The action is not interruptible and has already taken
                # effect; the operator's cancel still gets its terminal state.
                self._update(
                    active,
                    Status.DONE_NOT_OK,
                    error="cancelled by operator; the action had already completed",
                    error_code=ErrorCode.CANCELLED,
                )
            else:
                self._update(active, Status.DONE_OK)
        except Exception as exc:
            logger.error("task failed", task_id=active.task_id, task=name, error=str(exc))
            if not active.terminal:
                try:
                    self._update(active, Status.DONE_NOT_OK, error=str(exc))
                except Exception as update_exc:
                    logger.error(
                        "could not report task failure",
                        task_id=active.task_id,
                        error=str(update_exc),
                    )
        finally:
            with self._active_lock:
                self._active.pop(active.task_id, None)

    def _on_cancel(self, task_id: str) -> None:
        with self._active_lock:
            active = self._active.get(task_id)
        if active is None:
            # Start/Stop complete almost instantly, so a cancel usually arrives
            # after the task is already terminal; nothing further to report.
            logger.info("cancel request for a task that is not in progress", task_id=task_id)
            return
        logger.info("cancel requested", task_id=task_id)
        # The server has moved the task to a new status version; learn it so
        # the worker's terminal update is still strictly greater.
        self._resync_version(active)
        active.cancel.set()

    def _on_complete(self, task_id: str) -> None:
        with self._active_lock:
            active = self._active.get(task_id)
        if active is None:
            logger.info("complete request for a task that is not in progress", task_id=task_id)
            return
        logger.info("complete request received; task is terminal server-side", task_id=task_id)
        self._resync_version(active)
        with active.lock:
            active.terminal = True

    def _resync_version(self, active: _ActiveTask) -> None:
        """Re-read the task and adopt a higher server-side status version."""
        try:
            task = self._client.tasks.get_task(active.task_id, timeout_ms=UPDATE_STATUS_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(
                "could not re-read task version; continuing with the local counter",
                task_id=active.task_id,
                error=str(exc),
            )
            return
        version = task.version if task is not None else None
        active.observe_version(version.status_version if version is not None else None)

    def _update(
        self,
        active: _ActiveTask,
        status: Status,
        *,
        error: str | None = None,
        error_code: ErrorCode = ErrorCode.FAILED,
    ) -> None:
        with active.lock:
            if active.terminal:
                return
            active.status_version += 1
            status_version = active.status_version
        # The RPC runs outside the task lock; a concurrent resync can only
        # raise the counter, which the max() below preserves.
        returned = self._client.tasks.update_task_status(
            task_id=active.task_id,
            definition_version=active.definition_version,
            status_version=status_version,
            status=status,
            agent_entity_id=self._entity_id,
            error_message=error,
            error_code=error_code,
            timeout_ms=UPDATE_STATUS_TIMEOUT_MS,
        )
        active.observe_version(getattr(returned, "status_version", None))
        if status in (Status.DONE_OK, Status.DONE_NOT_OK):
            with active.lock:
                active.terminal = True
        logger.info(
            "task status updated",
            task_id=active.task_id,
            status=status.name,
            status_version=status_version,
            error=error,
        )
