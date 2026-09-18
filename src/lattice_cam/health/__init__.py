"""Health telemetry: probes -> sampler (worker, cache) -> contributor (entity ``Health``).

This package is the worked example of adding a feature to the service. It
touches nothing outside itself except one ``CameraObservation`` field and a
few wiring lines in ``main.py``:

* ``report.py``      plain-Python types the rest of the package speaks
* ``probes.py``      one class per observed source; never raise, fully injectable
* ``sampler.py``     the background worker that runs the probes and caches a snapshot
* ``contributor.py`` the ``EntityContributor`` that maps the snapshot to the SDK
"""

from .contributor import HealthContributor
from .probes import (
    PipelineProbe,
    PowerProbe,
    Probe,
    ProbeBase,
    SystemProbe,
    TaskStreamProbe,
    ThermalProbe,
    ThrottledFlags,
    ThrottledFlagsReader,
)
from .report import (
    AlertLevel,
    AlertReport,
    ComponentReport,
    HealthSnapshot,
    ProbeResult,
    Status,
)
from .sampler import HealthSampler

__all__ = [
    "AlertLevel",
    "AlertReport",
    "ComponentReport",
    "HealthContributor",
    "HealthSampler",
    "HealthSnapshot",
    "PipelineProbe",
    "PowerProbe",
    "Probe",
    "ProbeBase",
    "ProbeResult",
    "Status",
    "SystemProbe",
    "TaskStreamProbe",
    "ThermalProbe",
    "ThrottledFlags",
    "ThrottledFlagsReader",
]
