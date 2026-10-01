"""The System Health time series and events in PostgreSQL (PAW-066).

Every time is the database's clock (``now()``), never a host's: the buckets of
two Backend processes line up, and a sample is never placed in the future.

* :meth:`HealthStore.add_samples`: one raw row per metric in the bucket of the
  sampling interval (``date_bin``). A second process sampling in the same bucket
  inserts nothing (``ON CONFLICT DO NOTHING``): one sample per interval.
* :meth:`HealthStore.record_changes`: an event per component whose severity
  differs from its last event and that is not older than it, under a
  transaction-level advisory lock so that two processes do not both record the
  same change. A change seen before the last event (another process recorded
  the same outage and its end first) is dropped: the events of a component stay
  in the order they were seen, and a late replay opens no second outage. A
  time after the database's ``now()`` (a host clock ahead) is recorded as
  ``now()``, so that it cannot hold back the changes that follow.
* :meth:`HealthStore.roll_up`: moves the rows older than a tier's age into the
  next resolution (``DELETE ... RETURNING`` feeding ``INSERT ... ON CONFLICT DO
  UPDATE`` in one statement: a row is moved once and counted once, even when
  two processes roll up at the same time), then purges the hourly rows, the
  events and the agent incidents (Decision 0071) older than the retention
  period. At most ``MAX_ROLLUP_ROWS`` rows per statement.
* :meth:`HealthStore.series` and :meth:`HealthStore.events`: the reads of the API.

With ``notify=True`` (the application, issue #188, Decision 0070 Approved) a
recorded change is also a stored notification for the System Health audience
(``admin.system_health.view``: the Owner and the Admins), in the same transaction
(so once, whichever process records it): ``system_health.component_changed``
with the key ``system_health:<component>``, the severity of the change, and its
component, status, previous severity and reason codes. A component's first event
at ``info`` (a fresh start) is not one. ``on_notified`` is called after the
commit (the application publishes the ``notification.changed`` hint), and the
roll-up also purges the notifications past their retention.
"""

import asyncio
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.capabilities import Capability
from paw_backend.db import Database
from paw_backend.health.domain import ComponentHealth, Severity
from paw_backend.health.limits import (
    CHECK_TIMEOUT_SECONDS,
    HOURLY_RESOLUTION,
    MAX_ROLLUP_ROWS,
    METRIC_NAME_PATTERN,
    RAW_RESOLUTION,
    TIERS,
)
from paw_backend.health.models import MAX_REASONS_CHARS
from paw_backend.notifications import store as notifications
from paw_backend.notifications.domain import (
    PARAM_LIST_MAX_ITEMS,
    Category,
    NewNotification,
)

_METRIC_NAME = re.compile(METRIC_NAME_PATTERN)
# ``date_bin``'s origin: any fixed instant on a whole hour.
_EPOCH = "TIMESTAMPTZ '2000-01-01 00:00:00+00'"
# The advisory lock of ``record_changes`` (a fixed, arbitrary key).
_EVENTS_LOCK_KEY = 0x70617766_6865616C  # "pawfheal"
# A roll-up statement is repeated while it moves a full batch, at most this often.
_MAX_BATCHES = 20
WRITE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    bucket_start: datetime
    count: int
    mean: float
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class HealthEvent:
    id: int
    occurred_at: datetime
    component: str
    severity: str
    previous_severity: str | None
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RollupResult:
    moved: dict[int, int]  # rows moved out of each resolution
    purged_samples: int
    purged_events: int
    purged_incidents: int = 0
    purged_notifications: int = 0


_ADD_SAMPLES = f"""
INSERT INTO health_metric_samples
    (metric, resolution_seconds, bucket_start, sample_count,
     value_sum, value_min, value_max)
SELECT sample.metric, {RAW_RESOLUTION},
       date_bin(make_interval(secs => %(interval)s), now(), {_EPOCH}),
       1, sample.value, sample.value, sample.value
FROM unnest(%(metrics)s::text[], %(values)s::float8[]) AS sample(metric, value)
ON CONFLICT DO NOTHING
"""

_ROLL_UP = f"""
WITH doomed AS (
    SELECT metric, bucket_start FROM health_metric_samples
    WHERE resolution_seconds = :source
      AND bucket_start < date_bin(make_interval(secs => :target),
                                  now() - make_interval(secs => :age), {_EPOCH})
    ORDER BY bucket_start
    LIMIT :limit
), moved AS (
    DELETE FROM health_metric_samples AS sample
    USING doomed
    WHERE sample.resolution_seconds = :source
      AND sample.metric = doomed.metric
      AND sample.bucket_start = doomed.bucket_start
    RETURNING sample.metric, sample.bucket_start, sample.sample_count,
              sample.value_sum, sample.value_min, sample.value_max
), merged AS (
    INSERT INTO health_metric_samples AS target
        (metric, resolution_seconds, bucket_start, sample_count,
         value_sum, value_min, value_max)
    SELECT metric, :target,
           date_bin(make_interval(secs => :target), bucket_start, {_EPOCH}),
           sum(sample_count), sum(value_sum), min(value_min), max(value_max)
    FROM moved
    GROUP BY 1, 3
    ON CONFLICT (metric, resolution_seconds, bucket_start) DO UPDATE SET
        sample_count = target.sample_count + EXCLUDED.sample_count,
        value_sum = target.value_sum + EXCLUDED.value_sum,
        value_min = LEAST(target.value_min, EXCLUDED.value_min),
        value_max = GREATEST(target.value_max, EXCLUDED.value_max)
    RETURNING 1
)
SELECT count(*) FROM moved
"""

