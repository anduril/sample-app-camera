"""lattice-cam — a Raspberry Pi camera Lattice integration built on the
Lattice SDK for gRPC (Connect) in Python.

A long-running daemon that publishes the camera as a stationary asset entity
once per second, registers its SRT video ingress with Lattice's VideoManager,
and acts as a Lattice agent executing ``Start`` / ``Stop`` tasks.

Subpackages:

* ``lattice``  — TLS transport, auth metadata, one thin client per Lattice API.
* ``entity``   — the composition seam: a base entity plus one contributor per
                 component (location, sensors, media, task catalog, health).
* ``camera``   — the camera's position, the media pipeline and its status
                 probe, the task-driven Start/Stop control, and the video
                 ingress lifecycle.
* ``tasking``  — the task definitions the camera advertises and the agent
                 handler that executes them.
* ``health``   — probes sampled on their own cadence, rolled up into the
                 entity's ``Health`` component.

Root modules: ``config`` (.env), ``state`` (persistent JSON state),
``logging_setup`` (JSON logs), ``runtime`` (publish loop, workers, signals),
``main`` (CLI wiring).
"""

__version__ = "0.2.0"
