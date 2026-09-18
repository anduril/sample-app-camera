"""Unit tests for the Lattice client layer with fake generated stubs (no network).

Each per-API client is constructed normally (building a Connect stub does not
open a connection) and its ``stub`` is then swapped for a fake that records
the request and returns a canned response, so the tests pin down the request
shapes and response mapping the daemon relies on.
"""

from __future__ import annotations

import pytest
from anduril.entitymanager.v1.entity_manager_api_pub_pb import (
    PublishEntityRequest,
    PublishEntityResponse,
)
from anduril.entitymanager.v1.entity_pub_pb import Entity
from anduril.taskmanager.v1.task_manager_api_pub_pb import (
    UpdateStatusRequest,
    UpdateStatusResponse,
)
from anduril.taskmanager.v1.task_pub_pb import ErrorCode, Status, Task, TaskVersion
from anduril.videomanager.v1.create_ingress_stream_response_pub_pb import (
    CreateIngressStreamResponse,
)
from anduril.videomanager.v1.mpeg_ts_ingress_pub_pb import MpegTsIngress
from anduril.videomanager.v1.srt_ingress_pub_pb import SrtIngress
from protobuf import Oneof
from pyqwest import SyncClient

from lattice_cam.config import Config
from lattice_cam.lattice import LatticeClient, SrtIngressInfo
from lattice_cam.lattice.auth import AuthProvider
from lattice_cam.lattice.entities import EntityClient
from lattice_cam.lattice.tasks import TaskClient
from lattice_cam.lattice.video import VideoClient

BASE_URL = "https://example.test"
HEADERS = {"authorization": "Bearer tok", "anduril-sandbox-authorization": "Bearer sb"}


class FakeAuth:
    def headers(self) -> dict[str, str]:
        return dict(HEADERS)


class FakeStub:
    """Records the last call to any method and returns the scripted response."""

    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[tuple[str, object, dict]] = []

    def __getattr__(self, name):
        def call(request, **kwargs):
            self.calls.append((name, request, kwargs))
            return self.response

        return call


def _video(response) -> tuple[VideoClient, FakeStub]:
    client = VideoClient(BASE_URL, SyncClient(), FakeAuth())
    stub = FakeStub(response)
    client.stub = stub
    return client, stub


# -- video -------------------------------------------------------------------


def test_create_srt_ingress_maps_server_shaped_response():
    # Lattice returns "<client id>#<suffix>", not the id that was sent.
    returned_id = "82a0497e-0000-0000-0000-000000000000#c8b80372-1111-1111-1111-111111111111"
    response = CreateIngressStreamResponse(
        ingress_id=returned_id,
        ingress=Oneof("srt", SrtIngress(url="srt://lattice.example:9000", session_id="s-1")),
    )
    client, stub = _video(response)

    info = client.create_srt_ingress(
        title="RPi Camera", passphrase="secret", ingress_id="82a0497e-0000", timeout_ms=5
    )

    assert info == SrtIngressInfo(
        video_id=returned_id, push_url="srt://lattice.example:9000", session_id="s-1"
    )
    method, request, kwargs = stub.calls[0]
    assert method == "create_ingress_stream"
    assert request.title == "RPi Camera"
    assert request.ingress_id == "82a0497e-0000"
    assert request.ingress.field == "srt"
    assert request.ingress.value.passphrase == "secret"
    assert kwargs == {"headers": HEADERS, "timeout_ms": 5}


def test_create_srt_ingress_rejects_non_srt_ingress():
    response = CreateIngressStreamResponse(
        ingress_id="x", ingress=Oneof("mpeg_ts", MpegTsIngress(url="udp://1.2.3.4:5"))
    )
    client, _ = _video(response)
    with pytest.raises(RuntimeError, match="mpeg_ts"):
        client.create_srt_ingress(title="t")


def test_create_srt_ingress_rejects_missing_ingress():
    client, _ = _video(CreateIngressStreamResponse(ingress_id="x"))
    with pytest.raises(RuntimeError):
        client.create_srt_ingress(title="t")


def test_delete_srt_ingress_sends_id_and_auth():
    client, stub = _video(object())
    client.delete_srt_ingress("a#b", timeout_ms=7)
    method, request, kwargs = stub.calls[0]
    assert method == "delete_ingress_stream"
    assert request.ingress_id == "a#b"
    assert kwargs == {"headers": HEADERS, "timeout_ms": 7}


# -- tasks -------------------------------------------------------------------


