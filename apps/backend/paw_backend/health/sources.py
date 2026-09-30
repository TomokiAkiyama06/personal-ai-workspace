"""What System Health watches (PAW-066): one source per component.

Each source is read-only and returns a :class:`ComponentHealth` (codes and
numbers only). Its ``max_age_seconds`` is how long the monitor reuses its answer.
The thresholds are ``limits.py`` (Decision 0059, Proposed).

* :class:`DatabaseSource`: ``Database.check()`` (``SELECT 1``) and its latency.
* :class:`ComputeSource`: the Compute Resource Scheduler's ``status()`` (PAW-036)
  and Full GPU Mode's (PAW-037), through the :class:`ComputeStatusProvider` /
  :class:`FullGpuStatusProvider` protocols; without a scheduler, optionally the
  read-only GPU probe (``nvidia-smi --query-*``) alone; else ``not_configured``.
  It never loads or unloads a model and never starts anything but the probe.
* :class:`TaskQueueSource`: tasks by state, failures (events) of the last
  hour / day, retries and detected loops of the last hour.
* :class:`MemoryWorkerSource`: the Memory consolidation queue (pending, deferred
  for an unreachable worker, expired leases, dead letters).
* :class:`ConnectionSource`: the shared Codex / Claude connections (status,
  enabled, last check, calls in flight). Never the credential or its handle.
* :class:`ReaperSource`: the reaper of abandoned connection calls (its cycles).
* :class:`ScheduledJobSource`: a job run by a timer that records each run in
  ``audit_events`` (the Recovery backup, the Memory Projection, the audit
  retention): the last run, the age of the last success, failures in a row.
"""

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from paw_backend.compute.accounting import account, headroom_bytes
from paw_backend.compute.domain import DeploymentState, ModelRole, SchedulerMode
from paw_backend.compute.errors import ProbeUnavailableError
from paw_backend.compute.full_gpu import FullGpuState, FullGpuStatus
from paw_backend.compute.limits import (
    DEFAULT_HEADROOM_FRACTION,
    DEFAULT_HEADROOM_MIN_BYTES,
)
from paw_backend.compute.probe import GpuProbe
from paw_backend.compute.scheduler import ComputeStatus
from paw_backend.connections.domain import ConnectionKind, ConnectionStatus
from paw_backend.db import Database, DatabaseStatus
from paw_backend.health import limits
from paw_backend.health.domain import (
    Component,
    ComponentHealth,
    MetricValue,
    Severity,
    Status,
    worst,
)
from paw_backend.orchestrator.connection_reaper import ReaperStats
from paw_backend.tasks.queueing.domain import DEFAULT_LOOP_POLICY


class HealthSource(Protocol):
    component: Component
    max_age_seconds: float

    async def check(self) -> ComponentHealth: ...


def _health(
    component: Component,
    severity: Severity,
    status: Status,
    reasons: list[str] | tuple[str, ...] = (),
    metrics: Mapping[str, MetricValue] | None = None,
    parts: tuple[Mapping[str, MetricValue], ...] = (),
) -> ComponentHealth:
    return ComponentHealth(
        component, severity, status, tuple(reasons), metrics or {}, parts
    )


def _status_of(severity: Severity) -> Status:
    return {
        Severity.INFO: Status.OK,
        Severity.WARNING: Status.DEGRADED,
    }.get(severity, Status.FAILING)


def _seconds(value: object) -> float | None:
    return None if value is None else round(float(value), 3)


# -- PostgreSQL -------------------------------------------------------------------


