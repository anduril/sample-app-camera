"""Tests for the Start/Stop transitions: ingress lifecycle ordering and failures.

A fake Lattice client records every VideoManager call so the tests can assert
that Start registers a fresh ingress (new id each time) and writes the SRT
target *before* the pipeline is started, that Stop stops the pipeline *before*
archiving the ingress, that the ingress record is persisted so a crash can be
recovered at the next boot, and that reading the state never waits on an RPC.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from lattice_cam.camera.control import (
    CameraControl,
    CameraControlError,
    ControlSnapshot,
)
from lattice_cam.camera.ingress import STATE_KEY, VideoIngress
from lattice_cam.camera.pipeline import CommandPipeline, PipelineError
from lattice_cam.lattice import SrtIngressInfo
from lattice_cam.state import StateStore


class FakeVideoClient:
    """Stands in for ``LatticeClient.video``; returns server-shaped ids."""

    def __init__(self, *, fail_create=False, fail_delete=False) -> None:
        self.calls: list[tuple] = []
        self.fail_create = fail_create
        self.fail_delete = fail_delete
        # When set, create_srt_ingress blocks until ``release`` is set so a
        # test can observe the control state mid-RPC.
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def create_srt_ingress(self, *, title, passphrase="", ingress_id=None, timeout_ms=None):
        self.calls.append(("create", ingress_id))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(5.0), "test never released the fake RPC"
        if self.fail_create:
            raise RuntimeError("VideoManager unavailable")
        video_id = f"{ingress_id}#1"  # Lattice returns <client id>#<suffix>
        return SrtIngressInfo(
            video_id=video_id, push_url=f"srt://lattice:8890?streamid={video_id}", session_id="s"
        )

    def delete_srt_ingress(self, ingress_id, *, timeout_ms=None):
        self.calls.append(("delete", ingress_id))
        if self.fail_delete:
            raise RuntimeError("VideoManager unavailable")


class FakeClient:
    def __init__(self, **kwargs) -> None:
        self.video = FakeVideoClient(**kwargs)

    @property
    def calls(self) -> list[tuple]:
        return self.video.calls


def _setup(tmp_path, *, start_command="", stop_command="", with_state=True, **client_kwargs):
    client = FakeClient(**client_kwargs)
    target = tmp_path / "srt_target.env"
    state = StateStore(str(tmp_path / "state.json")) if with_state else None
    video = VideoIngress(client, title="cam", srt_target_file=str(target), state=state)
    pipeline = CommandPipeline(start_command=start_command, stop_command=stop_command)
    control = CameraControl(pipeline=pipeline, video=video)
    return client, video, control, target


def test_start_registers_fresh_ingress_and_writes_target_before_command(tmp_path):
    marker = tmp_path / "seen_target"
    # The start command copies the target file: it must already exist.
    client, video, control, target = _setup(
        tmp_path, start_command=f"cp '{tmp_path}/srt_target.env' '{marker}'"
    )
    control.start()

    snap = control.snapshot()
    assert snap == ControlSnapshot(desired_on=True, video_id=snap.video_id, in_transition=False)
    assert client.calls[0][0] == "create"
    ingress_id = client.calls[0][1]
    # Lattice accepts 4-36 characters: a bare UUID4 string.
    uuid.UUID(ingress_id)
    assert len(ingress_id) == 36
    # The advertised id is the one Lattice returned, not the one we sent.
    assert snap.video_id == f"{ingress_id}#1"
    assert target.read_text() == f"SRT_TARGET=srt://lattice:8890?streamid={ingress_id}#1\n"
    assert marker.read_text() == target.read_text()


def test_start_exports_push_url_to_the_command_environment(tmp_path):
    seen = tmp_path / "seen_env"
    client, video, control, target = _setup(
        tmp_path, start_command=f"echo \"$SRT_TARGET\" > '{seen}'"
    )
    control.start()
    assert seen.read_text().strip() == video.info.push_url


def test_srt_target_file_is_replaced_even_when_the_old_one_is_read_only(tmp_path):
    # A root-owned file from an earlier privileged run must not block the
    # unprivileged daemon: the file is replaced by rename, never opened for
    # writing in place.
    client, video, control, target = _setup(tmp_path)
    target.write_text("SRT_TARGET=srt://stale\n")
    target.chmod(0o444)
    control.start()
    assert target.read_text() == f"SRT_TARGET={video.info.push_url}\n"
    assert not (tmp_path / "srt_target.env.tmp").exists()


def test_stop_runs_command_then_archives_and_clears_video(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    ingress_id = control.snapshot().video_id
    control.stop()

    assert control.snapshot() == ControlSnapshot(
        desired_on=False, video_id=None, in_transition=False
    )
    assert client.calls == [("create", ingress_id[:-2]), ("delete", ingress_id)]


def test_restart_uses_a_new_ingress_id(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    first = control.snapshot().video_id
    control.stop()
    control.start()
    second = control.snapshot().video_id
    assert first != second
    assert [c[0] for c in client.calls] == ["create", "delete", "create"]


def test_start_failure_on_ingress_leaves_state_unchanged(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_create=True)
    with pytest.raises(CameraControlError, match="could not register video ingress"):
        control.start()
    assert control.snapshot() == ControlSnapshot(
        desired_on=False, video_id=None, in_transition=False
    )
    assert not target.exists()


def test_start_command_failure_archives_the_new_ingress(tmp_path):
    client, video, control, target = _setup(tmp_path, start_command="exit 7")
    with pytest.raises(CameraControlError, match="exited with 7"):
        control.start()
    assert control.snapshot().desired_on is False
    assert control.snapshot().video_id is None
    assert [c[0] for c in client.calls] == ["create", "delete"]


def test_stop_command_failure_keeps_ingress(tmp_path):
    client, video, control, target = _setup(tmp_path, stop_command="false")
    control.start()
    with pytest.raises(CameraControlError):
        control.stop()
    assert control.snapshot().desired_on is True
    assert control.snapshot().video_id is not None
    assert [c[0] for c in client.calls] == ["create"]


def test_archive_failure_reports_stopped_but_keeps_id_for_retry(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_delete=True)
    control.start()
    ingress_id = control.snapshot().video_id
    with pytest.raises(CameraControlError, match="could not be archived"):
        control.stop()
    assert control.snapshot().desired_on is False
    assert control.snapshot().video_id == ingress_id  # retained so a retry can archive it
    client.video.fail_delete = False
    control.stop()  # retry succeeds
    assert control.snapshot().video_id is None


def test_shutdown_is_best_effort(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_delete=True, stop_command="false")
    control.start()
    control.shutdown()  # must not raise
    assert control.snapshot().desired_on is False
    assert [c[0] for c in client.calls] == ["create", "delete"]


def test_without_video_or_pipeline_transitions_are_state_only():
    control = CameraControl()
    control.start()
    assert control.snapshot() == ControlSnapshot(
        desired_on=True, video_id=None, in_transition=False
    )
    control.stop()
    assert control.snapshot().desired_on is False


def test_initial_desired_state_is_honoured():
    assert CameraControl(desired_on=True).snapshot().desired_on is True


# --- commands --------------------------------------------------------------------


def test_camera_control_commands():
    ok = CameraControl(pipeline=CommandPipeline(start_command="true", stop_command="false"))
    ok.start()
    assert ok.snapshot().desired_on is True
    with pytest.raises(CameraControlError, match="stop command exited with 1"):
        ok.stop()
    # A failed stop leaves the desired state untouched.
    assert ok.snapshot().desired_on is True


def test_pipeline_errors_are_reported_as_control_errors():
    class Broken:
        def start(self, push_url):
            raise PipelineError("boom")

        def stop(self):
            raise PipelineError("bang")

        def status(self):
            raise AssertionError("not called by control")

    control = CameraControl(pipeline=Broken())
    with pytest.raises(CameraControlError, match="boom"):
        control.start()
    control = CameraControl(pipeline=Broken(), desired_on=True)
    with pytest.raises(CameraControlError, match="bang"):
        control.stop()


# --- state persistence and recovery ----------------------------------------------


def _state_of(video: VideoIngress) -> StateStore:
    return video._state  # the store the ingress was built with


def test_create_persists_the_ingress_record(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    record = _state_of(video).get(STATE_KEY)
    info = video.info
    assert record["video_id"] == info.video_id
    assert record["push_url"] == info.push_url
    assert record["session_id"] == info.session_id
    assert "created_at" in record
    # It really is on disk: a fresh store sees it.
    assert StateStore(str(tmp_path / "state.json")).get(STATE_KEY) == record


def test_delete_clears_the_ingress_record(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    control.stop()
    assert _state_of(video).get(STATE_KEY) is None


def test_archive_failure_keeps_the_ingress_record(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_delete=True)
    control.start()
    with pytest.raises(CameraControlError):
        control.stop()
    assert _state_of(video).get(STATE_KEY)["video_id"] == control.snapshot().video_id


def test_recover_archives_a_persisted_orphan_and_clears_it(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.set(STATE_KEY, {"video_id": "old#7", "push_url": "srt://x", "session_id": "s"})
    client = FakeClient()
    video = VideoIngress(client, title="cam", state=state)
    control = CameraControl(video=video)

    control.recover()

    assert client.calls == [("delete", "old#7")]
    assert state.get(STATE_KEY) is None
    assert control.snapshot().video_id is None


def test_recover_keeps_the_record_when_archiving_fails(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    record = {"video_id": "old#7", "push_url": "srt://x", "session_id": "s"}
    state.set(STATE_KEY, record)
    client = FakeClient(fail_delete=True)
    control = CameraControl(video=VideoIngress(client, title="cam", state=state))

    control.recover()  # must not raise

    assert client.calls == [("delete", "old#7")]
    assert state.get(STATE_KEY) == record


def test_recover_is_a_noop_without_a_record(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    client = FakeClient()
    CameraControl(video=VideoIngress(client, title="cam", state=state)).recover()
    assert client.calls == []
    CameraControl(video=VideoIngress(client, title="cam", state=None)).recover()
    CameraControl().recover()
    assert client.calls == []


def test_recover_ignores_a_malformed_record(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.set(STATE_KEY, "not a record")
    client = FakeClient()
    CameraControl(video=VideoIngress(client, title="cam", state=state)).recover()
    assert client.calls == []


def test_recover_never_archives_the_live_ingress(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    control.recover()
    assert [c[0] for c in client.calls] == ["create"]
    assert control.snapshot().video_id is not None


# --- the lock rule: reads never wait on an RPC -------------------------------


def test_snapshot_does_not_block_while_a_transition_is_inside_an_rpc(tmp_path):
    client, video, control, target = _setup(tmp_path)
    client.video.entered = threading.Event()
    client.video.release = threading.Event()

    errors: list[BaseException] = []

    def run_start():
        try:
            control.start()
        except BaseException as exc:  # pragma: no cover - surfaces in the assertion below
            errors.append(exc)

    starter = threading.Thread(target=run_start)
    starter.start()
    try:
        assert client.video.entered.wait(5.0), "start never reached the RPC"

        snapshots: list[ControlSnapshot] = []
        reader = threading.Thread(target=lambda: snapshots.append(control.snapshot()))
        reader.start()
        reader.join(1.0)
        assert not reader.is_alive(), "snapshot() blocked behind the transition"
        assert snapshots == [ControlSnapshot(desired_on=False, video_id=None, in_transition=True)]
    finally:
        client.video.release.set()
        starter.join(5.0)

    assert not errors
    assert control.snapshot().in_transition is False
    assert control.snapshot().desired_on is True


# --- entity updates as part of the transition --------------------------------


class RecordingPublisher:
    """Stands in for Runtime.publish_now: records the control state at call time."""

    def __init__(self, control_ref: list, fail: bool = False) -> None:
        self._control_ref = control_ref
        self.fail = fail
        self.snapshots: list[tuple[bool, str | None]] = []

    def __call__(self) -> None:
        snap = self._control_ref[0].snapshot()
        self.snapshots.append((snap.desired_on, snap.video_id))
        if self.fail:
            raise RuntimeError("publish failed")


def _setup_with_publisher(tmp_path, *, fail_publish=False, **kwargs):
    ref: list = []
    publisher = RecordingPublisher(ref, fail=fail_publish)
    client, video, control, target = _setup(tmp_path, **kwargs)
    control.on_change = publisher
    ref.append(control)
    return client, control, publisher


def test_start_publishes_new_ingress_id_to_entity_after_stream_is_up(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path)
    control.start()
    # Exactly one publish, made after the ingress exists and the stream is
    # desired on, so the asset's Media items carry the new id.
    assert publisher.snapshots == [(True, control.snapshot().video_id)]
    assert control.snapshot().video_id is not None


def test_stop_publishes_cleared_media_after_archiving(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path)
    control.start()
    control.stop()
    # The stop-time publish happens after delete: no video id, not desired on.
    assert publisher.snapshots[-1] == (False, None)
    assert client.calls[-1][0] == "delete"


def test_publish_failure_fails_the_transition_but_keeps_stream_state(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path, fail_publish=True)
    with pytest.raises(CameraControlError, match="entity could not be updated"):
        control.start()
    # The stream really is up; a repeated Start reuses the ingress and retries.
    assert control.snapshot().desired_on is True
    ingress_id = control.snapshot().video_id
    publisher.fail = False
    control.start()
    assert control.snapshot().video_id == ingress_id
    assert [c[0] for c in client.calls] == ["create"]


def test_shutdown_does_not_publish(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path)
    control.start()
    assert len(publisher.snapshots) == 1
    control.shutdown()
    assert len(publisher.snapshots) == 1  # main.py publishes the offline entity itself
    assert control.snapshot() == ControlSnapshot(
        desired_on=False, video_id=None, in_transition=False, closed=True
    )


# --- leftover ingress after a failed Stop, and the closed state -------------


def test_start_after_failed_archive_registers_a_fresh_ingress(tmp_path):
    """A Stop whose archive failed keeps the id for retry, but the next Start must
    not push at that possibly server-archived ingress: archive it, then create new."""
    client, video, control, target = _setup(tmp_path, fail_delete=True)
    control.start()
    first = control.snapshot().video_id
    with pytest.raises(CameraControlError, match="could not be archived"):
        control.stop()
    assert control.snapshot() == ControlSnapshot(
        desired_on=False, video_id=first, in_transition=False
    )

    client.video.fail_delete = False
    control.start()
    second = control.snapshot().video_id
    assert second != first
    assert [c[0] for c in client.calls] == ["create", "delete", "delete", "create"]
    assert client.calls[2][1] == first  # the leftover was archived before the new create


def test_start_after_failed_archive_fails_if_the_leftover_cannot_be_archived(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_delete=True)
    control.start()
    first = control.snapshot().video_id
    with pytest.raises(CameraControlError):
        control.stop()
    with pytest.raises(CameraControlError, match="left by a failed Stop"):
        control.start()
    # Still off, still holding the leftover for the next retry, nothing new created.
    snap = control.snapshot()
    assert snap.desired_on is False and snap.video_id == first
    assert [c[0] for c in client.calls] == ["create", "delete", "delete"]


def test_repeated_start_while_on_reuses_the_live_ingress(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    first = control.snapshot().video_id
    control.start()  # e.g. retry after a failed publish
    assert control.snapshot().video_id == first
    assert [c[0] for c in client.calls] == ["create"]


def test_transitions_are_refused_after_shutdown(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    control.shutdown()
    assert control.snapshot().closed is True
    with pytest.raises(CameraControlError, match="shutting down"):
        control.start()
    with pytest.raises(CameraControlError, match="shutting down"):
        control.stop()
    # Nothing was re-created or re-archived after the shutdown cleanup.
    assert [c[0] for c in client.calls] == ["create", "delete"]
    assert control.snapshot().desired_on is False


def test_start_queued_behind_shutdown_is_refused(tmp_path):
    """A Start task waiting on the transition lock while shutdown() runs must fail
    once it gets the lock, not undo the cleanup."""
    client, video, control, target = _setup(tmp_path)
    control.start()
    # Make shutdown's archive block so a start() can queue behind it.
    client.video.entered = threading.Event()
    client.video.release = threading.Event()
    original_delete = client.video.delete_srt_ingress

    def blocking_delete(ingress_id, *, timeout_ms=None):
        client.video.entered.set()
        assert client.video.release.wait(5.0)
        return original_delete(ingress_id, timeout_ms=timeout_ms)

    client.video.delete_srt_ingress = blocking_delete

    shutdown_thread = threading.Thread(target=control.shutdown)
    shutdown_thread.start()
    assert client.video.entered.wait(5.0)

    errors: list[Exception] = []

    def queued_start():
        try:
            control.start()
        except CameraControlError as exc:
            errors.append(exc)

    start_thread = threading.Thread(target=queued_start)
    start_thread.start()
    client.video.release.set()
    shutdown_thread.join(5.0)
    start_thread.join(5.0)

    assert len(errors) == 1 and "shutting down" in str(errors[0])
    assert [c[0] for c in client.calls] == ["create", "delete"]
    assert control.snapshot().video_id is None


# --- ingress record persistence failure -----------------------------------


class ExplodingState:
    """A StateStore whose set() raises, as a full or read-only card would before
    StateStore learned to swallow OSError; the ingress must still not leak."""

    def get(self, key):
        return None

    def set(self, key, value):
        raise RuntimeError("disk full")

    def delete(self, key):
        pass


def test_create_archives_the_ingress_when_the_record_cannot_be_written(tmp_path):
    client = FakeClient()
    video = VideoIngress(
        client, title="cam", srt_target_file=str(tmp_path / "t.env"), state=ExplodingState()
    )
    with pytest.raises(RuntimeError, match="disk full"):
        video.create()
    assert video.video_id is None
    assert [c[0] for c in client.calls] == ["create", "delete"]
    assert not (tmp_path / "t.env").exists()
