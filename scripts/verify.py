"""Live verification driver — run this SEPARATELY from the daemon, on a network
where the Lattice endpoint is reachable.

It constructs its own client and reads back the camera entity the daemon
publishes, proving the publish -> read-back round-trip (the definition of done).
The camera's entity id is resolved exactly as the daemon does it, so --entity-id defaults
to it.

Usage:
    python scripts/verify.py --config .env [--entity-id <uuid>]
"""

from __future__ import annotations

import argparse
import sys

# Make ``src`` importable when run directly from the repo root.
sys.path.insert(0, "src")

from lattice_cam import config as config_module  # noqa: E402
from lattice_cam.camera.source import pi_serial_number  # noqa: E402
from lattice_cam.entity.identity import resolve_entity_id  # noqa: E402
from lattice_cam.lattice import LatticeClient  # noqa: E402
from lattice_cam.state import StateStore  # noqa: E402


def _print_health(health) -> None:
    """One line for the roll-up, then one per component and one per alert."""
    if health is None:
        print("health:          <none reported>")
        return
    print(f"health: {health.health_status.name} connection: {health.connection_status.name}")
    for component in health.components or []:
        messages = "; ".join(m.message for m in component.messages or [])
        print(f"  {component.id} {component.health.name}: {messages}")
    for alert in health.active_alerts or []:
        print(f"  ALERT {alert.level.name} {alert.alert_code}: {alert.description}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a published entity back from Lattice.")
    parser.add_argument("--config", default=".env")
    parser.add_argument(
        "--entity-id",
        default=None,
        help="entity id to read back (defaults to config ENTITY_ID)",
    )
    args = parser.parse_args()

    config = config_module.load(args.config)
    config.validate()
    # Same resolution as the daemon: ENTITY_ID, else state.json, else the
    # id derived from this board's serial.
    entity_id = args.entity_id or resolve_entity_id(
        config, StateStore(config.state_file), pi_serial_number()
    )

    with LatticeClient(config) as client:
        response = client.entities.get_entity(entity_id, timeout_ms=30000)
        entity = response.entity
        print("entity_id:      ", entity.entity_id)
        print("is_live:        ", entity.is_live)
        print("aliases.name:   ", entity.aliases.name)
        alternate_ids = entity.aliases.alternate_ids if entity.aliases is not None else []
        for alt in alternate_ids or []:
            print("alternate id:   ", f"{alt.type.name}={alt.id}")
        if entity.mil_view is not None and entity.mil_view.nationality is not None:
            print("nationality:    ", entity.mil_view.nationality.name)
        pos = entity.location.position
        print(
            "position:       ",
            f"lat={pos.latitude_degrees:.5f} lon={pos.longitude_degrees:.5f}",
        )
        if entity.media is not None and entity.media.media:
            print("video id (media):", entity.media.media[0].item_identifier)
        else:
            print("video id (media): <none advertised>")
        if entity.sensors is not None and entity.sensors.sensors:
            print("sensor state:   ", entity.sensors.sensors[0].operational_state.name)
        if entity.task_catalog is not None and entity.task_catalog.task_definitions:
            print("task catalog:")
            for definition in entity.task_catalog.task_definitions:
                print("   ", definition.task_specification_url)
        else:
            print("task catalog:    <none advertised>")
        _print_health(entity.health)
        print("expiry_time:    ", entity.expiry_time.to_datetime().isoformat())
        if not entity.is_live:
            print("WARNING: entity is not live", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
