"""Entity contributors: each one fills a single component of the entity.

The entity published to Lattice is composed from independent parts (location,
sensors, media, task catalog, and health from the ``health`` package). Rather
than threading every part through one ever-growing function signature, each
part is an ``EntityContributor`` whose ``apply`` sets its component on the
``Entity`` under construction. ``EntityBuilder`` runs them in order after laying down
the identity floor from ``base.py``.

Contributors depend only on plain callables and small dataclasses, never on
the camera control, pipeline or client types, so the composition stays
decoupled from how the observations are produced. ``main.py`` adapts the real
sources (``lambda: CameraObservation(...)``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from anduril.entitymanager.v1.entity_pub_pb import Entity
from anduril.entitymanager.v1.location_pub_pb import Location, Position
from anduril.entitymanager.v1.media_pub_pb import Media, MediaItem, MediaType
from anduril.entitymanager.v1.sensors_pub_pb import (
    OperationalState,
    Sensor,
    Sensors,
    SensorType,
)
from anduril.tasks.v2.catalog_pub_pb import TaskCatalog, TaskDefinition
from protobuf.wkt import wrappers_pb

from ..camera.source import CameraSource


@dataclass(frozen=True)
class BuildContext:
    """Per-build inputs shared by every contributor.

    ``offline`` marks the final publish at shutdown: the entity is still
    published (so operators see the asset go dark deliberately rather than
    expire), but every component reports the camera as off/offline.
    """

    now: datetime
    offline: bool = False


class EntityContributor(Protocol):
    """One component of the entity. ``apply`` mutates ``entity`` in place."""

    def apply(self, entity: Entity, ctx: BuildContext) -> None: ...


@dataclass(frozen=True)
class CameraObservation:
    """What is currently known about the camera stream.

    ``desired_on`` is the operator's intent (Start/Stop tasks); ``ready`` is
    whether the media pipeline is observed to be producing frames, or ``None``
    when no probe is available; ``degraded`` says the platform is compromised
    (thermally throttled or under-powered, per the health snapshot) so the
    picture may suffer even though frames are flowing.
    """

    desired_on: bool
    ready: bool | None
    degraded: bool = False


class LocationContributor:
    """Sets ``Location`` from the camera source's current geodetic position."""

    def __init__(self, source: CameraSource) -> None:
        self._source = source

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        state = self._source.next_state()
        entity.location = Location(
            position=Position(
                latitude_degrees=state.latitude_degrees,
                longitude_degrees=state.longitude_degrees,
                altitude_hae_meters=wrappers_pb.DoubleValue(value=state.altitude_hae_meters),
            ),
        )


class SensorsContributor:
    """Sets the single EO camera ``Sensor`` with its operational state.

    State rule: offline or not desired on -> OFF; desired on but observed not
    ready -> NON_OPERATIONAL; desired on and ready (or readiness unknown) but
    the platform degraded -> DEGRADED; otherwise OPERATIONAL. Unknown
    readiness is optimistic because the probe is optional and its absence
    should not mark a working camera down.
    """

    def __init__(
        self,
        observe: Callable[[], CameraObservation],
        *,
        sensor_id: str = "rpi-cam-0",
        description: str = "Raspberry Pi Camera Module (EO)",
    ) -> None:
        self._observe = observe
        self._sensor_id = sensor_id
        self._description = description

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        obs = self._observe()
        entity.sensors = Sensors(
            sensors=[
                Sensor(
                    sensor_id=self._sensor_id,
                    sensor_type=SensorType.CAMERA,
                    operational_state=self._state(obs, ctx),
                    sensor_description=self._description,
                )
            ]
        )

    @staticmethod
    def _state(obs: CameraObservation, ctx: BuildContext) -> OperationalState:
        if ctx.offline or not obs.desired_on:
            return OperationalState.OFF
        if obs.ready is False:
            return OperationalState.NON_OPERATIONAL
        if obs.degraded:
            return OperationalState.DEGRADED
        return OperationalState.OPERATIONAL


class MediaContributor:
    """Advertises the live video id on ``Media``.

    ``video_id`` returns the id Lattice's VideoManager assigned to the current
    SRT ingress, or ``None`` when there is no stream. ``Media`` is then
    published explicitly empty (not absent) so a previously advertised item is
    cleared on the Lattice side.
    """

    def __init__(self, video_id: Callable[[], str | None]) -> None:
        self._video_id = video_id

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        video_id = self._video_id()
        items = [MediaItem(item_identifier=video_id, type=MediaType.VIDEO)] if video_id else []
        entity.media = Media(media=items)


class TaskCatalogContributor:
    """Advertises the task specification URLs the camera accepts right now.

    An operator can only assign a task the catalog lists, so the catalog is
    how the agent enables and disables its tasks: ``urls`` is re-read on every
    build and returns only the tasks that apply to the current state (after a
    Start only Stop, after a Stop only Start; ``TaskHandler.offered_task_urls``).
    With no URLs the component is left unset (tasking disabled).
    """

    def __init__(self, urls: Callable[[], Sequence[str]]) -> None:
        self._urls = urls

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        urls = list(self._urls())
        if not urls:
            return
        entity.task_catalog = TaskCatalog(
            task_definitions=[TaskDefinition(task_specification_url=url) for url in urls]
        )