def test_update_task_status_builds_request():
    returned = TaskVersion(task_id="t-1", definition_version=2, status_version=6)
    client = TaskClient(BASE_URL, SyncClient(), FakeAuth())
    stub = FakeStub(UpdateStatusResponse(task=Task(version=returned)))
    client.stub = stub

    version = client.update_task_status(
        task_id="t-1",
        definition_version=2,
        status_version=6,
        status=Status.DONE_NOT_OK,
        agent_entity_id="rpi-cam-01",
        error_message="boom",
        error_code=ErrorCode.CANCELLED,
        timeout_ms=9,
    )

    assert version == returned
    method, request, kwargs = stub.calls[0]
    assert method == "update_status"
    assert isinstance(request, UpdateStatusRequest)
    update = request.status_update
    assert update.version == TaskVersion(task_id="t-1", definition_version=2, status_version=6)
    assert update.status.status == Status.DONE_NOT_OK
    assert update.status.task_error.code == ErrorCode.CANCELLED
    assert update.status.task_error.message == "boom"
    assert update.author.agent.field == "system"
    assert update.author.agent.value.entity_id == "rpi-cam-01"
    assert kwargs == {"headers": HEADERS, "timeout_ms": 9}


def test_update_task_status_without_error_has_no_task_error():
    client = TaskClient(BASE_URL, SyncClient(), FakeAuth())
    stub = FakeStub(UpdateStatusResponse(task=Task(version=TaskVersion(task_id="t"))))
    client.stub = stub
    client.update_task_status(
        task_id="t",
        definition_version=1,
        status_version=1,
        status=Status.EXECUTING,
        agent_entity_id="a",
    )
    status = stub.calls[0][1].status_update.status
    assert status.status == Status.EXECUTING
    assert status.task_error is None


def test_listen_as_agent_selects_entity_and_heartbeat():
    client = TaskClient(BASE_URL, SyncClient(), FakeAuth())
    stub = FakeStub(iter(()))
    client.stub = stub
    client.listen_as_agent("rpi-cam-01", heartbeat_interval_ms=1234)
    method, request, kwargs = stub.calls[0]
    assert method == "listen_as_agent"
    assert request.agent_selector.field == "entity_ids"
    assert request.agent_selector.value.entity_ids == ["rpi-cam-01"]
    assert request.heartbeat_interval_ms == 1234
    assert kwargs == {"headers": HEADERS, "timeout_ms": None}


# -- entities ----------------------------------------------------------------


def test_publish_entity_passes_auth_headers():
    client = EntityClient(BASE_URL, SyncClient(), FakeAuth())
    stub = FakeStub(PublishEntityResponse())
    client.stub = stub
    request = PublishEntityRequest(entity=Entity(entity_id="rpi-cam-01"))

    client.publish_entity(request, timeout_ms=3)

    method, sent, kwargs = stub.calls[0]
    assert method == "publish_entity"
    assert sent is request
    assert kwargs == {"headers": HEADERS, "timeout_ms": 3}


def test_get_entity_passes_id_and_auth():
    client = EntityClient(BASE_URL, SyncClient(), FakeAuth())
    stub = FakeStub(object())
    client.stub = stub
    client.get_entity("rpi-cam-01")
    method, request, kwargs = stub.calls[0]
    assert method == "get_entity"
    assert request.entity_id == "rpi-cam-01"
    assert kwargs["headers"] == HEADERS


# -- facade ------------------------------------------------------------------


def test_lattice_client_exposes_per_api_clients_and_closes():
    config = Config(lattice_endpoint="example.test", environment_token="tok")
    with LatticeClient(config) as client:
        assert isinstance(client.entities, EntityClient)
        assert isinstance(client.video, VideoClient)
        assert isinstance(client.tasks, TaskClient)
        assert isinstance(client.auth, AuthProvider)
        assert client.auth.headers() == {"authorization": "Bearer tok"}
        assert hasattr(client.tasks, "stub")
        for legacy in ("publish_entity", "get_entity", "listen_as_agent", "update_task_status"):
            assert not hasattr(client, legacy)
    client.close()  # idempotent after the context manager already closed it


def test_get_task_reads_back_the_task_with_auth():
    from anduril.taskmanager.v1.task_manager_api_pub_pb import GetTaskResponse

    task = Task(version=TaskVersion(task_id="t-9", definition_version=1, status_version=4))
    client = TaskClient(BASE_URL, SyncClient(), FakeAuth())
    stub = FakeStub(GetTaskResponse(task=task))
    client.stub = stub

    result = client.get_task("t-9", timeout_ms=5000)

    assert result.version.status_version == 4
    name, request, kwargs = stub.calls[0]
    assert name == "get_task"
    assert request.task_id == "t-9"
    assert kwargs["headers"] == HEADERS
    assert kwargs["timeout_ms"] == 5000
