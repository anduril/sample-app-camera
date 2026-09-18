"""The entity id: a UUID that stays the same across restarts and reinstalls.

An asset must keep one id for its whole life or operators see a new camera
at every restart while the old one expires. Three sources, in order of precedence:

1. ``ENTITY_ID`` from config, when set. It must be a UUID (checked by
   ``Config.validate``) and it wins over everything, so an operator can pin
   the id or move it between boards.
2. The id recorded in ``state.json`` by an earlier run. Whatever was
   published before is what keeps being published.
3. A UUID derived from the board: ``uuid5`` over the Pi's hardware serial in
   this integration's namespace. It needs no state and is the same after a
   reinstall or a lost SD card, as long as the same board is used.

Without a serial (development off a Pi) a random ``uuid4`` is generated once
and persisted, which is stable for as long as ``state.json`` survives.
"""

from __future__ import annotations

import uuid

from ..config import Config
from ..logging_setup import get_logger
from ..state import StateStore

logger = get_logger(__name__)

STATE_KEY = "entity_id"
# Namespace for the derived id.
NAMESPACE = uuid.UUID("6f0f1c0e-2a5c-4d3a-9d2b-0c7e5a1b8f42")


def canonical_uuid(value: str) -> str | None:
    """The canonical lowercase form of ``value`` if it is a UUID, else ``None``."""
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def derived_entity_id(hardware_serial: str) -> str:
    """The deterministic id for a board, from its hardware serial."""
    return str(uuid.uuid5(NAMESPACE, f"lattice-cam:{hardware_serial.strip()}"))


def resolve_entity_id(config: Config, state: StateStore, hardware_serial: str | None) -> str:
    """Pick the entity id per the precedence above and record it in ``state``."""
    configured = canonical_uuid(config.entity_id) if config.entity_id else None
    recorded = state.get(STATE_KEY)
    recorded = canonical_uuid(recorded) if isinstance(recorded, str) else None

    if configured:
        source = "config"
        entity_id = configured
        if recorded and recorded != configured:
            logger.warning(
                "ENTITY_ID differs from the id published by the previous run; "
                "the previous entity will expire",
                configured=configured,
                previous=recorded,
            )
    elif recorded:
        source, entity_id = "state", recorded
    elif hardware_serial:
        source, entity_id = "hardware serial", derived_entity_id(hardware_serial)
    else:
        source, entity_id = "generated", str(uuid.uuid4())
        logger.warning(
            "no ENTITY_ID, no recorded id and no hardware serial; generated a random id "
            "that is stable only while the state file survives",
            state_file=state.path,
        )

    if entity_id != recorded:
        state.set(STATE_KEY, entity_id)
    logger.info("entity id resolved", entity_id=entity_id, source=source)
    return entity_id
