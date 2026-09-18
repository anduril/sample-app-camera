"""Tests for the task handler: dispatch by type URL, the status lifecycle, and
the reconnecting stream loop.

A fake client (``.tasks`` only, per the LatticeClient contract) records every
status update so the tests can assert the exact sequence of statuses and that
status_version strictly increases from the version the task arrived with. A
small ``FakeControl`` stands in for the camera control so this file does not
depend on ``camera/control.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from anduril.taskmanager.v1.task_api_pub_pb import CancelRequest, ExecuteRequest
from anduril.taskmanager.v1.task_manager_api_pub_pb import Heartbeat, ListenAsAgentResponse
from anduril.taskmanager.v1.task_pub_pb import ErrorCode, Status, Task, TaskVersion
from protobuf import Oneof
from protobuf.wkt import any_pb

from lattice_cam.tasking.definitions import task_name_from_type_url, task_type_url
from lattice_cam.tasking.handler import TaskHandler, TaskStreamState

PACKAGE = "anduril.sample_app_rpi_cam.camera.v1alpha"
AGENT = "rpi-cam-01"


class FakeTasks:
    """Stands in for ``client.tasks``."""

    def __init__(self, fail_update: bool = False, streams=None) -> None:
        self.updates: list[dict] = []
        self.fail_update = fail_update
        # One callable per listen_as_agent call; each returns an iterator or raises.
        self.streams = list(streams or [])
        self.listen_calls = 0
        # The server may report a higher status version than the one sent
        # (returned_bump), and get_task reports server_version when set.
        self.returned_bump = 0
        self.server_version: int | None = None
        self.fail_get = False
        self.get_calls: list[str] = []

    def update_task_status(self, **kwargs):
        if self.fail_update:
            raise RuntimeError("update_status unavailable")
        self.updates.append(kwargs)
        return TaskVersion(
            task_id=kwargs["task_id"],
            status_version=kwargs["status_version"] + self.returned_bump,
        )

    def get_task(self, task_id: str, *, timeout_ms=None):
        self.get_calls.append(task_id)
        if self.fail_get:
            raise RuntimeError("get_task unavailable")
        return Task(
            version=TaskVersion(
                task_id=task_id, definition_version=1, status_version=self.server_version or 0
            )
        )

    def listen_as_agent(self, entity_id: str, *, heartbeat_interval_ms: int):
        self.listen_calls += 1
        if not self.streams:
            raise AssertionError("no fake stream scripted for this call")
        return self.streams.pop(0)()


class FakeClient:
    def __init__(self, fail_update: bool = False, streams=None) -> None:
        self.tasks = FakeTasks(fail_update=fail_update, streams=streams)

    @property
    def updates(self) -> list[dict]:
        return self.tasks.updates


class FakeControl:
    """Minimal camera control: a flag plus optional failure on stop."""

    def __init__(self, streaming: bool = True, stop_error: str | None = None) -> None:
        self.streaming = streaming
        self.stop_error = stop_error

    def start(self) -> None:
        self.streaming = True

    def stop(self) -> None:
        if self.stop_error:
            raise RuntimeError(self.stop_error)
        self.streaming = False


class RecordingEvent(threading.Event):
    """A stop event that records the backoff delays the handler waits for."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout=None):  # noqa: D102
        self.waits.append(timeout)
        return super().wait(0)


def _execute_response(task_id: str, type_url: str | None, status_version: int = 3):
    spec = any_pb.Any(type_url=type_url, value=b"") if type_url else None
    task = Task(
        version=TaskVersion(task_id=task_id, definition_version=1, status_version=status_version),
        display_name=f"test {task_id}",
        specification=spec,
    )
    return ListenAsAgentResponse(request=Oneof("execute_request", ExecuteRequest(task=task)))


def _heartbeat() -> ListenAsAgentResponse:
    return ListenAsAgentResponse(heartbeat=Heartbeat())


def _handler(client, control=None, **kwargs):
    control = control or FakeControl(streaming=True)
    handler = TaskHandler(
        client, agent_entity_id=AGENT, task_package=PACKAGE, control=control, **kwargs
    )
    return handler, control


def _drain_threads():
    for t in threading.enumerate():
        if t.name.startswith("task-"):
            t.join(timeout=5)


def test_type_url_helpers():
    url = task_type_url(PACKAGE, "Start")
    assert url == "type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Start"
    assert task_name_from_type_url(url) == "Start"


def test_catalog_advertises_start_and_stop():
    handler, _ = _handler(FakeClient())
    assert handler.task_specification_urls == [
        task_type_url(PACKAGE, "Start"),
        task_type_url(PACKAGE, "Stop"),
    ]


