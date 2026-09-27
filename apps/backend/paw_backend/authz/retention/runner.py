"""One scheduled run of ``audit_events`` retention (Issue #117, Decision 0031).

``AuditRetentionService.run_maintenance`` (Decision 0027) does the three steps
and nothing else: it raises on the first failure and records nothing about the
run as a whole. A scheduler needs more than that, because a run that silently
stops happening (or keeps failing) leaves ``audit_events`` without a partition
for the next month, and from then on **every** audited action fails. So a
scheduled run, here:

1. takes the maintenance lock (``maintenance_lock``) — a second run at the same
   time is refused before anything is done;
2. runs ``ensure_partitions``, ``archive_due_partitions`` and
   ``purge_due_partitions`` one by one, stopping at the first that raises
   (what the earlier steps did stands: each is its own transaction);
3. checks that the live partitions cover the next ``required_months_ahead``
   months (``rules.first_uncovered_moment``) — a gap is a failure even though no
   step raised;
4. records the outcome as one ``audit_events`` row
   (``audit.retention.maintenance_completed`` / ``maintenance_failed``), in a
   transaction of its own, so a failure is recorded although the failed step
   rolled back. Only the step and the error's **type** are recorded, never its
   message (it may carry a connection detail).

The caller (``paw_backend.cli.retention``) turns ``MaintenanceRunResult.ok``
into the process exit code, which is what the scheduler (the systemd timer of
``deploy/systemd``, or cron) watches. Nothing here knows about a scheduler;
tests drive it with a fake service.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from paw_backend.authz.retention.audit import (
    REASON_MAX_LENGTH,
    RetentionAction,
    RetentionActor,
)
from paw_backend.authz.retention.records import (
    MaintenanceReport,
    PartitionWindow,
    RetentionPolicy,
    default_policy,
)
from paw_backend.authz.retention.rules import first_uncovered_moment

# The partitions must reach at least to the end of next month after every run:
# with daily runs and Decision 0027's ``horizon_months = 3``, a failure is
# reported about three months before an INSERT would actually fail.
DEFAULT_REQUIRED_MONTHS_AHEAD = 1

COVERAGE_GAP = "coverage_gap"


class MaintenanceStep(StrEnum):
    """The step a run failed at (``MaintenanceRunResult.failed_step``)."""

    ENSURE_PARTITIONS = "ensure_partitions"
    ARCHIVE = "archive_due_partitions"
    PURGE = "purge_due_partitions"
    VERIFY_COVERAGE = "verify_coverage"


class RetentionMaintenance(Protocol):
    """What the runner needs of ``AuditRetentionService`` (a fake in tests)."""

    def _now(self) -> datetime: ...

    def maintenance_lock(self): ...

    async def ensure_partitions(
        self, policy: RetentionPolicy | None = None, *, actor: RetentionActor | None
    ) -> list[PartitionWindow]: ...

    async def archive_due_partitions(
        self, policy: RetentionPolicy | None = None, *, actor: RetentionActor | None
    ) -> list[PartitionWindow]: ...

    async def purge_due_partitions(
        self, policy: RetentionPolicy | None = None, *, actor: RetentionActor | None
    ) -> list[PartitionWindow]: ...

    async def existing_partitions(self) -> list[PartitionWindow]: ...

    async def record_maintenance_outcome(
        self,
        action: RetentionAction,
        reason: str,
        *,
        actor: RetentionActor | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MaintenanceRunResult:
    """What one ``run_scheduled_maintenance`` call did and whether it is healthy.

    - ``report``: what the steps that ran did (a failed step contributes ``()``).
    - ``failed_step`` / ``error_type``: ``None`` on success; otherwise the step
      and the exception's class name (``"coverage_gap"`` for
      ``VERIFY_COVERAGE``).
    - ``uncovered_from``: the first moment without a live partition, when the
      coverage check failed.
    - ``audited``: whether the outcome row was written. A run is ``ok`` only if
      every step succeeded, the coverage holds **and** that was recorded
      (Decision 0027 section 5: the run and its result are in the audit trail).
    """

    report: MaintenanceReport
    failed_step: MaintenanceStep | None
    error_type: str | None
    uncovered_from: datetime | None
    audited: bool

    @property
    def ok(self) -> bool:
        return self.failed_step is None and self.audited


async def run_scheduled_maintenance(
    service: RetentionMaintenance,
    policy: RetentionPolicy | None = None,
    *,
    actor: RetentionActor | None = None,
    required_months_ahead: int = DEFAULT_REQUIRED_MONTHS_AHEAD,
) -> MaintenanceRunResult:
    """Run every step under the lock, check the coverage, record the outcome.

    Raises ``MaintenanceAlreadyRunningError`` (nothing done, nothing recorded)
    when another run holds the lock, and whatever taking the lock raises (the
    database is unreachable: nothing could be recorded either). Every other
    failure is returned, not raised.
    """
    policy = policy or default_policy()
    async with service.maintenance_lock():
        done: dict[MaintenanceStep, tuple[PartitionWindow, ...]] = {}
        failed_step: MaintenanceStep | None = None
        error_type: str | None = None
        uncovered_from: datetime | None = None
        steps = (
            (MaintenanceStep.ENSURE_PARTITIONS, service.ensure_partitions),
            (MaintenanceStep.ARCHIVE, service.archive_due_partitions),
            (MaintenanceStep.PURGE, service.purge_due_partitions),
        )
        for step, method in steps:
            try:
                done[step] = tuple(await method(policy, actor=actor))
            except Exception as error:
                failed_step, error_type = step, type(error).__name__
                break
        if failed_step is None:
            try:
                existing = await service.existing_partitions()
                uncovered_from = first_uncovered_moment(
                    service._now(), existing, required_months_ahead
                )
                if uncovered_from is not None:
                    failed_step = MaintenanceStep.VERIFY_COVERAGE
                    error_type = COVERAGE_GAP
            except Exception as error:
                failed_step = MaintenanceStep.VERIFY_COVERAGE
                error_type = type(error).__name__
        report = MaintenanceReport(
            created=done.get(MaintenanceStep.ENSURE_PARTITIONS, ()),
            archived=done.get(MaintenanceStep.ARCHIVE, ()),
            purged=done.get(MaintenanceStep.PURGE, ()),
        )
        if failed_step is None:
            action = RetentionAction.MAINTENANCE_COMPLETED
            reason = (
                f"created={len(report.created)} archived={len(report.archived)} "
                f"purged={len(report.purged)}"
            )
        else:
            action = RetentionAction.MAINTENANCE_FAILED
            reason = f"{failed_step.value}:{error_type}"[:REASON_MAX_LENGTH]
        try:
            await service.record_maintenance_outcome(action, reason, actor=actor)
            audited = True
        except Exception:
            audited = False
        return MaintenanceRunResult(
            report=report,
            failed_step=failed_step,
            error_type=error_type,
            uncovered_from=uncovered_from,
            audited=audited,
        )


__all__ = [
    "COVERAGE_GAP",
    "DEFAULT_REQUIRED_MONTHS_AHEAD",
    "MaintenanceRunResult",
    "MaintenanceStep",
    "RetentionMaintenance",
    "run_scheduled_maintenance",
]
