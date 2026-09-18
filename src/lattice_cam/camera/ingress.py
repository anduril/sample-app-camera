"""Lattice video ingress lifecycle for the camera.

``VideoIngress`` owns exactly one SRT ingress at a time:

* ``create()`` registers a new ingress with Lattice's VideoManager under a
  fresh client-side id, writes the SRT push URL to the ``EnvironmentFile`` the
  MediaMTX unit loads (``SRT_TARGET=<url>``), and records the ingress in the
  ``StateStore`` so it survives the process.
* ``delete()`` archives that ingress (``DeleteIngressStream`` archives rather
  than deletes: the record stays retrievable but stops accepting frames),
  clears the persisted record, and forgets its id.
* ``recover()`` runs once at boot: if the previous process died with an
  ingress still registered (crash, power loss, ``kill -9``), the persisted
  record names it and we archive it so Lattice is not left with a stream nobody
  pushes to. Best-effort: on failure the record is kept so the next boot tries
  again.

A new id per ``create()`` matters: archived records persist under their id, so
reusing one across a Stop/Start cycle is not safe. The id we *send* is a UUID,
but the id Lattice *returns* is server-shaped (``<client id>#<suffix>``,
observed live), so the returned id is the one we advertise, persist, and
archive by. The current id (or ``None`` when no ingress exists) is what the
entity advertises as its ``Media`` item, so the stale reference disappears from
the entity on the next publish after a ``delete()``.

Locking: the RPCs run **outside** the lock, which only guards ``_info``. That
keeps ``video_id`` / ``info`` non-blocking for the 1 Hz publish loop even while
a 30 s VideoManager call is in flight. ``create`` / ``delete`` / ``recover``
are therefore not safe to overlap with each other; ``CameraControl``
serialises them with its transition lock.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from ..lattice import LatticeClient, SrtIngressInfo
from ..logging_setup import get_logger
from ..state import StateStore

logger = get_logger(__name__)

STATE_KEY = "ingress"


class VideoIngress:
    """The camera's single SRT ingress in Lattice, created and archived on demand."""

    def __init__(
        self,
        client: LatticeClient,
        *,
        title: str,
        passphrase: str = "",
        srt_target_file: str = "",
        state: StateStore | None = None,
    ) -> None:
        self._client = client
        self._title = title
        self._passphrase = passphrase
        self._srt_target_file = srt_target_file
        self._state = state
        self._info: SrtIngressInfo | None = None
        self._lock = threading.Lock()

    @property
    def video_id(self) -> str | None:
        """The live ingress id to advertise on the entity, or ``None``."""
        with self._lock:
            return self._info.video_id if self._info else None

    @property
    def info(self) -> SrtIngressInfo | None:
        with self._lock:
            return self._info

    def create(self) -> SrtIngressInfo:
        """Register a new ingress and persist its push URL for MediaMTX.

        Raises on failure and leaves the previous state untouched. If an
        ingress already exists it is returned as-is (create is idempotent
        within a Start).
        """
        existing = self.info
        if existing is not None:
            return existing

        ingress_id = str(uuid.uuid4())
        info = self._client.video.create_srt_ingress(
            title=self._title,
            passphrase=self._passphrase,
            ingress_id=ingress_id,
        )
        logger.info("registered SRT ingress", video_id=info.video_id, push_url=info.push_url)
        # From here on the ingress exists in Lattice. Persist first, so a crash
        # before the env file is written is still recoverable at the next boot;
        # if either local step fails, nothing else will ever find this ingress,
        # so archive it now rather than leak it.
        try:
            self._persist(info)
            self._write_srt_target(info.push_url)
        except Exception:
            self._archive_best_effort(info.video_id, "ingress could not be recorded locally")
            raise
        with self._lock:
            self._info = info
        return info

    def delete(self) -> None:
        """Archive the current ingress in Lattice and forget it.

        Raises on failure and keeps the id so a retry can archive it later.
        """
        video_id = self.video_id
        if video_id is None:
            return
        self._client.video.delete_srt_ingress(video_id)
        logger.info("archived SRT ingress", video_id=video_id)
        self._clear_record()
        with self._lock:
            self._info = None

    def recover(self) -> None:
        """Archive an ingress left behind by a previous process, if any.

        Reads the persisted record and archives it best-effort. Never raises:
        a failure keeps the record so the next boot retries. Does nothing when
        this instance already owns a live ingress.
        """
        if self._state is None:
            return
        record = self._state.get(STATE_KEY)
        if not isinstance(record, dict) or not record.get("video_id"):
            return
        video_id = str(record["video_id"])
        if self.video_id is not None:
            logger.info("ingress already live; skipping recovery", video_id=video_id)
            return
        logger.warning(
            "archiving orphaned SRT ingress from a previous run",
            video_id=video_id,
            created_at=record.get("created_at"),
        )
        try:
            self._client.video.delete_srt_ingress(video_id)
        except Exception as exc:
            logger.warning(
                "could not archive orphaned ingress; will retry on next start",
                video_id=video_id,
                error=str(exc),
            )
            return
        logger.info("archived orphaned SRT ingress", video_id=video_id)
        self._clear_record()

    # -- internals -----------------------------------------------------------

    def _persist(self, info: SrtIngressInfo) -> None:
        if self._state is None:
            return
        record: dict[str, Any] = {
            "video_id": info.video_id,
            "push_url": info.push_url,
            "session_id": info.session_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._state.set(STATE_KEY, record)

    def _clear_record(self) -> None:
        if self._state is not None:
            self._state.delete(STATE_KEY)

    def _archive_best_effort(self, video_id: str, reason: str) -> None:
        try:
            self._client.video.delete_srt_ingress(video_id)
        except Exception as exc:
            logger.warning(
                "could not archive video ingress",
                reason=reason,
                video_id=video_id,
                error=str(exc),
            )
            return
        self._clear_record()

    def _write_srt_target(self, push_url: str) -> None:
        """Write the SRT push URL as an EnvironmentFile for the MediaMTX unit.

        Written atomically (temp file, then ``os.replace``) so MediaMTX never
        loads a half-written file, and so an existing file we cannot open for
        writing (one left behind root-owned by an earlier run, while the
        daemon now runs unprivileged) is still replaced: renaming into the
        directory needs write access to the directory, not to the old file.
        """
        path = self._srt_target_file
        if not path:
            return
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as handle:
            handle.write(f"SRT_TARGET={push_url}\n")
        os.replace(tmp, path)
        logger.info("wrote SRT target for MediaMTX", srt_target_file=path)
