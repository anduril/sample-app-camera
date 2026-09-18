"""Strict config parsing and validation."""

import pytest

from lattice_cam.config import Config, ConfigError, load

_KEYS = [
    "LATTICE_ENDPOINT",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "ENVIRONMENT_TOKEN",
    "SANDBOXES_TOKEN",
    "LATTICE_CA_CERT_PATH",
    "SKIP_TLS_VERIFY",
    "ENTITY_ID",
    "ENTITY_NAME",
    "PLATFORM_TYPE",
    "INTEGRATION_NAME",
    "CAMERA_LATITUDE",
    "CAMERA_LONGITUDE",
    "CAMERA_ALTITUDE_HAE_METERS",
    "STATE_FILE",
    "VIDEO_ENABLED",
    "VIDEO_TITLE",
    "SRT_PASSPHRASE",
    "SRT_TARGET_FILE",
    "MEDIAMTX_API_URL",
    "MEDIAMTX_PATH",
    "TASKING_ENABLED",
    "TASK_PACKAGE",
    "TASK_START_COMMAND",
    "TASK_STOP_COMMAND",
    "TASK_HEARTBEAT_INTERVAL_MS",
    "HEALTH_ENABLED",
    "HEALTH_SAMPLE_INTERVAL_SECONDS",
    "HEALTH_TEMP_WARN_C",
    "HEALTH_TEMP_FAIL_C",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Tests must not see the developer's real environment.
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_env(tmp_path, **values) -> str:
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return str(path)


def _valid() -> Config:
    return Config(lattice_endpoint="example.test", environment_token="tok")


# -- loading ---------------------------------------------------------------


def test_defaults_when_nothing_is_set(tmp_path):
    cfg = load(str(tmp_path / "missing.env"))
    assert cfg.entity_id == ""  # resolved at startup, see entity/identity.py
    assert cfg.entity_name == "RPi Camera"
    assert cfg.camera_latitude == 49.65108
    assert cfg.camera_longitude == 11.79045
    assert cfg.camera_altitude_hae_meters == 1048.0
    assert cfg.video_enabled is True
    assert cfg.tasking_enabled is True
    assert cfg.task_heartbeat_interval_ms == 30000
    assert cfg.state_file == "./state.json"
    assert cfg.mediamtx_api_url == "http://127.0.0.1:9997"
    assert cfg.mediamtx_path == "cam"
    assert cfg.health_enabled is True
    assert cfg.health_sample_interval_seconds == 5.0
    assert cfg.health_temp_warn_c == 75.0
    assert cfg.health_temp_fail_c == 85.0
    assert cfg._config_path == str(tmp_path / "missing.env")


def test_file_values_are_loaded(tmp_path):
    path = _write_env(
        tmp_path,
        LATTICE_ENDPOINT="env.example.test",
        ENVIRONMENT_TOKEN="tok",
        CAMERA_LATITUDE="37.5",
        CAMERA_LONGITUDE="-122.25",
        CAMERA_ALTITUDE_HAE_METERS="12",
        VIDEO_ENABLED="false",
        TASK_HEARTBEAT_INTERVAL_MS="5000",
        STATE_FILE="/var/lib/cam/state.json",
        MEDIAMTX_API_URL="http://localhost:9998",
        MEDIAMTX_PATH="front",
        HEALTH_ENABLED="false",
        HEALTH_SAMPLE_INTERVAL_SECONDS="2.5",
        HEALTH_TEMP_WARN_C="70",
        HEALTH_TEMP_FAIL_C="80.5",
    )
    cfg = load(path)
    assert cfg.lattice_endpoint == "env.example.test"
    assert cfg.camera_latitude == 37.5
    assert cfg.camera_longitude == -122.25
    assert cfg.camera_altitude_hae_meters == 12.0
    assert cfg.video_enabled is False
    assert cfg.task_heartbeat_interval_ms == 5000
    assert cfg.state_file == "/var/lib/cam/state.json"
    assert cfg.mediamtx_api_url == "http://localhost:9998"
    assert cfg.mediamtx_path == "front"
    assert cfg.health_enabled is False
    assert cfg.health_sample_interval_seconds == 2.5
    assert cfg.health_temp_warn_c == 70.0
    assert cfg.health_temp_fail_c == 80.5


def test_environment_overrides_file(tmp_path, monkeypatch):
    path = _write_env(tmp_path, LATTICE_ENDPOINT="from-file", CAMERA_LATITUDE="1.0")
    monkeypatch.setenv("LATTICE_ENDPOINT", "from-env")
    monkeypatch.setenv("CAMERA_LATITUDE", "2.0")
    cfg = load(path)
    assert cfg.lattice_endpoint == "from-env"
    assert cfg.camera_latitude == 2.0


def test_empty_values_fall_back_to_defaults(tmp_path, monkeypatch):
    path = _write_env(tmp_path, CAMERA_LATITUDE="", VIDEO_ENABLED="", ENTITY_ID="")
    monkeypatch.setenv("TASK_HEARTBEAT_INTERVAL_MS", "   ")
    cfg = load(path)
    assert cfg.camera_latitude == 49.65108
    assert cfg.video_enabled is True
    assert cfg.entity_id == ""  # resolved at startup, see entity/identity.py
    assert cfg.task_heartbeat_interval_ms == 30000


def test_values_are_stripped(tmp_path):
    path = _write_env(tmp_path, ENTITY_ID="  cam-7  ", CAMERA_LATITUDE=" 3.5 ")
    cfg = load(path)
    assert cfg.entity_id == "cam-7"
    assert cfg.camera_latitude == 3.5


# -- strict parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "CAMERA_LATITUDE",
        "CAMERA_LONGITUDE",
        "CAMERA_ALTITUDE_HAE_METERS",
        "HEALTH_SAMPLE_INTERVAL_SECONDS",
        "HEALTH_TEMP_WARN_C",
        "HEALTH_TEMP_FAIL_C",
    ],
)
def test_bad_float_names_the_key(tmp_path, monkeypatch, key):
    monkeypatch.setenv(key, "abc")
    with pytest.raises(ConfigError) as info:
        load(str(tmp_path / ".env"))
    assert key in str(info.value)
    assert "abc" in str(info.value)


