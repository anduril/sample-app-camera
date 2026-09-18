"""Entity id resolution: a UUID that is stable across restarts and reinstalls."""

from __future__ import annotations

import uuid

from lattice_cam.config import Config
from lattice_cam.entity.identity import (
    STATE_KEY,
    canonical_uuid,
    derived_entity_id,
    resolve_entity_id,
)
from lattice_cam.state import StateStore

SERIAL = "10000000deadbeef"


def _cfg(entity_id: str = "") -> Config:
    return Config(lattice_endpoint="example.test", environment_token="tok", entity_id=entity_id)


def _is_uuid(value: str) -> bool:
    return str(uuid.UUID(value)) == value


def test_derived_id_is_a_uuid_and_deterministic():
    a = derived_entity_id(SERIAL)
    assert _is_uuid(a)
    assert a == derived_entity_id(f"  {SERIAL}\n")  # whitespace does not change identity
    assert a != derived_entity_id("10000000cafef00d")


def test_serial_derivation_is_recorded_and_reused(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    first = resolve_entity_id(_cfg(), state, SERIAL)
    assert first == derived_entity_id(SERIAL)
    assert state.get(STATE_KEY) == first

    # Next boot on the same board, even with the serial unreadable: same id.
    again = resolve_entity_id(_cfg(), StateStore(str(tmp_path / "state.json")), None)
    assert again == first


def test_configured_uuid_wins_and_is_canonicalised(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.set(STATE_KEY, derived_entity_id(SERIAL))
    configured = "6BA7B810-9DAD-11D1-80B4-00C04FD430C8"
    resolved = resolve_entity_id(_cfg(configured), state, SERIAL)
    assert resolved == configured.lower()
    assert state.get(STATE_KEY) == resolved  # the record follows the operator's choice


def test_without_serial_a_random_id_is_generated_once(tmp_path):
    path = str(tmp_path / "state.json")
    first = resolve_entity_id(_cfg(), StateStore(path), None)
    assert _is_uuid(first)
    assert resolve_entity_id(_cfg(), StateStore(path), None) == first


def test_garbage_in_state_is_ignored(tmp_path):
    state = StateStore(str(tmp_path / "state.json"))
    state.set(STATE_KEY, "rpi-cam-01")
    resolved = resolve_entity_id(_cfg(), state, SERIAL)
    assert resolved == derived_entity_id(SERIAL)
    assert state.get(STATE_KEY) == resolved


def test_canonical_uuid():
    assert canonical_uuid(" 6BA7B810-9DAD-11D1-80B4-00C04FD430C8 ") == (
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    )
    assert canonical_uuid("rpi-cam-01") is None
    assert canonical_uuid("") is None
