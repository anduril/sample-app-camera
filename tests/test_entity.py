"""Entity composition: identity floor, contributors and the builder."""

from datetime import UTC, datetime, timedelta

import pytest
from anduril.entitymanager.v1.classification_pub_pb import ClassificationLevels
from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest
from anduril.entitymanager.v1.health_status_pub_pb import ConnectionStatus, HealthStatus
from anduril.entitymanager.v1.media_pub_pb import MediaType
from anduril.entitymanager.v1.sensors_pub_pb import OperationalState, SensorType
from anduril.entitymanager.v1.types_pub_pb import Template
from anduril.ontology.v1.type_pub_pb import Disposition, Environment

from lattice_cam.camera.source import CameraSource
from lattice_cam.config import Config
from lattice_cam.entity import (
    ENTITY_EXPIRY_SECONDS,
    BuildContext,
    CameraObservation,
    EntityBuilder,
    LocationContributor,
    MediaContributor,
    SensorsContributor,
    TaskCatalogContributor,
    base_entity,
    sidc_for,
)
from lattice_cam.health import HealthContributor

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
CREATED = NOW - timedelta(hours=1)
URLS = [
    "type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Start",
    "type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Stop",
]


def _config() -> Config:
    return Config(
        lattice_endpoint="example.test",
        environment_token="tok",
        entity_id="rpi-cam-01",
        entity_name="Front Gate Camera",
        platform_type="Camera",
        camera_latitude=37.1234,
        camera_longitude=-122.5678,
        camera_altitude_hae_meters=30.0,
    )


def _source(cfg: Config) -> CameraSource:
    return CameraSource(cfg.camera_latitude, cfg.camera_longitude, cfg.camera_altitude_hae_meters)


def _builder(
    cfg: Config,
    *,
    desired_on: bool = True,
    ready: bool | None = None,
    degraded: bool = False,
    video_id: str | None = None,
    urls: list[str] | None = None,
) -> EntityBuilder:
    return EntityBuilder(
        cfg,
        entity_id=cfg.entity_id,
        created_time=CREATED,
        contributors=[
            LocationContributor(_source(cfg)),
            SensorsContributor(
                lambda: CameraObservation(desired_on=desired_on, ready=ready, degraded=degraded)
            ),
            MediaContributor(lambda: video_id),
            TaskCatalogContributor(lambda: urls or []),
            # The real contributor with no snapshot: health present, NOT_READY.
            # Its full mapping is covered in test_health.py.
            HealthContributor(lambda: None),
        ],
    )


# -- base entity -----------------------------------------------------------


def test_base_entity_identity():
    cfg = _config()
    e = base_entity(cfg, entity_id="rpi-cam-01", created_time=CREATED, now=NOW)
    assert e.entity_id == "rpi-cam-01"
    assert e.description == "Raspberry Pi Camera Module"
    assert e.is_live is True
    assert e.created_time.to_datetime() == CREATED
    assert e.expiry_time.to_datetime() == NOW + timedelta(seconds=ENTITY_EXPIRY_SECONDS)
    assert e.aliases.name == "Front Gate Camera"
    assert e.ontology.template == Template.ASSET
    assert e.ontology.platform_type == "Camera"
    assert e.mil_view.disposition == Disposition.FRIENDLY
    assert e.mil_view.environment == Environment.LAND
    assert e.provenance.integration_name == "lattice-cam"
    assert e.provenance.data_type == "camera"
    assert e.provenance.source_update_time.to_datetime() == NOW
    # No observed components on the floor.
    assert e.location is None
    assert e.sensors is None
    assert e.media is None
    assert e.health is None
    assert e.task_catalog is None


def test_base_entity_classification_and_symbology():
    e = base_entity(_config(), entity_id="x", created_time=CREATED, now=NOW)
    assert e.data_classification.default.level == ClassificationLevels.UNCLASSIFIED
    assert e.symbology.standard.field == "mil_std_2525_c"
    assert e.symbology.standard.value.sidc == "SFGP-----------"