class DatabaseSource:
    """``CRITICAL`` when PostgreSQL does not answer (the Notification Policy's
    "PostgreSQL異常"), a warning when the check itself is slow."""

    component = Component.DATABASE
    max_age_seconds = limits.REPORT_MAX_AGE_SECONDS

    def __init__(self, database: Database) -> None:
        self._database = database

    async def check(self) -> ComponentHealth:
        if not self._database.configured:
            return _health(self.component, Severity.INFO, Status.NOT_CONFIGURED)
        started = time.monotonic()
        status = await self._database.check()
        latency = round((time.monotonic() - started) * 1000, 1)
        if status is not DatabaseStatus.OK:
            return self._unavailable({"up": 0, "latency_ms": latency})
        slow = latency > limits.DATABASE_SLOW_MS
        return _health(
            self.component,
            Severity.WARNING if slow else Severity.INFO,
            Status.DEGRADED if slow else Status.OK,
            ["database_slow"] if slow else [],
            {"up": 1, "latency_ms": latency},
        )

    def on_timeout(self) -> ComponentHealth:
        """The monitor's answer when the check outlives its timeout: the probe's
        own deadline (``PAW_DATABASE_TIMEOUT_SECONDS``) may be longer, and a
        PostgreSQL that does not answer in time is down, not a check that failed."""
        return self._unavailable({"up": 0})

    def _unavailable(self, metrics: dict[str, MetricValue]) -> ComponentHealth:
        return _health(
            self.component,
            Severity.CRITICAL,
            Status.UNAVAILABLE,
            ["database_unavailable"],
            metrics,
        )


# -- GPU / models -----------------------------------------------------------------


class ComputeStatusProvider(Protocol):
    """The Compute Resource Scheduler (``ComputeScheduler.status``)."""

    def status(self) -> ComputeStatus: ...


class FullGpuStatusProvider(Protocol):
    """Kaggle / Full GPU Mode (``FullGpuMode.status``)."""

    def status(self) -> FullGpuStatus: ...


