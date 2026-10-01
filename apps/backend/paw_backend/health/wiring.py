"""How the application builds System Health (PAW-066).

``build_system_health`` returns the monitor with every source, plus the two
sources whose subject the application creates later: the connection reaper
(started in the lifespan) and the Compute Scheduler (issue #165), each attached
with ``attach``. Without a database the database-backed sources are left out
(the report then holds the database as ``not_configured``, the compute and the
reaper) and nothing is sampled.
"""

from dataclasses import dataclass

from paw_backend.authz.retention.audit import RUN_RESOURCE_KIND, RetentionAction
from paw_backend.compute.probe import GpuProbe
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.events import EventBus, notification_changed
from paw_backend.health import limits
from paw_backend.health.domain import Component
from paw_backend.health.monitor import HealthMonitor
from paw_backend.health.sources import (
    ComputeSource,
    ComputeStatusProvider,
    ConnectionSource,
    DatabaseSource,
    FullGpuStatusProvider,
    HealthSource,
    MemoryWorkerSource,
    ReaperSource,
    ScheduledJob,
    ScheduledJobSource,
    TaskQueueSource,
)
from paw_backend.health.store import NOTIFICATION_AUDIENCE, HealthStore
from paw_backend.memory.projection.audit import RESOURCE_KIND as PROJECTION_KIND
from paw_backend.memory.projection.audit import ProjectionAction
from paw_backend.recovery.audit import BACKUP_RESOURCE_KIND, RecoveryAction

SCHEDULED_JOBS = (
    ScheduledJob(
        Component.RECOVERY_BACKUP,
        resource_kind=BACKUP_RESOURCE_KIND,
        completed=RecoveryAction.BACKUP_COMPLETED.value,
        failed=RecoveryAction.BACKUP_FAILED.value,
        stale_after=limits.RECOVERY_BACKUP_STALE,
    ),
    ScheduledJob(
        Component.MEMORY_PROJECTION,
        resource_kind=PROJECTION_KIND,
        completed=ProjectionAction.COMPLETED.value,
        failed=ProjectionAction.FAILED.value,
        stale_after=limits.MEMORY_PROJECTION_STALE,
    ),
    ScheduledJob(
        Component.AUDIT_RETENTION,
        resource_kind=RUN_RESOURCE_KIND,
        completed=RetentionAction.MAINTENANCE_COMPLETED.value,
        failed=RetentionAction.MAINTENANCE_FAILED.value,
        stale_after=limits.AUDIT_RETENTION_STALE,
    ),
)


@dataclass(frozen=True, slots=True)
class SystemHealth:
    monitor: HealthMonitor
    compute: ComputeSource
    reaper: ReaperSource
    # The history (``None`` without a database: the API then answers 503).
    store: HealthStore | None
    # Whether the application runs the sampling loop.
    sampling: bool


def build_system_health(
    settings: Settings,
    database: Database,
    *,
    compute: ComputeStatusProvider | None = None,
    full_gpu: FullGpuStatusProvider | None = None,
    probe: GpuProbe | None = None,
    event_bus: EventBus | None = None,
) -> SystemHealth:
    compute_source = ComputeSource(compute, full_gpu, probe=probe)
    reaper_source = ReaperSource()
    sources: list[HealthSource] = [DatabaseSource(database), compute_source]
    if database.configured:
        sources += [
            TaskQueueSource(database),
            MemoryWorkerSource(database),
            ConnectionSource(database),
        ]
    sources.append(reaper_source)
    if database.configured:
        sources += [ScheduledJobSource(database, job) for job in SCHEDULED_JOBS]
    sampling = database.configured
    store = None
    if database.configured:
        # A severity change is also a stored notification for the System Health
        # audience (issue #188, Decision 0070), announced on the event bus.
        on_notified = None
        if event_bus is not None:
            bus = event_bus

            def on_notified() -> None:
                bus.publish(notification_changed(capability=NOTIFICATION_AUDIENCE))

        store = HealthStore(database, notify=True, on_notified=on_notified)
    monitor = HealthMonitor(
        sources,
        store=store,
        sample_interval_seconds=settings.health_sample_interval_seconds,
        retention_days=settings.health_retention_days,
    )
    return SystemHealth(monitor, compute_source, reaper_source, store, sampling)
