"""TaskManager API, agent side only: listen for tasks routed to the camera
entity and report their status. Operators create tasks from the Lattice UI;
the ``stub`` attribute exposes the generated client for operator-side tooling
that lives outside this package."""

from __future__ import annotations

from collections.abc import Iterator

from anduril.taskmanager.v1.task_manager_api_pub_connect import (
    TaskManagerAPIClientSync,
)
from anduril.taskmanager.v1.task_manager_api_pub_pb import (
    EntityIds,
    GetTaskRequest,
    ListenAsAgentRequest,
    ListenAsAgentResponse,
    UpdateStatusRequest,
)
from anduril.taskmanager.v1.task_pub_pb import (
    ErrorCode,
    Principal,
    Status,
    StatusUpdate,
    System,
    Task,
    TaskError,
    TaskStatus,
    TaskVersion,
)
from connectrpc.protocol import ProtocolType
from protobuf import Oneof

from .auth import AuthProvider


class TaskClient:
    def __init__(self, base_url: str, http_client, auth: AuthProvider) -> None:
        self._auth = auth
        self.stub = TaskManagerAPIClientSync(
            base_url, http_client=http_client, protocol=ProtocolType.GRPC
        )

    def listen_as_agent(
        self, entity_id: str, *, heartbeat_interval_ms: int = 30000
    ) -> Iterator[ListenAsAgentResponse]:
        """Open the long-lived agent stream for tasks routed to ``entity_id``.

        No client timeout is applied: the stream is meant to stay open. The
        server sends a heartbeat every ``heartbeat_interval_ms`` (0 disables
        heartbeats) so a silent connection can be told apart from a quiet one.
        """
        request = ListenAsAgentRequest(
            agent_selector=Oneof("entity_ids", EntityIds(entity_ids=[entity_id])),
            heartbeat_interval_ms=heartbeat_interval_ms,
        )
        return self.stub.listen_as_agent(request, headers=self._auth.headers(), timeout_ms=None)

    def update_task_status(
        self,
        *,
        task_id: str,
        definition_version: int,
        status_version: int,
        status: Status,
        agent_entity_id: str,
        error_message: str | None = None,
        error_code: ErrorCode = ErrorCode.FAILED,
        timeout_ms: int | None = 10000,
    ) -> TaskVersion:
        """Report a task status change on behalf of the agent entity.

        ``status_version`` must be strictly greater than the last version
        Lattice holds for the task, otherwise the update is ignored.
        """
        task_status = TaskStatus(status=status)
        if error_message:
            task_status.task_error = TaskError(code=error_code, message=error_message)
        update = StatusUpdate(
            version=TaskVersion(
                task_id=task_id,
                definition_version=definition_version,
                status_version=status_version,
            ),
            status=task_status,
            author=Principal(agent=Oneof("system", System(entity_id=agent_entity_id))),
        )
        response = self.stub.update_status(
            UpdateStatusRequest(status_update=update),
            headers=self._auth.headers(),
            timeout_ms=timeout_ms,
        )
        return response.task.version

    def get_task(self, task_id: str, *, timeout_ms: int | None = 10000) -> Task:
        """Read a task back.

        The agent uses this to resynchronise its status version when the
        server changes the task behind its back (a cancel or complete request
        carries only the task id, not the new version).
        """
        response = self.stub.get_task(
            GetTaskRequest(task_id=task_id),
            headers=self._auth.headers(),
            timeout_ms=timeout_ms,
        )
        return response.task
