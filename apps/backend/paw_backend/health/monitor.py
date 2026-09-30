"""The System Health monitor (PAW-066): runs the sources, keeps the time series.

:meth:`HealthMonitor.report` reads every source whose last answer is older than
its ``max_age_seconds`` (all of them at once, each within
``CHECK_TIMEOUT_SECONDS``) and returns the report. Concurrent callers share one
refresh. A source that raises or times out is ``check_failed`` (``WARNING``),
named by its component only: nothing the error says is kept or logged. A source
with an ``on_timeout()`` answers a timeout with it instead (the database:
``CRITICAL`` / ``unavailable``, whatever ``PAW_DATABASE_TIMEOUT_SECONDS`` is).

:meth:`HealthMonitor.run` is the sampling loop of the application (when
a database is configured; ``PAW_HEALTH_SAMPLE_INTERVAL_SECONDS``, 10 to 30):
every interval (the first one interval after the start) it refreshes the
report, stores one sample of every numeric metric, records the components whose
severity changed, and every ``ROLLUP_EVERY_CYCLES`` cycles rolls the old samples
up and purges what is past retention (``store.py``). Errors are logged by type
and the loop goes on; ``stop`` (or a cancellation) ends it.
"""

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from paw_backend.health.domain import (
    ComponentHealth,
    HealthReport,
    Severity,
    Status,
    numeric_metrics,
)
from paw_backend.health.limits import (
    CHECK_TIMEOUT_SECONDS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    REPORT_MAX_AGE_SECONDS,
    ROLLUP_EVERY_CYCLES,
)
from paw_backend.health.sources import HealthSource
from paw_backend.health.store import HealthStore

# How many reports with changes wait for PostgreSQL at most (``run_cycle``).
MAX_UNRECORDED_REPORTS = 100

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(
        self,
        sources: Iterable[HealthSource],
        *,
        store: HealthStore | None = None,
        sample_interval_seconds: int = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        check_timeout_seconds: float = CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._sources = tuple(sources)
        components = [source.component for source in self._sources]
        if len(set(components)) != len(components):
            raise ValueError("one source per component")
        self._store = store
        self._interval = sample_interval_seconds
        self._retention_days = retention_days
        self._timeout = check_timeout_seconds
        # component -> (answer, monotonic time it was taken)
        self._answers: dict[object, tuple[ComponentHealth, float]] = {}
        self._report: HealthReport | None = None
        self._reported_at: float | None = None
        self._lock = asyncio.Lock()
        # Reports whose severity changes are not in ``health_events`` yet.
        self._unrecorded: list[HealthReport] = []
        self._stopping = asyncio.Event()

    @property
    def sources(self) -> tuple[HealthSource, ...]:
        return self._sources

    async def report(
        self, *, max_age_seconds: float = REPORT_MAX_AGE_SECONDS
    ) -> HealthReport:
        """The report, no older than ``max_age_seconds`` (0: read every source
        whose own answer is stale now)."""
        if self._fresh(max_age_seconds):
            return self._report  # type: ignore[return-value]
        async with self._lock:
            if self._fresh(max_age_seconds):  # another caller refreshed it
                return self._report  # type: ignore[return-value]
            return await self._refresh()

    def _fresh(self, max_age_seconds: float) -> bool:
        return (
            self._report is not None
            and self._reported_at is not None
            and time.monotonic() - self._reported_at < max_age_seconds
        )

    async def _refresh(self) -> HealthReport:
        now = time.monotonic()
        due = [
            source
            for source in self._sources
            if (answer := self._answers.get(source.component)) is None
            or now - answer[1] >= source.max_age_seconds
        ]
        results = await asyncio.gather(*(self._check(source) for source in due))
        taken = time.monotonic()
        for source, health in zip(due, results, strict=True):
            self._answers[source.component] = (health, taken)
        report = HealthReport(
            checked_at=datetime.now(UTC),
            components=tuple(
                self._answers[source.component][0] for source in self._sources
            ),
        )
        self._report = report
        self._reported_at = taken
        return report

    async def _check(self, source: HealthSource) -> ComponentHealth:
        try:
            async with asyncio.timeout(self._timeout):
                return await source.check()
        except Exception as error:  # a broken check must not break the report
            timed_out = isinstance(error, TimeoutError)
            logger.warning(
                "Health check of %s failed (%s)",
                source.component.value,
                type(error).__name__,
            )
            # A source whose silence is itself the finding (PostgreSQL not
            # answering in time is down) says what a timeout means.
            on_timeout = getattr(source, "on_timeout", None)
            if timed_out and on_timeout is not None:
                return on_timeout()
            reason = "check_timeout" if timed_out else "check_error"
            return ComponentHealth(
                source.component, Severity.WARNING, Status.CHECK_FAILED, (reason,)
            )

    # -- the sampling loop ------------------------------------------------------

    def stop(self) -> None:
        self._stopping.set()

    async def run_cycle(self, cycle: int) -> None:
        """Refresh, record the changes and store the samples; roll up now and then.

        The changes are recorded first, and every report whose changes could not
        be recorded (PostgreSQL did not answer) is kept and recorded, in order and
        with the time it was taken, at the next cycle that can: an outage of
        PostgreSQL itself is then in ``health_events`` once it is back."""
        if self._store is None:
            raise RuntimeError("no store to sample into")
        async with self._lock:
            report = await self._refresh()
        self._keep_unrecorded(report)
        while self._unrecorded:
            pending = self._unrecorded[0]
            await self._store.record_changes(
                pending.components, occurred_at=pending.checked_at
            )
            self._unrecorded.pop(0)
        values: dict[str, float] = {}
        for health in report.components:
            values.update(numeric_metrics(health))
        await self._store.add_samples(values, interval_seconds=self._interval)
        if cycle % ROLLUP_EVERY_CYCLES == 0:
            await self._store.roll_up(retention_days=self._retention_days)

    def _keep_unrecorded(self, report: HealthReport) -> None:
        """Queue ``report`` for recording, unless no severity changed since the
        last queued one. At most ``MAX_UNRECORDED_REPORTS`` wait: the first one
        (the start of an outage) is kept, the oldest after it are dropped."""
        if self._unrecorded and _severities(self._unrecorded[-1]) == _severities(
            report
        ):
            return
        self._unrecorded.append(report)
        if len(self._unrecorded) > MAX_UNRECORDED_REPORTS:
            del self._unrecorded[1]

    async def run(self) -> None:
        # The first cycle comes one interval after the start: nothing is urgent
        # then, and the start does not open connections of its own.
        await self._sleep(self._interval)
        cycle = 0
        while not self._stopping.is_set():
            try:
                await self.run_cycle(cycle)
            except Exception as error:  # a supervisor: one bad cycle must not end it
                logger.warning(
                    "Sampling System Health failed (%s)", type(error).__name__
                )
            cycle += 1
            await self._sleep(self._interval)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass


def _severities(report: HealthReport) -> tuple[tuple[object, Severity], ...]:
    return tuple((h.component, h.severity) for h in report.components)