def test_stop_task_runs_to_done_ok_and_flips_state():
    client = FakeClient()
    handler, control = _handler(client)
    assert control.streaming is True

    handler.handle_response(
        _execute_response("t-1", task_type_url(PACKAGE, "Stop"), status_version=3)
    )
    _drain_threads()

    assert control.streaming is False
    statuses = [u["status"] for u in client.updates]
    assert statuses == [Status.EXECUTING, Status.DONE_OK]
    versions = [u["status_version"] for u in client.updates]
    assert versions == [4, 5]  # strictly increasing from the delivered version
    assert all(u["task_id"] == "t-1" for u in client.updates)
    assert all(u["definition_version"] == 1 for u in client.updates)
    assert all(u["agent_entity_id"] == AGENT for u in client.updates)
    assert client.updates[-1]["error_message"] is None


def test_start_task_after_stop_restores_state():
    client = FakeClient()
    handler, control = _handler(client, FakeControl(streaming=False))
    handler.handle_response(_execute_response("t-2", task_type_url(PACKAGE, "Start")))
    _drain_threads()
    assert control.streaming is True
    assert [u["status"] for u in client.updates] == [Status.EXECUTING, Status.DONE_OK]


def test_unsupported_task_type_is_done_not_ok():
    client = FakeClient()
    handler, control = _handler(client)
    handler.handle_response(
        _execute_response("t-3", "type.googleapis.com/anduril.tasks.v2.Investigate")
    )
    _drain_threads()
    assert control.streaming is True
    assert [u["status"] for u in client.updates] == [Status.DONE_NOT_OK]
    assert "unsupported task type" in client.updates[0]["error_message"]


def test_missing_specification_is_done_not_ok():
    client = FakeClient()
    handler, _ = _handler(client)
    handler.handle_response(_execute_response("t-4", None))
    _drain_threads()
    assert [u["status"] for u in client.updates] == [Status.DONE_NOT_OK]


def test_failing_action_reports_done_not_ok():
    client = FakeClient()
    control = FakeControl(streaming=True, stop_error="stop command exited with 3")
    handler, _ = _handler(client, control)
    handler.handle_response(_execute_response("t-5", task_type_url(PACKAGE, "Stop")))
    _drain_threads()
    assert control.streaming is True  # state unchanged on failure
    assert [u["status"] for u in client.updates] == [Status.EXECUTING, Status.DONE_NOT_OK]
    assert "exited with 3" in client.updates[-1]["error_message"]


def test_heartbeat_and_cancel_for_unknown_task_are_harmless():
    client = FakeClient()
    handler, _ = _handler(client)
    assert handler.last_heartbeat is None
    handler.handle_response(_heartbeat())
    assert handler.last_heartbeat is not None
    handler.handle_response(
        ListenAsAgentResponse(request=Oneof("cancel_request", CancelRequest(task_id="nope")))
    )
    assert client.updates == []


def test_update_failure_does_not_crash_worker():
    client = FakeClient(fail_update=True)
    handler, control = _handler(client)
    handler.handle_response(_execute_response("t-6", task_type_url(PACKAGE, "Stop")))
    _drain_threads()
    # The action never ran because EXECUTING could not be reported first.
    assert control.streaming is True


def test_stream_state_after_heartbeat():
    handler, _ = _handler(FakeClient())
    before = handler.stream_state()
    assert before == TaskStreamState(
        connected=False, last_heartbeat=None, last_error=None, reconnects=0
    )
    handler.handle_response(_heartbeat())
    after = handler.stream_state()
    assert after.last_heartbeat is not None
    assert after.last_heartbeat == handler.last_heartbeat
    assert after.connected is False  # only run() marks the stream connected
    assert after.reconnects == 0


def test_run_reconnects_with_backoff_and_tracks_state():
    stop = RecordingEvent()
    base, cap = 0.01, 0.02

    def failing_stream() -> Iterator[ListenAsAgentResponse]:
        raise ConnectionError("dial failed")

    def failing_stream_2() -> Iterator[ListenAsAgentResponse]:
        raise ConnectionError("dial failed again")

    seen_connected: list[bool] = []

    def live_stream() -> Iterator[ListenAsAgentResponse]:
        yield _heartbeat()
        seen_connected.append(handler.stream_state().connected)
        stop.set()
        yield _heartbeat()  # never consumed: the loop checks stop first

    client = FakeClient(streams=[failing_stream, failing_stream_2, live_stream])
    handler, _ = _handler(client, reconnect_base_seconds=base, reconnect_cap_seconds=cap)

    handler.run(stop)

    assert client.tasks.listen_calls == 3
    # Two failures -> two backoff waits: base, then doubled (capped); each jittered
    # into [nominal/2, nominal].
    assert len(stop.waits) == 2
    assert base / 2 <= stop.waits[0] <= base
    assert cap / 2 <= stop.waits[1] <= cap
    assert seen_connected == [True]
    state = handler.stream_state()
    assert state.reconnects == 2
    assert state.connected is False  # stream closed on shutdown
    assert state.last_error == "dial failed again"
    assert state.last_heartbeat is not None