@pytest.mark.parametrize(
    ("disposition", "environment", "expected"),
    [
        (Disposition.FRIENDLY, Environment.LAND, "SFGP-----------"),
        (Disposition.HOSTILE, Environment.AIR, "SHAP-----------"),
        (Disposition.NEUTRAL, Environment.SURFACE, "SNSP-----------"),
        (Disposition.UNKNOWN, Environment.UNKNOWN, "SUZP-----------"),
    ],
)
def test_sidc_derivation(disposition, environment, expected):
    sidc = sidc_for(disposition, environment)
    assert sidc == expected
    assert len(sidc) == 15


# -- builder ---------------------------------------------------------------


def test_builder_produces_full_entity():
    cfg = _config()
    req = _builder(cfg, video_id="ingress-abc-123", urls=URLS).build(now=NOW)
    assert isinstance(req, PublishEntityRequest)
    e = req.entity
    assert e.entity_id == "rpi-cam-01"
    assert e.aliases.name == "Front Gate Camera"
    assert e.location.position.latitude_degrees == 37.1234
    assert e.location.position.longitude_degrees == -122.5678
    assert e.location.position.altitude_hae_meters.value == 30.0
    assert e.sensors.sensors[0].sensor_id == "rpi-cam-0"
    assert e.sensors.sensors[0].sensor_type == SensorType.CAMERA
    assert e.sensors.sensors[0].operational_state == OperationalState.OPERATIONAL
    assert e.sensors.sensors[0].sensor_description == "Raspberry Pi Camera Module (EO)"
    assert e.media.media[0].item_identifier == "ingress-abc-123"
    assert e.media.media[0].type == MediaType.VIDEO
    assert [d.task_specification_url for d in e.task_catalog.task_definitions] == URLS
    assert e.health.connection_status == ConnectionStatus.ONLINE
    assert e.health.health_status == HealthStatus.NOT_READY
    assert e.health.update_time.to_datetime() == NOW
    assert len(req.to_binary()) > 0


def test_builder_defaults_now_to_utc_now():
    before = datetime.now(UTC)
    req = _builder(_config()).build()
    expiry = req.entity.expiry_time.to_datetime()
    assert before + timedelta(seconds=ENTITY_EXPIRY_SECONDS) <= expiry
    assert expiry <= datetime.now(UTC) + timedelta(seconds=ENTITY_EXPIRY_SECONDS)


def test_builder_exposes_identity():
    b = _builder(_config())
    assert b.entity_id == "rpi-cam-01"
    assert b.created_time == CREATED


def test_contributors_applied_in_order():
    order: list[str] = []

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def apply(self, entity, ctx) -> None:
            order.append(self.name)
            # Later contributors see (and may override) earlier work.
            entity.description = self.name

    b = EntityBuilder(
        _config(),
        entity_id="x",
        created_time=CREATED,
        contributors=[Recorder("first"), Recorder("second"), Recorder("third")],
    )
    req = b.build(now=NOW)
    assert order == ["first", "second", "third"]
    assert req.entity.description == "third"


def test_contributors_receive_context():
    seen: list[BuildContext] = []

    class Spy:
        def apply(self, entity, ctx) -> None:
            seen.append(ctx)

    b = EntityBuilder(_config(), entity_id="x", created_time=CREATED, contributors=[Spy()])
    b.build(now=NOW)
    b.build(now=NOW, offline=True)
    assert seen == [BuildContext(now=NOW), BuildContext(now=NOW, offline=True)]


