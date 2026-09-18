"""Task-dispatch driver for TESTING AND VALIDATION ONLY — run it SEPARATELY
from the daemon.

In normal operation tasks are created by an operator in the Lattice UI; the
systemd service is only an agent (it listens and reports status) and never
creates tasks. This script stands in for the operator: it constructs its own
client, creates a Start or Stop task assigned to the camera entity, then polls
the task until it reaches a terminal state, proving the server-side lifecycle
(SENT -> EXECUTING -> DONE_OK).

Task creation is kept out of the installed package on purpose, so the
operator-side calls live here as plain functions over the public
``client.tasks.stub`` and ``client.auth``.

Usage:
    python scripts/send_task.py --config .env Stop
    python scripts/send_task.py --config .env Start [--entity-id <uuid>] [--wait 20]
"""

from __future__ import annotations

import argparse
import sys
import time

# Make ``src`` importable when run directly from the repo root.
sys.path.insert(0, "src")

from anduril.taskmanager.v1.task_manager_api_pub_pb import (  # noqa: E402
    CreateTaskRequest,
    GetTaskRequest,
)
from anduril.taskmanager.v1.task_pub_pb import (  # noqa: E402
    Principal,
    Relations,
    Status,
    System,
    Task,
)
from protobuf import Oneof  # noqa: E402
from protobuf.wkt import any_pb  # noqa: E402

from lattice_cam import config as config_module  # noqa: E402
from lattice_cam.camera.source import pi_serial_number  # noqa: E402
from lattice_cam.entity.identity import resolve_entity_id  # noqa: E402
from lattice_cam.lattice import LatticeClient  # noqa: E402
from lattice_cam.state import StateStore  # noqa: E402
from lattice_cam.tasking.definitions import (  # noqa: E402
    SUPPORTED_TASKS,
    task_type_url,
)

TERMINAL = {Status.DONE_OK, Status.DONE_NOT_OK}


def create_task(
    client: LatticeClient,
    *,
    display_name: str,
    type_url: str,
    assignee_entity_id: str,
    author_service_name: str,
    specification_bytes: bytes = b"",
    description: str = "",
    timeout_ms: int | None = 30000,
) -> Task:
    """Create a task assigned to the agent entity (operator side).

    The specification is a ``google.protobuf.Any`` carrying ``type_url`` and
    the serialized task message; for the empty Start/Stop messages the
    payload is empty.
    """
    request = CreateTaskRequest(
        display_name=display_name,
        description=description,
        specification=any_pb.Any(type_url=type_url, value=specification_bytes),
        author=Principal(agent=Oneof("system", System(service_name=author_service_name))),
        relations=Relations(
            assignee=Principal(agent=Oneof("system", System(entity_id=assignee_entity_id)))
        ),
        is_executed_elsewhere=False,
    )
    response = client.tasks.stub.create_task(
        request, headers=client.auth.headers(), timeout_ms=timeout_ms
    )
    return response.task


def get_task(client: LatticeClient, task_id: str, *, timeout_ms: int | None = 30000) -> Task:
    """Read a task back."""
    response = client.tasks.stub.get_task(
        GetTaskRequest(task_id=task_id),
        headers=client.auth.headers(),
        timeout_ms=timeout_ms,
    )
    return response.task


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a Start/Stop task to the camera agent.")
    parser.add_argument("task", choices=SUPPORTED_TASKS, help="task message name")
    parser.add_argument("--config", default=".env")
    parser.add_argument("--entity-id", default=None, help="assignee (defaults to config ENTITY_ID)")
    parser.add_argument(
        "--wait", type=float, default=20.0, help="seconds to wait for a terminal state"
    )
    args = parser.parse_args()

    config = config_module.load(args.config)
    config.validate()
    # Same resolution as the daemon: ENTITY_ID, else state.json, else the
    # id derived from this board's serial.
    entity_id = args.entity_id or resolve_entity_id(
        config, StateStore(config.state_file), pi_serial_number()
    )
    type_url = task_type_url(config.task_package, args.task)

    with LatticeClient(config) as client:
        entity = client.entities.get_entity(entity_id, timeout_ms=30000).entity
        catalog = (
            [d.task_specification_url for d in entity.task_catalog.task_definitions]
            if entity.task_catalog
            else []
        )
        print("agent is_live:  ", entity.is_live)
        print("agent catalog:  ", catalog or "<none advertised>")
        if type_url not in catalog:
            print(f"WARNING: agent does not advertise {type_url}", file=sys.stderr)

        task = create_task(
            client,
            display_name=f"{args.task} camera",
            type_url=type_url,
            assignee_entity_id=entity_id,
            author_service_name="rpi-cam-send-task",
            description=f"{args.task} the Raspberry Pi camera stream",
        )
        task_id = task.version.task_id
        print("task_id:        ", task_id)
        print("type_url:       ", type_url)

        deadline = time.monotonic() + args.wait
        last = None
        while True:
            current = get_task(client, task_id)
            status = current.status.status if current.status else None
            version = current.version.status_version if current.version else None
            line = (status.name if status is not None else "?", version)
            if line != last:
                print(f"status: {line[0]:<14} status_version: {line[1]}")
                last = line
            if status in TERMINAL:
                if status == Status.DONE_NOT_OK and current.status.task_error:
                    print("task_error:     ", current.status.task_error.message)
                return 0 if status == Status.DONE_OK else 1
            if time.monotonic() > deadline:
                print("WARNING: task did not reach a terminal state in time", file=sys.stderr)
                return 2
            time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