def test_run_resets_backoff_after_a_message():
    stop = RecordingEvent()
    base, cap = 0.01, 0.08

    def failing() -> Iterator[ListenAsAgentResponse]:
        raise ConnectionError("down")

    def brief() -> Iterator[ListenAsAgentResponse]:
        yield _heartbeat()  # stream ends normally after one message

    def final() -> Iterator[ListenAsAgentResponse]:
        yield _heartbeat()
        stop.set()

    client = FakeClient(streams=[failing, failing, failing, brief, final])
    handler, _ = _handler(client, reconnect_base_seconds=base, reconnect_cap_seconds=cap)
    handler.run(stop)

    # Three failures: base, 2*base, 4*base; then `brief` delivered a message,
    # so the wait after it ended is back at the base delay.
    assert len(stop.waits) == 4
    assert base / 2 <= stop.waits[0] <= base
    assert base <= stop.waits[1] <= 2 * base
    assert 2 * base <= stop.waits[2] <= 4 * base
    assert base / 2 <= stop.waits[3] <= base
    assert handler.stream_state().reconnects == 4


# --- status versions vs the server, cancel and complete mid-action ------------


class BlockingControl(FakeControl):
    """A control whose stop() blocks until the test releases it, so a cancel or
    complete request can arrive while the action is running."""

    def __init__(self) -> None:
        super().__init__(streaming=True)
        self.started = threading.Event()
        self.release = threading.Event()

    def stop(self) -> None:
        self.started.set()
        assert self.release.wait(5.0), "test never released the action"
        self.streaming = False


def _cancel(task_id: str) -> ListenAsAgentResponse:
    return ListenAsAgentResponse(request=Oneof("cancel_request", CancelRequest(task_id=task_id)))


def _complete(task_id: str) -> ListenAsAgentResponse:
    from anduril.taskmanager.v1.task_api_pub_pb import CompleteRequest

    return ListenAsAgentResponse(
        request=Oneof("complete_request", CompleteRequest(task_id=task_id))
    )


def test_update_adopts_a_higher_server_reported_version():
    client = FakeClient()
    client.tasks.returned_bump = 5  # server answers EXECUTING@4 with version 9
    handler, control = _handler(client)
    handler.handle_response(_execute_response("t-10", task_type_url(PACKAGE, "Stop"), 3))
    _drain_threads()
    assert [u["status"] for u in client.updates] == [Status.EXECUTING, Status.DONE_OK]
    assert [u["status_version"] for u in client.updates] == [4, 10]


def test_cancel_during_action_ends_done_not_ok_cancelled_above_server_version():
    client = FakeClient()
    control = BlockingControl()
    handler, _ = _handler(client, control)
    handler.handle_response(_execute_response("t-11", task_type_url(PACKAGE, "Stop"), 3))
    assert control.started.wait(5.0)

    # The operator cancels: the server has moved the task to version 7.
    client.tasks.server_version = 7
    handler.handle_response(_cancel("t-11"))
    control.release.set()
    _drain_threads()

    assert client.tasks.get_calls == ["t-11"]
    assert control.streaming is False  # the action is not interruptible
    statuses = [u["status"] for u in client.updates]
    assert statuses == [Status.EXECUTING, Status.DONE_NOT_OK]
    final = client.updates[-1]
    assert final["status_version"] == 8  # strictly above the re-read version
    assert final["error_code"] == ErrorCode.CANCELLED
    assert "cancelled by operator" in final["error_message"]


def test_complete_request_marks_terminal_and_suppresses_the_final_update():
    client = FakeClient()
    control = BlockingControl()
    handler, _ = _handler(client, control)
    handler.handle_response(_execute_response("t-12", task_type_url(PACKAGE, "Stop"), 3))
    assert control.started.wait(5.0)

    client.tasks.server_version = 6
    handler.handle_response(_complete("t-12"))
    control.release.set()
    _drain_threads()

    assert [u["status"] for u in client.updates] == [Status.EXECUTING]
    assert client.tasks.get_calls == ["t-12"]


def test_get_task_failure_during_cancel_is_tolerated():
    client = FakeClient()
    client.tasks.fail_get = True
    control = BlockingControl()
    handler, _ = _handler(client, control)
    handler.handle_response(_execute_response("t-13", task_type_url(PACKAGE, "Stop"), 3))
    assert control.started.wait(5.0)
    handler.handle_response(_cancel("t-13"))
    control.release.set()
    _drain_threads()

    statuses = [u["status"] for u in client.updates]
    assert statuses == [Status.EXECUTING, Status.DONE_NOT_OK]
    assert [u["status_version"] for u in client.updates] == [4, 5]  # local counter
    assert client.updates[-1]["error_code"] == ErrorCode.CANCELLED
