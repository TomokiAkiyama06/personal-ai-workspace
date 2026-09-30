"""The vocabulary of System Health (PAW-066).

``REQUIREMENTS.md`` ("Observability / System Health baseline", FIXED) lists what
is watched and ties the severity to the Notification Policy (``INFO`` /
``WARNING`` / ``ERROR`` / ``CRITICAL``); this module turns both into closed enums.
The choices the requirements leave open (which checks, their thresholds, who may
read what) are ``docs/decisions/0059-system-health-observability.md``
(Proposed).

A component's report holds codes and numbers only: never a message from a
dependency, a path, a URL, a user's text or a process id (``docs/OBSERVABILITY.md``:
"Metrics are operational data, not user private-content inspection").
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType


class Severity(StrEnum):
    """The Notification Policy's levels (``docs/NOTIFICATION_POLICY.md``)."""

    INFO = "info"  # normal
    WARNING = "warning"  # a single minor failure, a retry, resource pressure
    ERROR = "error"  # a continuing failure, a job that stopped
    CRITICAL = "critical"  # PostgreSQL down, a security-sensitive incident

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}


def worst(severities: Iterable[Severity]) -> Severity:
    """The highest of ``severities`` (``INFO`` for none)."""
    result = Severity.INFO
    for severity in severities:
        if severity.rank > result.rank:
            result = severity
    return result


class Component(StrEnum):
    """What is watched. Declaration order is the order of a report."""

    DATABASE = "database"  # PostgreSQL
    COMPUTE = "compute"  # GPU / VRAM / model residency (the Compute Scheduler)
    TASK_QUEUE = "task_queue"  # queued / running / waiting / failed tasks
    MEMORY_WORKER = "memory_worker"  # the Memory consolidation queue
    CONNECTIONS = "connections"  # the shared Codex / Claude connections
    CONNECTION_REAPER = "connection_reaper"  # abandoned connection calls
    RECOVERY_BACKUP = "recovery_backup"  # Recovery Repository commit / push
    MEMORY_PROJECTION = "memory_projection"  # Memory Markdown Projection
    AUDIT_RETENTION = "audit_retention"  # the audit partitions' maintenance


class Status(StrEnum):
    """A component's state, as a closed code (the severity says how bad it is)."""

    OK = "ok"
    DEGRADED = "degraded"  # works, with pressure or a single failure
    FAILING = "failing"  # does not work, or keeps failing
    STALE = "stale"  # a scheduled job has not succeeded for too long
    UNAVAILABLE = "unavailable"  # the dependency cannot be reached
    NOT_CONFIGURED = "not_configured"  # nothing to watch in this deployment
    NEVER_RAN = "never_ran"  # a scheduled job has no recorded run yet
    CHECK_FAILED = "check_failed"  # the check itself failed or timed out


# A metric's value: a number, a flag, a closed code or unknown. Only numbers
# (``int`` / ``float``, not ``bool``) are kept as time series.
MetricValue = int | float | bool | str | None


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    component: Component
    severity: Severity
    status: Status
    # Closed codes that say why (``probe_unavailable``, ``expired:claude``, ...).
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, MetricValue] = field(default_factory=dict)
    # Parts of the component, each a flat mapping (the models of the Compute
    # Scheduler, the two shared connections). Codes and numbers only.
    parts: tuple[Mapping[str, MetricValue], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(
            self, "parts", tuple(MappingProxyType(dict(p)) for p in self.parts)
        )


@dataclass(frozen=True, slots=True)
class HealthReport:
    checked_at: datetime
    components: tuple[ComponentHealth, ...]

    @property
    def severity(self) -> Severity:
        return worst(c.severity for c in self.components)

    def component(self, component: Component) -> ComponentHealth | None:
        for health in self.components:
            if health.component is component:
                return health
        return None


def numeric_metrics(health: ComponentHealth) -> dict[str, float]:
    """The metrics of ``health`` kept as time series: ``<component>.<name>``
    for every number, plus ``<component>.severity`` (its rank, 0 to 3)."""
    series = {f"{health.component.value}.severity": float(health.severity.rank)}
    for name, value in health.metrics.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        series[f"{health.component.value}.{name}"] = float(value)
    return series
