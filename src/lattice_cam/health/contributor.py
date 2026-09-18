"""``HealthContributor``: the cached ``HealthSnapshot`` as the entity's ``Health``.

This is the only file that knows both vocabularies: the plain-Python report
types on one side and the SDK's ``anduril.entitymanager.v1`` health messages
on the other. The two enum mappings at the top are the whole translation; a
new status or alert level is added there and nowhere else.

The contributor never samples anything. It reads whatever the sampler cached
(possibly ``None`` before the first round, or always ``None`` when health is
disabled) and publishes that. ``NOT_READY`` with no components is therefore
an honest "health is not being sampled", which is what an operator should see
rather than a hard-coded HEALTHY.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from anduril.entitymanager.v1.entity_pub_pb import Entity
from anduril.entitymanager.v1.health_status_pub_pb import (
    Alert,
    AlertCondition,
    AlertLevel,
    ComponentHealth,
    ComponentMessage,
    ConnectionStatus,
    Health,
    HealthStatus,
)
from protobuf.wkt import timestamp_pb

from ..entity.contributors import BuildContext
from .report import AlertLevel as ReportLevel
from .report import AlertReport, ComponentReport, HealthSnapshot, Status

_STATUS: dict[Status, HealthStatus] = {
    Status.HEALTHY: HealthStatus.HEALTHY,
    Status.WARN: HealthStatus.WARN,
    Status.FAIL: HealthStatus.FAIL,
    Status.OFFLINE: HealthStatus.OFFLINE,
    Status.NOT_READY: HealthStatus.NOT_READY,
}

_LEVEL: dict[ReportLevel, AlertLevel] = {
    ReportLevel.ADVISORY: AlertLevel.ADVISORY,
    ReportLevel.CAUTION: AlertLevel.CAUTION,
    ReportLevel.WARNING: AlertLevel.WARNING,
}


def _timestamp(dt: datetime) -> timestamp_pb.Timestamp:
    return timestamp_pb.Timestamp.from_datetime(dt)


class HealthContributor:
    """Sets ``entity.health`` from the latest snapshot; OFFLINE on the shutdown publish."""

    def __init__(self, snapshot: Callable[[], HealthSnapshot | None]) -> None:
        self._snapshot = snapshot

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        snapshot = self._snapshot()
        entity.health = Health(
            connection_status=(
                ConnectionStatus.OFFLINE if ctx.offline else ConnectionStatus.ONLINE
            ),
            health_status=_STATUS[snapshot.overall] if snapshot else HealthStatus.NOT_READY,
            components=[_component(c) for c in snapshot.components] if snapshot else [],
            update_time=_timestamp(ctx.now),
            active_alerts=[_alert(a) for a in snapshot.alerts] if snapshot else [],
        )


def _component(report: ComponentReport) -> ComponentHealth:
    status = _STATUS[report.status]
    return ComponentHealth(
        id=report.id,
        name=report.name,
        health=status,
        messages=[ComponentMessage(status=status, message=m) for m in report.messages],
        update_time=_timestamp(report.sampled_at),
    )


def _alert(report: AlertReport) -> Alert:
    return Alert(
        alert_code=report.code,
        description=report.description,
        level=_LEVEL[report.level],
        activated_time=_timestamp(report.activated_at),
        active_conditions=[
            AlertCondition(condition_code=code, description=text)
            for code, text in report.conditions
        ],
    )