@pytest.mark.parametrize("value", ["abc", "1.5", "3e3"])
def test_bad_int_names_the_key(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TASK_HEARTBEAT_INTERVAL_MS", value)
    with pytest.raises(ConfigError, match="TASK_HEARTBEAT_INTERVAL_MS"):
        load(str(tmp_path / ".env"))


@pytest.mark.parametrize("key", ["VIDEO_ENABLED", "TASKING_ENABLED", "HEALTH_ENABLED"])
@pytest.mark.parametrize("value", ["maybe", "2", "y", "enabled"])
def test_bad_bool_names_the_key(tmp_path, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match=key):
        load(str(tmp_path / ".env"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("On", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("OFF", False),
    ],
)
def test_bool_spellings(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("VIDEO_ENABLED", value)
    assert load(str(tmp_path / ".env")).video_enabled is expected


def test_skip_tls_verify_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIP_TLS_VERIFY", "true")
    cfg = load(str(tmp_path / ".env"))
    assert not hasattr(cfg, "skip_tls_verify_requested")


# -- validation ------------------------------------------------------------


def test_valid_config_passes():
    _valid().validate()


def test_endpoint_required():
    with pytest.raises(ConfigError, match="LATTICE_ENDPOINT"):
        Config(environment_token="tok").validate()


def test_auth_required():
    with pytest.raises(ConfigError, match="ENVIRONMENT_TOKEN"):
        Config(lattice_endpoint="example.test").validate()
    with pytest.raises(ConfigError, match="CLIENT_SECRET"):
        Config(lattice_endpoint="example.test", client_id="id").validate()
    Config(lattice_endpoint="example.test", client_id="id", client_secret="s").validate()


def test_static_token_preferred():
    cfg = Config(
        lattice_endpoint="example.test", environment_token="tok", client_id="id", client_secret="s"
    )
    assert cfg.uses_static_token is True
    oauth_only = Config(lattice_endpoint="example.test", client_id="id", client_secret="s")
    assert oauth_only.uses_static_token is False


def test_missing_ca_path_rejected(tmp_path):
    cfg = _valid()
    cfg.ca_cert_path = str(tmp_path / "nope.pem")
    with pytest.raises(ConfigError, match="LATTICE_CA_CERT_PATH"):
        cfg.validate()
    (tmp_path / "ca.pem").write_text("x")
    cfg.ca_cert_path = str(tmp_path / "ca.pem")
    cfg.validate()


@pytest.mark.parametrize("lat", [-90.0001, 90.0001, 1000.0])
def test_latitude_range(lat):
    cfg = _valid()
    cfg.camera_latitude = lat
    with pytest.raises(ConfigError, match="CAMERA_LATITUDE"):
        cfg.validate()


@pytest.mark.parametrize("lon", [-180.0001, 180.0001])
def test_longitude_range(lon):
    cfg = _valid()
    cfg.camera_longitude = lon
    with pytest.raises(ConfigError, match="CAMERA_LONGITUDE"):
        cfg.validate()


def test_range_boundaries_are_inclusive():
    cfg = _valid()
    cfg.camera_latitude = -90.0
    cfg.camera_longitude = 180.0
    cfg.validate()


@pytest.mark.parametrize("alt", [float("nan"), float("inf"), float("-inf")])
def test_altitude_must_be_finite(alt):
    cfg = _valid()
    cfg.camera_altitude_hae_meters = alt
    with pytest.raises(ConfigError, match="CAMERA_ALTITUDE_HAE_METERS"):
        cfg.validate()


def test_nan_from_env_is_rejected(tmp_path, monkeypatch):
    # float("nan") parses; validate() must still refuse it.
    monkeypatch.setenv("CAMERA_ALTITUDE_HAE_METERS", "nan")
    with pytest.raises(ConfigError, match="CAMERA_ALTITUDE_HAE_METERS"):
        cfg = load(str(tmp_path / ".env"))
        cfg.lattice_endpoint, cfg.environment_token = "example.test", "tok"
        cfg.validate()


def test_heartbeat_interval_non_negative():
    cfg = _valid()
    cfg.task_heartbeat_interval_ms = -1
    with pytest.raises(ConfigError, match="TASK_HEARTBEAT_INTERVAL_MS"):
        cfg.validate()
    cfg.task_heartbeat_interval_ms = 1000
    cfg.validate()


def test_heartbeats_required_when_tasking_enabled():
    # The health "tasking" probe relies on heartbeats, so 0 (disabled) and
    # sub-second intervals are rejected while tasking is on, but fine when off.
    cfg = _valid()
    cfg.tasking_enabled = True
    for value in (0, 999):
        cfg.task_heartbeat_interval_ms = value
        with pytest.raises(ConfigError, match="TASK_HEARTBEAT_INTERVAL_MS must be >= 1000"):
            cfg.validate()
    cfg.tasking_enabled = False
    cfg.task_heartbeat_interval_ms = 0
    cfg.validate()


def test_task_package_required_when_tasking_enabled():
    cfg = _valid()
    cfg.task_package = ""
    with pytest.raises(ConfigError, match="TASK_PACKAGE"):
        cfg.validate()
    cfg.tasking_enabled = False
    cfg.validate()


@pytest.mark.parametrize("interval", [0.0, 0.25, 0.4999, -1.0, float("nan")])
def test_health_sample_interval_minimum(interval):
    cfg = _valid()
    cfg.health_sample_interval_seconds = interval
    with pytest.raises(ConfigError, match="HEALTH_SAMPLE_INTERVAL_SECONDS"):
        cfg.validate()
    cfg.health_sample_interval_seconds = 0.5
    cfg.validate()


@pytest.mark.parametrize(("warn", "fail"), [(85.0, 85.0), (90.0, 85.0), (75.0, 74.9)])
def test_health_temp_warn_must_be_below_fail(warn, fail):
    cfg = _valid()
    cfg.health_temp_warn_c, cfg.health_temp_fail_c = warn, fail
    with pytest.raises(ConfigError, match="HEALTH_TEMP_FAIL_C"):
        cfg.validate()
    cfg.health_temp_warn_c, cfg.health_temp_fail_c = 70.0, 80.0
    cfg.validate()


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_health_temp_thresholds_must_be_finite(value):
    cfg = _valid()
    cfg.health_temp_warn_c = value
    with pytest.raises(ConfigError, match="HEALTH_TEMP"):
        cfg.validate()


def test_health_thresholds_validated_even_when_disabled():
    # A bad threshold is a config error regardless of HEALTH_ENABLED, so that
    # turning health on later never fails at that moment.
    cfg = _valid()
    cfg.health_enabled = False
    cfg.health_temp_fail_c = cfg.health_temp_warn_c
    with pytest.raises(ConfigError, match="HEALTH_TEMP_FAIL_C"):
        cfg.validate()


def test_identity_keys_load_with_defaults(tmp_path, monkeypatch):
    for key in ("NATIONALITY", "ALTERNATE_ID", "ALTERNATE_ID_TYPE"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("LATTICE_ENDPOINT=example.test\nENVIRONMENT_TOKEN=tok\n")
    cfg = load(str(tmp_path / ".env"))
    assert cfg.nationality == "UNITED_STATES_OF_AMERICA"
    assert cfg.alternate_id == ""
    assert cfg.alternate_id_type == "SERIAL_NUMBER"

    (tmp_path / ".env").write_text(
        "LATTICE_ENDPOINT=example.test\nENVIRONMENT_TOKEN=tok\n"
        "NATIONALITY=GERMANY\nALTERNATE_ID=cam-7\nALTERNATE_ID_TYPE=REGISTRATION_ID\n"
    )
    cfg = load(str(tmp_path / ".env"))
    assert (cfg.nationality, cfg.alternate_id, cfg.alternate_id_type) == (
        "GERMANY",
        "cam-7",
        "REGISTRATION_ID",
    )


def test_entity_id_must_be_a_uuid_when_set():
    cfg = _valid()
    cfg.entity_id = "rpi-cam-01"
    with pytest.raises(ConfigError, match="ENTITY_ID must be a UUID"):
        cfg.validate()
    cfg.entity_id = "6BA7B810-9DAD-11D1-80B4-00C04FD430C8"
    cfg.validate()
    cfg.entity_id = ""
    cfg.validate()