_PURGE_SAMPLES = f"""
WITH doomed AS (
    SELECT metric, bucket_start FROM health_metric_samples
    WHERE resolution_seconds = {HOURLY_RESOLUTION}
      AND bucket_start < now() - make_interval(days => :days)
    ORDER BY bucket_start
    LIMIT :limit
), gone AS (
    DELETE FROM health_metric_samples AS sample
    USING doomed
    WHERE sample.resolution_seconds = {HOURLY_RESOLUTION}
      AND sample.metric = doomed.metric
      AND sample.bucket_start = doomed.bucket_start
    RETURNING 1
)
SELECT count(*) FROM gone
"""

_PURGE_EVENTS = """
WITH doomed AS (
    SELECT id FROM health_events
    WHERE occurred_at < now() - make_interval(days => :days)
    ORDER BY occurred_at
    LIMIT :limit
), gone AS (
    DELETE FROM health_events WHERE id IN (SELECT id FROM doomed) RETURNING 1
)
SELECT count(*) FROM gone
"""

# The agent incidents (Decision 0071, Proposed) are kept as long as the events.
_PURGE_INCIDENTS = """
WITH doomed AS (
    SELECT id FROM agent_incidents
    WHERE occurred_at < now() - make_interval(days => :days)
    ORDER BY occurred_at
    LIMIT :limit
), gone AS (
    DELETE FROM agent_incidents WHERE id IN (SELECT id FROM doomed) RETURNING 1
)
SELECT count(*) FROM gone
"""

_RECORD_CHANGE = """
INSERT INTO health_events
    (occurred_at, component, severity, previous_severity, status, reasons)
SELECT change.at, :component, :severity, last.severity, :status, :reasons
FROM (SELECT LEAST(COALESCE(CAST(:occurred_at AS timestamptz), now()), now()) AS at)
    AS change
LEFT JOIN LATERAL (
    SELECT severity, occurred_at FROM health_events
    WHERE component = :component
    ORDER BY id DESC
    LIMIT 1
) AS last ON true
WHERE last.severity IS DISTINCT FROM :severity
  AND (last.occurred_at IS NULL OR change.at >= last.occurred_at)
RETURNING id, previous_severity
"""

_SERIES = f"""
SELECT date_bin(make_interval(secs => %(step)s), bucket_start, {_EPOCH}) AS bucket,
       sum(sample_count), sum(value_sum), min(value_min), max(value_max)
FROM health_metric_samples
WHERE metric = %(metric)s AND bucket_start >= %(since)s AND bucket_start < %(until)s
GROUP BY bucket
ORDER BY bucket
LIMIT %(limit)s
"""

_EVENTS = """
SELECT id, occurred_at, component, severity, previous_severity, status, reasons
FROM health_events
WHERE occurred_at >= %(since)s
ORDER BY id DESC
LIMIT %(limit)s
"""


# Who receives the System Health notifications (Decision 0059 3: the detail is
# the Owner's and the Admins').
NOTIFICATION_AUDIENCE = Capability.ADMIN_SYSTEM_HEALTH_VIEW
NOTIFICATION_KIND = "system_health.component_changed"


def notification_key(component: str) -> str:
    return f"system_health:{component}"


