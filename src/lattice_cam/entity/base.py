"""The identity floor of the camera entity.

Everything here is fixed for the life of the service or derived from config:
who the entity is (id, name, description), what it is (ontology, mil view,
symbology), who produced it (provenance) and how it should be handled
(classification, liveness, expiry). Anything observed at runtime is added by
contributors on top of this floor.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from anduril.entitymanager.v1.classification_pub_pb import (
    Classification,
    ClassificationInformation,
    ClassificationLevels,
)
from anduril.entitymanager.v1.entity_pub_pb import Aliases, AlternateId, Entity, Provenance
from anduril.entitymanager.v1.ontology_pub_pb import MilView, Ontology
from anduril.entitymanager.v1.symbology_pub_pb import MilStd2525C, Symbology
from anduril.entitymanager.v1.types_pub_pb import AltIdType, Template
from anduril.ontology.v1.type_pub_pb import Disposition, Environment, Nationality
from protobuf import Oneof
from protobuf.wkt import timestamp_pb

from ..config import Config, ConfigError

# Keep the entity live: expiry is short and we re-publish on every tick.
ENTITY_EXPIRY_SECONDS = 10

ENTITY_DESCRIPTION = "Raspberry Pi Camera Module"

# MIL-STD-2525C position 2 (affiliation) and position 3 (battle dimension).
_SIDC_AFFILIATION = {
    Disposition.FRIENDLY: "F",
    Disposition.ASSUMED_FRIENDLY: "A",
    Disposition.HOSTILE: "H",
    Disposition.SUSPICIOUS: "S",
    Disposition.NEUTRAL: "N",
    Disposition.PENDING: "P",
    Disposition.UNKNOWN: "U",
}
_SIDC_DIMENSION = {
    Environment.AIR: "A",
    Environment.LAND: "G",
    Environment.SURFACE: "S",
    Environment.SUB_SURFACE: "U",
    Environment.SPACE: "P",
    Environment.UNKNOWN: "Z",
}


def sidc_for(disposition: Disposition, environment: Environment) -> str:
    """Derive a 15-character MIL-STD-2525C symbol id code.

    Only the coding scheme (S = warfighting), affiliation, battle dimension and
    status (P = present) are set; the function id and modifiers stay ``-`` so
    the symbol renders as a generic present unit of that affiliation and
    dimension, e.g. ``SFGP-----------`` for a friendly land asset.
    """
    affiliation = _SIDC_AFFILIATION.get(disposition, "U")
    dimension = _SIDC_DIMENSION.get(environment, "Z")
    return f"S{affiliation}{dimension}P" + "-" * 11


def _timestamp(dt: datetime) -> timestamp_pb.Timestamp:
    return timestamp_pb.Timestamp.from_datetime(dt)


def _enum_member(enum_cls, key: str, value: str):
    """Resolve a config string to an SDK enum member, or fail at startup.

    Enum names are accepted case-insensitively with spaces or hyphens as
    underscores; ``INVALID`` is never accepted. A typo must stop the daemon
    rather than publish an unknown nationality or alternate-id type.
    """
    name = value.strip().upper().replace(" ", "_").replace("-", "_")
    member = enum_cls.__members__.get(name)
    if member is None or name == "INVALID":
        raise ConfigError(
            f"{key} must be one of the SDK {enum_cls.__name__} names "
            f"(for example {', '.join(list(enum_cls.__members__)[1:4])}), got {value!r}"
        )
    return member


def nationality_for(config: Config) -> Nationality:
    return _enum_member(Nationality, "NATIONALITY", config.nationality)


def alternate_id_type_for(config: Config) -> AltIdType:
    return _enum_member(AltIdType, "ALTERNATE_ID_TYPE", config.alternate_id_type)


def validate_identity(config: Config) -> None:
    """Check the config-driven identity enums once, before the first publish."""
    nationality_for(config)
    alternate_id_type_for(config)


def base_entity(
    config: Config,
    *,
    entity_id: str,
    created_time: datetime,
    now: datetime,
) -> Entity:
    """Build the entity with its identity fields only; no observed components."""
    disposition = Disposition.FRIENDLY
    environment = Environment.LAND
    alternate_ids = None
    if config.alternate_id:
        alternate_ids = [
            AlternateId(id=config.alternate_id, type=alternate_id_type_for(config)),
        ]
    return Entity(
        entity_id=entity_id,
        description=ENTITY_DESCRIPTION,
        is_live=True,
        created_time=_timestamp(created_time),
        expiry_time=_timestamp(now + timedelta(seconds=ENTITY_EXPIRY_SECONDS)),
        aliases=Aliases(name=config.entity_name, alternate_ids=alternate_ids),
        ontology=Ontology(template=Template.ASSET, platform_type=config.platform_type),
        mil_view=MilView(
            disposition=disposition,
            environment=environment,
            nationality=nationality_for(config),
        ),
        provenance=Provenance(
            integration_name=config.integration_name,
            data_type="camera",
            source_update_time=_timestamp(now),
        ),
        data_classification=Classification(
            default=ClassificationInformation(level=ClassificationLevels.UNCLASSIFIED)
        ),
        symbology=Symbology(
            standard=Oneof("mil_std_2525_c", MilStd2525C(sidc=sidc_for(disposition, environment)))
        ),
    )