class ComputeSource:
    """The GPU, the VRAM and the models' residency.

    The scheduler is attached when the application builds one (issue #165); until
    then, a probe (``PAW_HEALTH_GPU_PROBE``) gives the GPU and its VRAM without
    the models, and with neither the component is ``not_configured`` (``INFO``).
    """

    component = Component.COMPUTE
    max_age_seconds = limits.REPORT_MAX_AGE_SECONDS

    def __init__(
        self,
        scheduler: ComputeStatusProvider | None = None,
        full_gpu: FullGpuStatusProvider | None = None,
        *,
        probe: GpuProbe | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._full_gpu = full_gpu
        self._probe = probe

    def attach(
        self,
        scheduler: ComputeStatusProvider | None,
        full_gpu: FullGpuStatusProvider | None = None,
    ) -> None:
        """Use ``scheduler`` (and ``full_gpu``) from now on."""
        self._scheduler = scheduler
        self._full_gpu = full_gpu

    async def check(self) -> ComponentHealth:
        if self._scheduler is not None:
            return self._from_scheduler(self._scheduler.status())
        if self._probe is not None:
            return await self._from_probe(self._probe)
        return _health(self.component, Severity.INFO, Status.NOT_CONFIGURED)

    def _from_scheduler(self, status: ComputeStatus) -> ComponentHealth:
        reasons: list[str] = []
        errors = warnings = False
        metrics: dict[str, MetricValue] = {
            "mode": status.mode.value,
            "relief": int(status.relief),
            "probe_ok": status.probe_ok,
            "sample_age_seconds": _seconds(status.sample_age_seconds),
            "utilization_percent": status.utilization_percent,
            "leases": sum(status.leases.values()),
            "cloud_leases": status.cloud_leases,
            "waiting": sum(status.waiting.values()),
            "waiting_for_vram": status.vram_waiting,
            "exclusive_age_seconds": _seconds(status.exclusive_age_seconds),
        }
        for resource_class, count in status.leases.items():
            metrics[f"leases_{resource_class.value}"] = count
        for resource_class, count in status.waiting.items():
            metrics[f"waiting_{resource_class.value}"] = count
        view = status.vram
        if view is not None:
            metrics.update(
                vram_total_bytes=view.total,
                vram_used_bytes=view.actual,
                vram_reserved_bytes=view.reserved,
                vram_external_bytes=view.external,
                vram_headroom_bytes=view.headroom,
                vram_available_bytes=view.available,
            )
        if not status.probe_ok:
            errors = True
            reasons.append("probe_unavailable")
        if status.needs_human:
            errors = True
            reasons.append("main_model_change_needed")
        if view is not None and view.under_pressure:
            warnings = True
            reasons.append("vram_pressure")
        if status.relief > 0 and not status.needs_human:
            warnings = True
            reasons.append("relief_active")
        if status.vram_waiting or status.exclusive_waiting_for_vram:
            warnings = True
            reasons.append("waiting_for_vram")
        parts = []
        for deployment in status.deployments:
            parts.append(
                {
                    "name": deployment.name,
                    "role": deployment.role.value,
                    "state": deployment.state.value,
                    "draining": deployment.draining,
                    "leases": deployment.leases,
                    "reserved_tokens": deployment.reserved_tokens,
                    "capacity_tokens": deployment.capacity_tokens,
                    "max_sequences": deployment.max_sequences,
                    "observed_kv_fraction": deployment.observed_kv_fraction,
                }
            )
            if deployment.state is DeploymentState.FAILED:
                errors = True
                reasons.append(f"model_failed:{deployment.role.value}")
            elif (
                deployment.role is ModelRole.MAIN
                and deployment.state is not DeploymentState.GPU
                and status.mode is SchedulerMode.NORMAL
            ):
                # Kept on the GPU (ResidencyPolicy.ALWAYS) but for an Exclusive job.
                errors = True
                reasons.append("main_model_not_resident")
        metrics["models_on_gpu"] = sum(
            1 for d in status.deployments if d.state is DeploymentState.GPU
        )
        if self._full_gpu is not None:
            full = self._full_gpu.status()
            metrics.update(
                full_gpu_state=full.state.value,
                full_gpu_held_tasks=full.held_tasks,
                full_gpu_on_seconds=_seconds(full.on_seconds),
                full_gpu_last_failure=(
                    None if full.last_failure is None else full.last_failure.value
                ),
            )
            if full.needs_human:
                errors = True
                reasons.append("full_gpu_resume_stuck")
            elif full.last_failure is not None and full.state is FullGpuState.OFF:
                warnings = True
                reasons.append("full_gpu_start_failed")
        severity = (
            Severity.ERROR
            if errors
            else Severity.WARNING
            if warnings
            else Severity.INFO
        )
        return _health(
            self.component,
            severity,
            _status_of(severity),
            reasons,
            metrics,
            tuple(parts),
        )

    async def _from_probe(self, probe: GpuProbe) -> ComponentHealth:
        try:
            sample = await probe.sample()
        except ProbeUnavailableError:
            return _health(
                self.component,
                Severity.ERROR,
                Status.UNAVAILABLE,
                ["probe_unavailable"],
                {"probe_ok": False},
            )
        metrics: dict[str, MetricValue] = {
            "probe_ok": True,
            "devices": len(sample.devices),
        }
        parts = []
        totals = dict.fromkeys(("total", "used", "headroom", "available"), 0)
        utilization: list[int] = []
        pressure = False
        for device in sample.devices:
            headroom = headroom_bytes(
                device.total_bytes,
                minimum_bytes=DEFAULT_HEADROOM_MIN_BYTES,
                fraction=DEFAULT_HEADROOM_FRACTION,
            )
            view = account(device, sample.processes_on(device), (), headroom=headroom)
            pressure = pressure or view.under_pressure
            totals["total"] += view.total
            totals["used"] += view.actual
            totals["headroom"] += view.headroom
            totals["available"] += view.available
            if device.utilization_percent is not None:
                utilization.append(device.utilization_percent)
            # No pid and no process name: they may be another user's.
            parts.append(
                {
                    "index": device.index,
                    "name": device.name,
                    "vram_total_bytes": view.total,
                    "vram_used_bytes": view.actual,
                    "vram_available_bytes": view.available,
                    "utilization_percent": device.utilization_percent,
                    "processes": len(sample.processes_on(device)),
                }
            )
        metrics.update(
            vram_total_bytes=totals["total"],
            vram_used_bytes=totals["used"],
            vram_headroom_bytes=totals["headroom"],
            vram_available_bytes=totals["available"],
            utilization_percent=max(utilization) if utilization else None,
        )
        severity = Severity.WARNING if pressure else Severity.INFO
        return _health(
            self.component,
            severity,
            _status_of(severity),
            ["vram_pressure"] if pressure else [],
            metrics,
            tuple(parts),
        )


# -- tasks and the Memory Worker -----------------------------------------------------

# The tasks by state: the active ones through the partial index
# ``ix_tasks_active_state``, the ones that ended in the last day through
# ``ix_tasks_ended_updated_at`` (revision ``0066``): the cost does not grow with
# the tasks that ended long ago.
_TASKS = """
SELECT
    count(*) FILTER (WHERE state = 'queued'),
    count(*) FILTER (WHERE state = 'running'),
    count(*) FILTER (WHERE state = 'waiting'),
    count(*) FILTER (WHERE state = 'waiting' AND wait_reason = 'resource'),
    count(*) FILTER (WHERE state = 'waiting' AND wait_reason = 'approval'),
    count(*) FILTER (WHERE state = 'waiting' AND wait_reason = 'user'),
    count(*) FILTER (WHERE state = 'paused'),
    count(*) FILTER (WHERE state = 'evaluating'),
    count(*) FILTER (WHERE state = 'completed'),
    count(*) FILTER (WHERE state = 'cancelled')
FROM tasks
WHERE state IN ('queued', 'running', 'waiting', 'paused', 'evaluating')
   OR (state IN ('completed', 'cancelled')
       AND updated_at >= now() - interval '24 hours')
"""
_TASK_COLUMNS = (
    "queued",
    "running",
    "waiting",
    "waiting_resource",
    "waiting_approval",
    "waiting_user",
    "paused",
    "evaluating",
    "completed_last_day",
    "cancelled_last_day",
)


# Failures (the ``fail`` command, the only way into ``failed``) of the last hour
# and day, and retries of the last hour, as events: a task that fails, is retried
# and fails again counts each failure, whatever its state now (Codex P1 on PR
# #170). The partial index ``ix_task_events_retry_fail_created_at`` (revision
# ``0066``) holds only those rows.
_TASK_EVENTS = """
SELECT
    count(*) FILTER (WHERE command = 'fail'
                     AND created_at >= now() - interval '1 hour'),
    count(*) FILTER (WHERE command = 'fail'),
    count(*) FILTER (WHERE command = 'retry'
                     AND created_at >= now() - interval '1 hour')
FROM task_events
WHERE command IN ('retry', 'fail') AND created_at >= now() - interval '24 hours'
"""
# Loops detected in the last hour (PAW-033): a task attempt and approach whose
# same failure signature is in the task's stored window (``LoopDetector`` keeps at
# most ``window_size`` rows per task) at least the loop policy's
# ``repeat_threshold`` times, the latest of them in the last hour: the condition
# of the detector's TRY_ALTERNATIVE / ESCALATE verdict when that failure was
# recorded, however long ago the earlier ones were (Codex P1 on PR #170). A loop
# never fails its task, so the failures do not show it. The tasks with a failure
# in the last hour come from ``ix_loop_failure_signatures_created_at``, their
# windows from the index on ``(task_id, seq)``.
_LOOPS = """
SELECT count(DISTINCT task_id) FROM (
    SELECT window_row.task_id FROM loop_failure_signatures AS window_row
    WHERE window_row.task_id IN (
        SELECT task_id FROM loop_failure_signatures
        WHERE created_at >= now() - interval '1 hour'
    )
    GROUP BY window_row.task_id, window_row.attempt, window_row.approach,
             window_row.signature
    HAVING count(*) >= %(threshold)s
       AND max(window_row.created_at) >= now() - interval '1 hour'
) AS repeated
"""


class TaskQueueSource:
    """Every user's tasks, counted (no id, title or project), with the retries
    and the detected loops of the last hour.

    A failure, a retry or a loop in the last hour is a warning (the Notification
    Policy's "単発の軽微なfailure、retry"); ``TASK_FAILURES_ERROR`` failures or
    ``TASK_LOOPS_ERROR`` loops an error."""

    component = Component.TASK_QUEUE
    max_age_seconds = limits.REPORT_MAX_AGE_SECONDS

    def __init__(
        self,
        database: Database,
        *,
        loop_threshold: int = DEFAULT_LOOP_POLICY.repeat_threshold,
    ) -> None:
        self._database = database
        self._loop_threshold = loop_threshold

    async def check(self) -> ComponentHealth:
        timeout = limits.CHECK_TIMEOUT_SECONDS
        (row,) = await self._database.fetch_abortable(_TASKS, timeout_seconds=timeout)
        ((failed, failed_day, retries),) = await self._database.fetch_abortable(
            _TASK_EVENTS, timeout_seconds=timeout
        )
        ((loops,),) = await self._database.fetch_abortable(
            _LOOPS, {"threshold": self._loop_threshold}, timeout_seconds=timeout
        )
        metrics = {
            name: int(value) for name, value in zip(_TASK_COLUMNS, row, strict=True)
        }
        metrics["failed_last_hour"] = int(failed)
        metrics["failed_last_day"] = int(failed_day)
        metrics["retries_last_hour"] = int(retries)
        metrics["loops_last_hour"] = int(loops)
        reasons = []
        severities = [Severity.INFO]
        for count, reason, error_at in (
            (int(failed), "task_failures", limits.TASK_FAILURES_ERROR),
            (int(loops), "loops_detected", limits.TASK_LOOPS_ERROR),
            (int(retries), "task_retries", None),
        ):
            if not count:
                continue
            reasons.append(reason)
            error = error_at is not None and count >= error_at
            severities.append(Severity.ERROR if error else Severity.WARNING)
        severity = worst(severities)
        return _health(self.component, severity, _status_of(severity), reasons, metrics)


_MEMORY_QUEUE = """
SELECT
    count(*) FILTER (WHERE status = 'queued'),
    count(*) FILTER (WHERE status = 'claimed'),
    extract(epoch FROM now() - min(enqueued_at) FILTER (WHERE status = 'queued')),
    count(*) FILTER (WHERE status = 'queued'
                     AND last_failure = 'worker_unavailable'),
    count(*) FILTER (WHERE status = 'claimed' AND lease_expires_at <= now())
FROM memory_consolidation_queue
WHERE status IN ('queued', 'claimed')
"""
_MEMORY_DEAD = """
SELECT count(*) FROM memory_consolidation_queue
WHERE status = 'dead' AND finished_at >= now() - interval '24 hours'
"""


class MemoryWorkerSource:
    """The Memory consolidation queue (PAW-041): pending and leased jobs, the
    oldest waiting one, the jobs deferred because the Memory Worker was not
    reachable (``last_failure = 'worker_unavailable'``), the leases that expired
    (a worker that took a job and went silent), and jobs that went to the dead
    letter in the last day. Each of the last three is a warning (Codex P1 on PR
    #170: a worker that is down defers its jobs and never dead-letters them). The
    Memory Worker model itself is part of ``compute``."""

    component = Component.MEMORY_WORKER
    max_age_seconds = 60.0

    def __init__(self, database: Database) -> None:
        self._database = database

    async def check(self) -> ComponentHealth:
        (queue,) = await self._database.fetch_abortable(
            _MEMORY_QUEUE, timeout_seconds=limits.CHECK_TIMEOUT_SECONDS
        )
        ((dead,),) = await self._database.fetch_abortable(
            _MEMORY_DEAD, timeout_seconds=limits.CHECK_TIMEOUT_SECONDS
        )
        metrics = {
            "pending": int(queue[0]),
            "claimed": int(queue[1]),
            "oldest_pending_seconds": _seconds(queue[2]),
            "waiting_for_worker": int(queue[3]),
            "expired_leases": int(queue[4]),
            "dead_last_day": int(dead),
        }
        reasons = [
            reason
            for reason, warning in (
                ("worker_unavailable", metrics["waiting_for_worker"] > 0),
                ("expired_leases", metrics["expired_leases"] > 0),
                ("dead_letters", int(dead) >= limits.MEMORY_DEAD_WARNING),
            )
            if warning
        ]
        severity = Severity.WARNING if reasons else Severity.INFO
        return _health(self.component, severity, _status_of(severity), reasons, metrics)


# -- Codex / Claude -------------------------------------------------------------------

_CONNECTIONS = """
SELECT kind, status, enabled, extract(epoch FROM now() - checked_at)
FROM shared_connections
"""
_IN_FLIGHT = """
SELECT kind, count(*), extract(epoch FROM now() - min(started_at))
FROM connection_usage
WHERE status = 'in_flight'
GROUP BY kind
"""


class ConnectionSource:
    """The shared Codex / Claude connections (PAW-030).

    Per kind: whether it is configured, enabled, its status (the last health
    check or a failed call), the age of that check and the calls in flight. An
    expired credential is an error, an unavailable one a warning, a disabled or
    missing one ``INFO`` (a deployment may use neither). ``available`` is what a
    general user may see (the UI's "Claude: Available / Unavailable")."""

    component = Component.CONNECTIONS
    max_age_seconds = limits.REPORT_MAX_AGE_SECONDS

    def __init__(self, database: Database) -> None:
        self._database = database

    async def check(self) -> ComponentHealth:
        rows = await self._database.fetch_abortable(
            _CONNECTIONS, timeout_seconds=limits.CHECK_TIMEOUT_SECONDS
        )
        in_flight = {
            kind: (int(count), age)
            for kind, count, age in await self._database.fetch_abortable(
                _IN_FLIGHT, timeout_seconds=limits.CHECK_TIMEOUT_SECONDS
            )
        }
        stored = {row[0]: row for row in rows}
        parts = []
        reasons: list[str] = []
        severities = [Severity.INFO]
        metrics: dict[str, MetricValue] = {}
        for kind in ConnectionKind:
            row = stored.get(kind.value)
            count, oldest = in_flight.get(kind.value, (0, None))
            part: dict[str, MetricValue] = {
                "kind": kind.value,
                "configured": row is not None,
                "enabled": bool(row[2]) if row is not None else False,
                "status": row[1] if row is not None else None,
                "checked_seconds_ago": _seconds(row[3]) if row is not None else None,
                "in_flight": count,
                "oldest_in_flight_seconds": _seconds(oldest),
            }
            available = (
                row is not None
                and bool(row[2])
                and row[1] == ConnectionStatus.CONNECTED.value
            )
            part["available"] = available
            parts.append(part)
            metrics[f"{kind.value}_available"] = 1 if available else 0
            metrics[f"{kind.value}_in_flight"] = count
            if row is None or not row[2]:
                continue
            if row[1] == ConnectionStatus.EXPIRED.value:
                severities.append(Severity.ERROR)
                reasons.append(f"expired:{kind.value}")
            elif row[1] == ConnectionStatus.UNAVAILABLE.value:
                severities.append(Severity.WARNING)
                reasons.append(f"unavailable:{kind.value}")
        severity = worst(severities)
        return _health(
            self.component,
            severity,
            _status_of(severity),
            reasons,
            metrics,
            tuple(parts),
        )


# -- the connection reaper ---------------------------------------------------------


class ReaperStatsProvider(Protocol):
    @property
    def stats(self) -> ReaperStats: ...


class ReaperSource:
    """The reaper of abandoned connection calls (issue #52's note from PR #106):
    what its cycles settled and whether they fail. Settling a row at all is a
    warning (a process died during a call); failing cycles a warning, and
    ``REAPER_FAILURES_ERROR`` in a row an error. ``not_configured`` when the
    application does not run it (no database, or switched off)."""

    component = Component.CONNECTION_REAPER
    max_age_seconds = 0.0

    def __init__(self, reaper: ReaperStatsProvider | None = None) -> None:
        self._reaper = reaper

    def attach(self, reaper: ReaperStatsProvider | None) -> None:
        self._reaper = reaper

    async def check(self) -> ComponentHealth:
        if self._reaper is None:
            return _health(self.component, Severity.INFO, Status.NOT_CONFIGURED)
        stats = self._reaper.stats
        now = datetime.now(UTC)
        metrics: dict[str, MetricValue] = {
            "cycles": stats.cycles,
            "last_reaped": stats.last_reaped,
            "total_reaped": stats.total_reaped,
            "consecutive_failures": stats.consecutive_failures,
            "total_failures": stats.total_failures,
            "last_error": stats.last_error,
            "last_cycle_seconds_ago": _age(now, stats.last_cycle_at),
            "last_success_seconds_ago": _age(now, stats.last_success_at),
        }
        reasons = []
        if stats.consecutive_failures >= limits.REAPER_FAILURES_ERROR:
            severity = Severity.ERROR
            reasons.append("reaper_failing")
        elif stats.consecutive_failures:
            severity = Severity.WARNING
            reasons.append("reaper_failed")
        elif stats.last_reaped:
            severity = Severity.WARNING
            reasons.append("abandoned_calls_settled")
        else:
            severity = Severity.INFO
        return _health(self.component, severity, _status_of(severity), reasons, metrics)


def _age(now: datetime, then: datetime | None) -> float | None:
    return None if then is None else round((now - then).total_seconds(), 3)


# -- the scheduled jobs -------------------------------------------------------------

_JOB = """
WITH last_ok AS (
    SELECT recorded_at FROM audit_events
    WHERE resource_kind = %(kind)s AND action = %(completed)s
    ORDER BY recorded_at DESC
    LIMIT 1
), last_run AS (
    SELECT action, recorded_at, reason FROM audit_events
    WHERE resource_kind = %(kind)s AND action IN (%(completed)s, %(failed)s)
    ORDER BY recorded_at DESC
    LIMIT 1
), failures AS (
    SELECT count(*) AS n FROM (
        SELECT 1 FROM audit_events
        WHERE resource_kind = %(kind)s AND action = %(failed)s
          AND recorded_at > COALESCE((SELECT recorded_at FROM last_ok), '-infinity')
        LIMIT %(cap)s
    ) AS counted
)
SELECT
    (SELECT action FROM last_run),
    extract(epoch FROM now() - (SELECT recorded_at FROM last_run)),
    (SELECT reason FROM last_run),
    extract(epoch FROM now() - (SELECT recorded_at FROM last_ok)),
    (SELECT n FROM failures)
"""


class ScheduledJob:
    """A job run by a timer that records each run as one ``audit_events`` row."""

    def __init__(
        self,
        component: Component,
        *,
        resource_kind: str,
        completed: str,
        failed: str,
        stale_after: tuple[int, int],
    ) -> None:
        self.component = component
        self.resource_kind = resource_kind
        self.completed = completed
        self.failed = failed
        self.stale_after = stale_after


class ScheduledJobSource:
    """The last run of a scheduled job and the age of its last success, by the
    database's clock (``recorded_at``).

    Never ran: ``INFO`` (``never_ran``: the deployment may not run it). A failed
    last run is a warning, ``JOB_FAILURES_ERROR`` failures in a row an error (the
    Notification Policy's "backup/recovery push継続失敗"). The last success
    older than the job's ``stale_after`` is a warning, then an error. The
    reason of a failed run (``<step>:<code>``, a closed code the job wrote) is
    shown; the one of a completed run (counts) is not."""

    max_age_seconds = limits.JOB_STATUS_MAX_AGE_SECONDS

    def __init__(self, database: Database, job: ScheduledJob) -> None:
        self._database = database
        self._job = job
        self.component = job.component

    async def check(self) -> ComponentHealth:
        job = self._job
        (
            (action, run_age, reason, ok_age, failures),
        ) = await self._database.fetch_abortable(
            _JOB,
            {
                "kind": job.resource_kind,
                "completed": job.completed,
                "failed": job.failed,
                "cap": limits.MAX_COUNTED_FAILURES,
            },
            timeout_seconds=limits.CHECK_TIMEOUT_SECONDS,
        )
        failures = int(failures)
        last_failed = action == job.failed
        metrics: dict[str, MetricValue] = {
            "last_run": (
                None if action is None else "failed" if last_failed else "completed"
            ),
            "last_run_seconds_ago": _seconds(run_age),
            "last_success_seconds_ago": _seconds(ok_age),
            "consecutive_failures": failures,
            "last_failure": reason if last_failed else None,
        }
        if action is None:
            return _health(job.component, Severity.INFO, Status.NEVER_RAN, [], metrics)
        severities = [Severity.INFO]
        reasons = []
        status = Status.OK
        if failures >= limits.JOB_FAILURES_ERROR:
            severities.append(Severity.ERROR)
            reasons.append("failing")
            status = Status.FAILING
        elif last_failed:
            severities.append(Severity.WARNING)
            reasons.append("last_run_failed")
            status = Status.DEGRADED
        warn_after, error_after = job.stale_after
        # Never succeeded: the failures in a row above say how bad it is.
        age = None if ok_age is None else float(ok_age)
        if age is None:
            pass
        elif age > error_after:
            severities.append(Severity.ERROR)
            reasons.append("no_recent_success")
            status = Status.STALE if status is Status.OK else status
        elif age > warn_after:
            severities.append(Severity.WARNING)
            reasons.append("no_recent_success")
            status = Status.STALE if status is Status.OK else status
        return _health(job.component, worst(severities), status, reasons, metrics)
