"""``EntityBuilder``: the extension seam of this service.

To add a component to the published entity, write a new contributor class
(see ``contributors.py``) and add one line to the ``contributors`` list in
``main.py``. Nothing else changes: the builder lays down the identity floor
from ``base.py`` and then lets each contributor set its component, in order.
The health feature is the worked example of this pattern.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest

from ..config import Config
from .base import base_entity
from .contributors import BuildContext, EntityContributor


class EntityBuilder:
    """Composes a ``PublishEntityRequest`` from the identity floor plus contributors."""

    def __init__(
        self,
        config: Config,
        *,
        entity_id: str,
        created_time: datetime,
        contributors: Sequence[EntityContributor],
    ) -> None:
        self._config = config
        # Identity is fixed for the life of the service so the periodic loop
        # and on-demand publishes describe the same entity.
        self._entity_id = entity_id
        self._created_time = created_time
        self._contributors = list(contributors)

    @property
    def entity_id(self) -> str:
        return self._entity_id

    @property
    def created_time(self) -> datetime:
        return self._created_time

    def build(self, *, now: datetime | None = None, offline: bool = False) -> PublishEntityRequest:
        """Build the request: base entity, then every contributor in order."""
        now = now or datetime.now(UTC)
        ctx = BuildContext(now=now, offline=offline)
        entity = base_entity(
            self._config,
            entity_id=self._entity_id,
            created_time=self._created_time,
            now=now,
        )
        for contributor in self._contributors:
            contributor.apply(entity, ctx)
        return PublishEntityRequest(entity=entity)
