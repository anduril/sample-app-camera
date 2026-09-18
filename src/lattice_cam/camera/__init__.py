"""The camera path: its observed state, the task-driven stream control, the
media pipeline that realises it, and the SRT video ingress lifecycle."""

from .control import CameraControl, CameraControlError, ControlSnapshot
from .ingress import VideoIngress
from .pipeline import (
    CommandPipeline,
    MediaMtxStatusProbe,
    MediaPipeline,
    PipelineError,
    PipelineStatus,
)
from .source import CameraSource, CameraState

__all__ = [
    "CameraControl",
    "CameraControlError",
    "CameraSource",
    "CameraState",
    "CommandPipeline",
    "ControlSnapshot",
    "MediaMtxStatusProbe",
    "MediaPipeline",
    "PipelineError",
    "PipelineStatus",
    "VideoIngress",
]
