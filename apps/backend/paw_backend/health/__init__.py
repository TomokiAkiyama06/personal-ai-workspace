"""System Health / Observability (PAW-066).

The sources (``sources.py``) read each watched component (PostgreSQL, the GPU and
the models, the task queue, the Memory Worker's queue, the shared Codex / Claude
connections, the connection reaper, the Recovery backup, the Memory Projection,
the audit retention) into a severity of the Notification Policy, a closed
status, reason codes and numbers. The monitor (``monitor.py``) combines them into
a report and, in the application, samples the numbers into a downsampled time
series and records severity changes (``store.py``). ``/api/v1/system/health*``
serves them. See ``docs/decisions/0059-system-health-observability.md``
(Proposed).
"""

from paw_backend.health.domain import (
    Component,
    ComponentHealth,
    HealthReport,
    Severity,
    Status,
)
from paw_backend.health.monitor import HealthMonitor
from paw_backend.health.store import HealthStore
from paw_backend.health.wiring import SystemHealth, build_system_health

__all__ = [
    "Component",
    "ComponentHealth",
    "HealthMonitor",
    "HealthReport",
    "HealthStore",
    "Severity",
    "Status",
    "SystemHealth",
    "build_system_health",
]
