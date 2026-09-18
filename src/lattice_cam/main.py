"""CLI entry point: configuration, wiring, and the run/shutdown sequence.

This module is deliberately *only* wiring. Every feature lives in its own
package and is plugged in here:

* ``lattice``  — the client (transport, auth, one thin client per API)
* ``camera``   — the media pipeline, the Start/Stop control, the video ingress
* ``entity``   — the composition seam: a base entity plus one *contributor*
                 per component (location, sensors, media, task catalog, health)
* ``tasking``  — the agent stream that executes Start/Stop
* ``health``   — probes sampled on their own cadence, published as ``Health``

Adding a feature means adding a package and a few lines in ``_build_*`` below,
never changing the publish loop or the other packages.

Run sequence: recover any ingress a previous process left behind, bring the
stream up (best-effort), run until a signal, then stop the stream, archive the
ingress, and publish one final entity marked offline.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from . import config as config_module
from .camera.control import CameraControl
from .camera.ingress import VideoIngress
from .camera.pipeline import CommandPipeline, MediaMtxStatusProbe
from .camera.source import CameraSource, pi_serial_number
from .config import Config, ConfigError
from .entity import (
    CameraObservation,
    EntityBuilder,
    EntityContributor,
    LocationContributor,
    MediaContributor,
    SensorsContributor,
    TaskCatalogContributor,
)
from .entity.base import validate_identity
from .entity.identity import resolve_entity_id
from .health import (
    HealthContributor,
    HealthSampler,
    PipelineProbe,
    PowerProbe,
    Probe,
    SystemProbe,
    TaskStreamProbe,
    ThermalProbe,
    ThrottledFlagsReader,
)
from .lattice import LatticeClient
from .logging_setup import configure, get_logger
from .runtime import Runtime, Worker
from .state import StateStore
from .tasking.handler import TaskHandler

CREATED_TIME_KEY = "created_time"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lattice-cam",
        description="Publish a Raspberry Pi camera as a Lattice entity via gRPC (Connect).",
    )
    parser.add_argument(
        "--config", default=".env", help="path to the .env config file (default: .env)"
    )
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def _created_time(state: StateStore) -> datetime:
    """A stable-id asset keeps one creation time across restarts."""
    raw = state.get(CREATED_TIME_KEY)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    created = datetime.now(UTC)
    state.set(CREATED_TIME_KEY, created.isoformat())
    return created


def _build_pipeline(config: Config) -> CommandPipeline:
    """The media pipeline: shell commands to start/stop MediaMTX, observed via its API."""
    probe = MediaMtxStatusProbe(api_url=config.mediamtx_api_url, path=config.mediamtx_path)
    return CommandPipeline(
        start_command=config.task_start_command,
        stop_command=config.task_stop_command,
        probe=probe,
    )


def _build_control(
    client: LatticeClient, config: Config, state: StateStore, pipeline: CommandPipeline, logger
) -> CameraControl:
    """Wire the task-driven camera control, with the video ingress when enabled."""
    video = None
    if config.video_enabled:
        video = VideoIngress(
            client,
            title=config.video_title or f"{config.entity_name} (SRT)",
            passphrase=config.srt_passphrase,
            srt_target_file=config.srt_target_file,
            state=state,
        )
    else:
        logger.info("video ingress disabled (VIDEO_ENABLED=false)")
    return CameraControl(pipeline=pipeline, video=video)


def _build_health(
    config: Config,
    control: CameraControl,
    pipeline: CommandPipeline,
    task_handler: TaskHandler | None,
) -> HealthSampler | None:
    """The health probes and the worker that samples them; ``None`` when disabled."""
    if not config.health_enabled:
        return None
    throttled = ThrottledFlagsReader()  # one ``vcgencmd`` per round, shared by two probes
    probes: list[Probe] = [
        ThermalProbe(
            throttled=throttled,
            warn_c=config.health_temp_warn_c,
            fail_c=config.health_temp_fail_c,
        ),
        PowerProbe(throttled=throttled),
        PipelineProbe(status=pipeline.status, desired_on=lambda: control.snapshot().desired_on),
        SystemProbe(),
    ]
    if task_handler is not None:
        probes.append(
            TaskStreamProbe(
                stream_state=task_handler.stream_state,
                heartbeat_interval_ms=config.task_heartbeat_interval_ms,
            )
        )
    return HealthSampler(probes, interval_s=config.health_sample_interval_seconds)


def _build_entity_builder(
    config: Config,
    *,
    created_time: datetime,
    source: CameraSource,
    control: CameraControl,
    pipeline: CommandPipeline,
    task_handler: TaskHandler | None,
    health: HealthSampler | None,
) -> EntityBuilder:
    """Compose the entity: identity floor plus one contributor per component."""

    def observe() -> CameraObservation:
        # Runs on every publish tick, so it must not block: with health
        # enabled the pipeline status comes from the sampler's cache (at most
        # one sampling interval old); without it, from the pipeline's own
        # short-timeout, TTL-cached probe as before.
        desired_on = control.snapshot().desired_on
        if not desired_on:
            return CameraObservation(desired_on=False, ready=None)
        if health is None:
            return CameraObservation(desired_on=True, ready=pipeline.status().ready)
        snapshot = health.snapshot()
        if snapshot is None:
            return CameraObservation(desired_on=True, ready=None)
        ready = snapshot.pipeline.ready if snapshot.pipeline is not None else None
        return CameraObservation(desired_on=True, ready=ready, degraded=snapshot.is_degraded)

    # With health disabled the contributor still publishes Health, as
    # NOT_READY, so operators see that nothing is being sampled.
    health_snapshot = health.snapshot if health is not None else lambda: None
    contributors: list[EntityContributor] = [
        LocationContributor(source),
        SensorsContributor(observe),
        MediaContributor(lambda: control.snapshot().video_id),
        HealthContributor(health_snapshot),
    ]
    if task_handler is not None:
        # The catalog follows the stream state: Start is withdrawn once the
        # stream is on, Stop once it is off (see TaskHandler.offered_task_urls).
        handler = task_handler
        contributors.append(
            TaskCatalogContributor(lambda: handler.offered_task_urls(control.snapshot().desired_on))
        )
    return EntityBuilder(
        config,
        entity_id=config.entity_id,
        created_time=created_time,
        contributors=contributors,
    )


def _initial_start(control: CameraControl, logger) -> None:
    """Bring the stream up at boot.

    Best-effort: on failure (for example an unreachable endpoint) the service
    still publishes the camera entity, reporting the sensor OFF and no video,
    and an operator's Start task is the retry.
    """
    try:
        control.start()
    except Exception as exc:
        logger.warning(
            "could not start the camera stream at boot; publishing with the "
            "sensor OFF and no video reference (send a Start task to retry)",
            error=str(exc),
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure(debug=args.debug)
    logger = get_logger()

    try:
        config = config_module.load(args.config)
        config.validate()
        validate_identity(config)
    except ConfigError as exc:
        logger.error("invalid configuration", error=str(exc))
        return 2
    if not config.alternate_id:
        # No alias configured: fall back to the board's hardware serial, which
        # is stable and unique, so other systems can correlate the asset.
        config.alternate_id = pi_serial_number() or ""
        if config.alternate_id:
            logger.info("using the Pi serial as the alternate id", alternate_id=config.alternate_id)
        else:
            logger.info("no alternate id available; alias omitted")

    try:
        client = LatticeClient(config)
    except Exception as exc:
        logger.error("failed to initialize Lattice client", error=str(exc))
        return 1

    state = StateStore(config.state_file)
    # A UUID that survives restarts: ENTITY_ID, else the previous run's id,
    # else one derived from the board serial (entity/identity.py).
    config.entity_id = resolve_entity_id(config, state, pi_serial_number())
    pipeline = _build_pipeline(config)
    control = _build_control(client, config, state, pipeline, logger)
    runtime: Runtime | None = None
    try:
        source = CameraSource(
            latitude_degrees=config.camera_latitude,
            longitude_degrees=config.camera_longitude,
            altitude_hae_meters=config.camera_altitude_hae_meters,
        )

        task_handler = None
        if config.tasking_enabled:
            task_handler = TaskHandler(
                client,
                agent_entity_id=config.entity_id,
                task_package=config.task_package,
                control=control,
                heartbeat_interval_ms=config.task_heartbeat_interval_ms,
            )
        else:
            logger.info("tasking disabled (TASKING_ENABLED=false)")

        health = _build_health(config, control, pipeline, task_handler)
        if health is None:
            logger.info("health telemetry disabled (HEALTH_ENABLED=false)")

        builder = _build_entity_builder(
            config,
            created_time=_created_time(state),
            source=source,
            control=control,
            pipeline=pipeline,
            task_handler=task_handler,
            health=health,
        )
        workers: list[tuple[str, Worker]] = []
        if task_handler is not None:
            workers.append(("tasking", task_handler.run))
        if health is not None:
            workers.append(("health", health.run))
        runtime = Runtime(config, client, builder=builder, workers=workers)
        # Start/Stop push their outcome to the asset immediately (Media item
        # added or cleared, the task just run withdrawn from the catalog)
        # instead of waiting for the next 1 Hz tick.
        control.on_change = runtime.publish_now

        # Archive an ingress a crashed predecessor never cleaned up, then
        # register a fresh one. Sample health first so the publish that
        # follows the initial Start already carries real telemetry.
        if health is not None:
            health.sample_once()
        control.recover()
        _initial_start(control, logger)
        return runtime.run()
    finally:
        # Leave nothing pushing at, or advertised as, a stream that no longer
        # has a producer, and tell operators the asset went offline on purpose.
        control.shutdown()
        if runtime is not None:
            runtime.publish_offline()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
