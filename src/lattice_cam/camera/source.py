"""The camera as a sensor source.

``CameraState`` is a plain-Python snapshot of what the camera reports about
itself, deliberately decoupled from any Lattice/protobuf type. The Raspberry Pi
camera is a *stationary* sensor: it reports a fixed geodetic location from
config.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CameraState:
    """A single observation of the camera: its geodetic position."""

    latitude_degrees: float
    longitude_degrees: float
    altitude_hae_meters: float


class CameraSource:
    """A stationary Raspberry Pi camera at a fixed, configured location."""

    def __init__(
        self,
        latitude_degrees: float,
        longitude_degrees: float,
        altitude_hae_meters: float,
    ) -> None:
        self._latitude = latitude_degrees
        self._longitude = longitude_degrees
        self._altitude_hae = altitude_hae_meters

    def next_state(self) -> CameraState:
        return CameraState(
            latitude_degrees=self._latitude,
            longitude_degrees=self._longitude,
            altitude_hae_meters=self._altitude_hae,
        )


PI_CPUINFO_PATH = "/proc/cpuinfo"


def pi_serial_number(path: str = PI_CPUINFO_PATH) -> str | None:
    """The Raspberry Pi's hardware serial from ``/proc/cpuinfo``, or ``None``.

    Used as the entity's alternate id when none is configured: it is stable
    across reinstalls and unique per board, so other systems can correlate
    the asset with the physical unit.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.partition(":")
                if sep and key.strip() == "Serial":
                    serial = value.strip()
                    return serial or None
    except OSError:
        return None
    return None
