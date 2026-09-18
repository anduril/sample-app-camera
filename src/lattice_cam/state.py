"""Small persistent state for the daemon: a JSON file of named records.

The daemon must survive crashes and power loss without leaking Lattice-side
resources or losing its identity. Two things need to outlive a process:

* the **current video ingress** (id, push URL, session id), so a restart can
  archive an ingress the previous process never got to clean up;
* the entity's **created_time**, so a stable-id asset keeps one creation time
  across restarts instead of resetting it on every boot.

``StateStore`` is deliberately minimal: string keys, JSON-serialisable values,
atomic writes (write to a temp file, then ``os.replace``), and a lock so the
publish loop, the task worker, and the control path can all use it. A missing
or corrupt file is treated as empty and logged, never raised, because losing
the state file must degrade to "as if fresh", not prevent the daemon starting.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from .logging_setup import get_logger

logger = get_logger(__name__)


class StateStore:
    """A JSON-backed key/value file with atomic writes."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._last_flush_error: str | None = None
        self._data: dict[str, Any] = self._load()

    @property
    def path(self) -> str:
        return self._path

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._flush()

    def delete(self, key: str) -> None:
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._flush()

    # -- internals -----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self._path or not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(
                "state file unreadable; starting with empty state",
                state_file=self._path,
                error=str(exc),
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "state file is not a JSON object; starting with empty state",
                state_file=self._path,
            )
            return {}
        return data

    def _flush(self) -> None:
        """Write the file; on failure keep the in-memory value and warn once.

        Persistence is best-effort: an unwritable path (full or read-only
        card) must not crash the daemon at its first boot write, it only
        loses crash recovery until the disk is fixed. The warning repeats only
        when the error changes, because writes happen on every transition.
        """
        if not self._path:
            return
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self._path)
        except OSError as exc:
            message = str(exc)
            if message != self._last_flush_error:
                self._last_flush_error = message
                logger.warning(
                    "state file could not be written; keeping state in memory only",
                    state_file=self._path,
                    error=message,
                )
            return
        self._last_flush_error = None