# -- sensors ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("desired_on", "ready", "offline", "expected"),
    [
        (True, True, False, OperationalState.OPERATIONAL),
        (True, None, False, OperationalState.OPERATIONAL),
        (True, False, False, OperationalState.NON_OPERATIONAL),
        (False, True, False, OperationalState.OFF),
        (False, None, False, OperationalState.OFF),
        (False, False, False, OperationalState.OFF),
        (True, True, True, OperationalState.OFF),
        (True, None, True, OperationalState.OFF),
        (True, False, True, OperationalState.OFF),
        (False, True, True, OperationalState.OFF),
    ],
)
def test_sensor_operational_state(desired_on, ready, offline, expected):
    req = _builder(_config(), desired_on=desired_on, ready=ready).build(now=NOW, offline=offline)
    assert req.entity.sensors.sensors[0].operational_state == expected


@pytest.mark.parametrize(
    ("desired_on", "ready", "offline", "expected"),
    [
        # Frames flowing (or readiness unknown) on a throttled platform: DEGRADED.
        (True, True, False, OperationalState.DEGRADED),
        (True, None, False, OperationalState.DEGRADED),
        # Not ready beats degraded: nothing is flowing at all.
        (True, False, False, OperationalState.NON_OPERATIONAL),
        # Off is off, whatever the platform is doing.
        (False, True, False, OperationalState.OFF),
        (True, True, True, OperationalState.OFF),
    ],
)
def test_sensor_degraded_rule(desired_on, ready, offline, expected):
    req = _builder(_config(), desired_on=desired_on, ready=ready, degraded=True).build(
        now=NOW, offline=offline
    )
    assert req.entity.sensors.sensors[0].operational_state == expected


def test_observation_defaults_to_not_degraded():
    assert CameraObservation(desired_on=True, ready=True).degraded is False


def test_sensor_id_and_description_are_configurable():
    e = base_entity(_config(), entity_id="x", created_time=CREATED, now=NOW)
    SensorsContributor(
        lambda: CameraObservation(desired_on=True, ready=True),
        sensor_id="thermal-1",
        description="IR",
    ).apply(e, BuildContext(now=NOW))
    assert e.sensors.sensors[0].sensor_id == "thermal-1"
    assert e.sensors.sensors[0].sensor_description == "IR"


# -- media -----------------------------------------------------------------


def test_video_id_advertised_as_media_item():
    req = _builder(_config(), video_id="ingress-abc-123").build(now=NOW)
    item = req.entity.media.media[0]
    assert item.item_identifier == "ingress-abc-123"
    assert item.type == MediaType.VIDEO


@pytest.mark.parametrize("video_id", [None, ""])
def test_no_video_id_means_empty_media_list(video_id):
    req = _builder(_config(), video_id=video_id).build(now=NOW)
    # Explicitly empty (not absent) so a previously advertised item is cleared.
    assert req.entity.media is not None
    assert list(req.entity.media.media) == []
    assert len(req.to_binary()) > 0


def test_media_reflects_current_value_each_build():
    current: dict[str, str | None] = {"id": "vid-1"}
    b = EntityBuilder(
        _config(),
        entity_id="x",
        created_time=CREATED,
        contributors=[MediaContributor(lambda: current["id"])],
    )
    assert b.build(now=NOW).entity.media.media[0].item_identifier == "vid-1"
    current["id"] = None
    assert list(b.build(now=NOW).entity.media.media) == []


# -- task catalog ----------------------------------------------------------


def test_task_catalog_present_when_urls_given():
    req = _builder(_config(), urls=URLS).build(now=NOW)
    assert [d.task_specification_url for d in req.entity.task_catalog.task_definitions] == URLS


def test_task_catalog_absent_when_no_urls():
    req = _builder(_config(), urls=[]).build(now=NOW)
    assert req.entity.task_catalog is None


def test_task_catalog_is_reread_on_every_build():
    # The catalog is how the agent enables/disables tasks, so it must follow
    # the source between builds rather than be fixed at construction.
    offered = [URLS[0]]
    cfg = _config()
    builder = EntityBuilder(
        cfg,
        entity_id=cfg.entity_id,
        created_time=CREATED,
        contributors=[TaskCatalogContributor(lambda: offered)],
    )
    first = builder.build(now=NOW).entity.task_catalog
    offered[:] = [URLS[1]]
    second = builder.build(now=NOW).entity.task_catalog
    assert [d.task_specification_url for d in first.task_definitions] == [URLS[0]]
    assert [d.task_specification_url for d in second.task_definitions] == [URLS[1]]


