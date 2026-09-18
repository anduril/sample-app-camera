"""``LatticeClient``: one object per process holding the TLS transport, the
auth provider, and a thin client per Lattice API.

The facade does not wrap the per-API clients; it exposes them as attributes so
each consumer talks to exactly the surface it needs:

* ``client.entities`` — publish the camera entity and read it back.
* ``client.video`` — register and archive the SRT ingress.
* ``client.tasks`` — the agent-side task stream and status updates. Its
  ``stub`` is the generated client, for operator tooling in ``scripts/``.
* ``client.auth`` — per-call auth metadata (``headers()``), also for scripts.

The task surface inside the package is deliberately limited to what an agent
needs (listen and report status); task creation lives only in
``scripts/send_task.py``.
"""

from __future__ import annotations

from pyqwest import SyncClient, SyncHTTPTransport

from ..config import Config
from .auth import AuthProvider
from .entities import EntityClient
from .tasks import TaskClient
from .transport import base_url, build_transport
from .video import VideoClient


class LatticeClient:
    """Shares one transport and auth provider across the per-API clients."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._transport: SyncHTTPTransport | None = build_transport(config)
        self._http = SyncClient(self._transport)
        self.auth = AuthProvider(config, self._http)
        url = base_url(config)
        self.entities = EntityClient(url, self._http, self.auth)
        self.video = VideoClient(url, self._http, self.auth)
        self.tasks = TaskClient(url, self._http, self.auth)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying pyqwest transport (idempotent).

        ``pyqwest.SyncClient`` has no ``close``; the connection pool belongs
        to the ``SyncHTTPTransport``, which does.
        """
        transport = self._transport
        if transport is None:
            return
        self._transport = None
        transport.close()

    def __enter__(self) -> LatticeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