class HealthStore:
    def __init__(
        self,
        database: Database,
        *,
        notify: bool = False,
        on_notified: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._notify = bool(notify)
        self._on_notified = on_notified

    async def add_samples(
        self, values: Mapping[str, float], *, interval_seconds: int
    ) -> None:
        """One raw sample of each metric, in the current bucket of the interval.
        Values that are not finite, and names the schema would refuse (which
        would fail the whole statement), are left out."""
        kept = {
            name: float(value)
            for name, value in values.items()
            if isinstance(name, str)
            and _METRIC_NAME.fullmatch(name)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
        }
        if not kept:
            return
        await self._database.execute_abortable(
            _ADD_SAMPLES,
            {
                "interval": interval_seconds,
                "metrics": list(kept),
                "values": list(kept.values()),
            },
            timeout_seconds=CHECK_TIMEOUT_SECONDS,
        )

    async def record_changes(
        self,
        components: Sequence[ComponentHealth],
        *,
        occurred_at: datetime | None = None,
    ) -> int:
        """An event for each component whose severity is not the one of its last
        event (or that has none) and that is not older than that event; return
        how many were recorded. ``occurred_at``
        is when the report was taken (a report recorded late, after PostgreSQL
        came back); ``None`` is the database's ``now()``."""
        if not components:
            return 0

        async def work(session: AsyncSession) -> int:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _EVENTS_LOCK_KEY}
            )
            recorded = notified = 0
            for health in components:
                reasons = _reasons(health.reasons)
                rows = (
                    await session.execute(
                        text(_RECORD_CHANGE),
                        {
                            "component": health.component.value,
                            "severity": health.severity.value,
                            "status": health.status.value,
                            "reasons": reasons,
                            "occurred_at": occurred_at,
                        },
                    )
                ).all()
                recorded += len(rows)
                for row in rows:
                    if self._notify and (
                        row.previous_severity is not None
                        or health.severity is not Severity.INFO
                    ):
                        await notifications.add_in(
                            session, _notification(health, reasons, row)
                        )
                        notified += 1
            return recorded, notified

        async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
            recorded, notified = await self._database.run_abortable(work)
        if notified and self._on_notified is not None:
            self._on_notified()
        return recorded

    async def roll_up(self, *, retention_days: int) -> RollupResult:
        """Move old rows to the next resolution; purge what is past retention."""
        steps = [
            (source, age, TIERS[index + 1][0] if index + 1 < len(TIERS) else None)
            for index, (source, age) in enumerate(TIERS)
        ]
        moved: dict[int, int] = {}
        for source, age, target in steps:
            target = HOURLY_RESOLUTION if target is None else target
            moved[source] = await self._repeat(
                _ROLL_UP,
                {"source": source, "target": target, "age": age},
            )
        purged_samples = await self._repeat(_PURGE_SAMPLES, {"days": retention_days})
        purged_events = await self._repeat(_PURGE_EVENTS, {"days": retention_days})
        purged_incidents = await self._repeat(
            _PURGE_INCIDENTS, {"days": retention_days}
        )
        purged_notifications = 0
        if self._notify:
            for _ in range(_MAX_BATCHES):

                async def purge(session: AsyncSession) -> int:
                    return await notifications.purge_in(session)

                async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                    count = await self._database.run_abortable(purge)
                purged_notifications += count
                if count < notifications.MAX_PURGE_ROWS:
                    break
        return RollupResult(
            moved,
            purged_samples,
            purged_events,
            purged_incidents=purged_incidents,
            purged_notifications=purged_notifications,
        )

    async def _repeat(self, sql: str, params: dict[str, int]) -> int:
        total = 0
        for _ in range(_MAX_BATCHES):

            async def work(session: AsyncSession) -> int:
                result = await session.execute(
                    text(sql), {**params, "limit": MAX_ROLLUP_ROWS}
                )
                return int(result.scalar_one())

            async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                count = await self._database.run_abortable(work)
            total += count
            if count < MAX_ROLLUP_ROWS:
                break
        return total

    async def series(
        self,
        metric: str,
        *,
        since: datetime,
        until: datetime,
        step_seconds: int,
        limit: int,
    ) -> tuple[SeriesPoint, ...]:
        rows = await self._database.fetch_abortable(
            _SERIES,
            {
                "metric": metric,
                "since": since,
                "until": until,
                "step": step_seconds,
                "limit": limit,
            },
            timeout_seconds=CHECK_TIMEOUT_SECONDS,
        )
        return tuple(
            SeriesPoint(
                bucket_start=bucket,
                count=int(count),
                mean=float(total) / int(count),
                minimum=float(minimum),
                maximum=float(maximum),
            )
            for bucket, count, total, minimum, maximum in rows
        )

    async def events(self, *, since: datetime, limit: int) -> tuple[HealthEvent, ...]:
        rows = await self._database.fetch_abortable(
            _EVENTS,
            {"since": since, "limit": limit},
            timeout_seconds=CHECK_TIMEOUT_SECONDS,
        )
        return tuple(
            HealthEvent(
                id=int(row[0]),
                occurred_at=row[1],
                component=row[2],
                severity=row[3],
                previous_severity=row[4],
                status=row[5],
                reasons=tuple(code for code in row[6].split(",") if code),
            )
            for row in rows
        )


def _reasons(reasons: Sequence[str]) -> str:
    joined = ""
    for code in reasons:
        candidate = code if not joined else f"{joined},{code}"
        if len(candidate) > MAX_REASONS_CHARS:
            break
        joined = candidate
    return joined


__all__ = [
    "HealthEvent",
    "HealthStore",
    "RollupResult",
    "SeriesPoint",
]


def _notification(health: ComponentHealth, reasons: str, row) -> NewNotification:
    component = health.component.value
    return NewNotification(
        key=notification_key(component),
        kind=NOTIFICATION_KIND,
        severity=health.severity.value,
        category=Category.SYSTEM,
        audience_capability=NOTIFICATION_AUDIENCE,
        params={
            "component": component,
            "status": health.status.value,
            "previous_severity": row.previous_severity,
            "reasons": [r for r in reasons.split(",") if r][:PARAM_LIST_MAX_ITEMS],
        },
    )