# -- health ----------------------------------------------------------------


def test_health_online_by_default_and_offline_on_shutdown():
    b = _builder(_config())
    online = b.build(now=NOW).entity.health
    assert online.connection_status == ConnectionStatus.ONLINE
    offline = b.build(now=NOW, offline=True).entity.health
    assert offline.connection_status == ConnectionStatus.OFFLINE
    assert offline.update_time.to_datetime() == NOW


# -- location --------------------------------------------------------------


def test_location_reads_source_each_build():
    calls = []

    class CountingSource(CameraSource):
        def next_state(self):
            calls.append(1)
            return super().next_state()

    b = EntityBuilder(
        _config(),
        entity_id="x",
        created_time=CREATED,
        contributors=[LocationContributor(CountingSource(1.0, 2.0, 3.0))],
    )
    b.build(now=NOW)
    pos = b.build(now=NOW).entity.location.position
    assert len(calls) == 2
    assert (pos.latitude_degrees, pos.longitude_degrees, pos.altitude_hae_meters.value) == (
        1.0,
        2.0,
        3.0,
    )


# --- nationality and alternate id --------------------------------------------


def test_nationality_is_published_from_config():
    from anduril.ontology.v1.type_pub_pb import Nationality

    e = base_entity(_config(), entity_id="x", created_time=CREATED, now=NOW)
    assert e.mil_view.nationality == Nationality.UNITED_STATES_OF_AMERICA

    cfg = _config()
    cfg.nationality = "united kingdom"  # case and separators are forgiven
    e = base_entity(cfg, entity_id="x", created_time=CREATED, now=NOW)
    assert e.mil_view.nationality == Nationality.UNITED_KINGDOM


def test_alternate_id_is_published_when_configured():
    from anduril.entitymanager.v1.types_pub_pb import AltIdType

    cfg = _config()
    cfg.alternate_id = "10000000abcd1234"
    e = base_entity(cfg, entity_id="x", created_time=CREATED, now=NOW)
    assert [(a.type, a.id) for a in e.aliases.alternate_ids] == [
        (AltIdType.SERIAL_NUMBER, "10000000abcd1234")
    ]
    assert e.aliases.name == "Front Gate Camera"

    cfg.alternate_id_type = "registration-id"
    e = base_entity(cfg, entity_id="x", created_time=CREATED, now=NOW)
    assert e.aliases.alternate_ids[0].type == AltIdType.REGISTRATION_ID


def test_alternate_id_is_omitted_when_empty():
    e = base_entity(_config(), entity_id="x", created_time=CREATED, now=NOW)
    assert not e.aliases.alternate_ids


def test_identity_enums_are_validated_at_startup():
    from lattice_cam.config import ConfigError
    from lattice_cam.entity import validate_identity

    cfg = _config()
    validate_identity(cfg)
    for field, value in (("nationality", "Narnia"), ("nationality", "INVALID")):
        bad = _config()
        setattr(bad, field, value)
        with pytest.raises(ConfigError, match="NATIONALITY must be one of"):
            validate_identity(bad)
    bad = _config()
    bad.alternate_id_type = "PLATE"
    with pytest.raises(ConfigError, match="ALTERNATE_ID_TYPE must be one of"):
        validate_identity(bad)


def test_pi_serial_number_reads_cpuinfo(tmp_path):
    from lattice_cam.camera.source import pi_serial_number

    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nModel\t\t: Raspberry Pi 5\nSerial\t\t: 10000000deadbeef\n")
    assert pi_serial_number(str(cpuinfo)) == "10000000deadbeef"
    cpuinfo.write_text("processor\t: 0\n")
    assert pi_serial_number(str(cpuinfo)) is None
    assert pi_serial_number(str(tmp_path / "missing")) is None
