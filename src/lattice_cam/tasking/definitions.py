"""The custom task definitions the camera agent accepts (see ``task-def/``).

Both tasks are empty Protobuf messages, so the agent dispatches purely on the
message *name* carried in the task specification's type URL:
``type.googleapis.com/<TASK_PACKAGE>.Start`` and ``.Stop``.
"""

from __future__ import annotations

TYPE_URL_PREFIX = "type.googleapis.com/"
TASK_START = "Start"
TASK_STOP = "Stop"
SUPPORTED_TASKS = (TASK_START, TASK_STOP)


def task_type_url(package: str, name: str) -> str:
    """Build the type URL Lattice uses to identify a task definition."""
    return f"{TYPE_URL_PREFIX}{package}.{name}"


def task_name_from_type_url(type_url: str) -> str:
    """Return the message name (last dotted component) of a type URL."""
    return type_url.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
