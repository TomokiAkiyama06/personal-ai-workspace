"""Retention / partitioning rules of ``audit_events``: pure functions, no database.

Decision 0027 (Issue #86, approved; see ``docs/decisions/0027-audit-retention-and-
partitioning.md``) partitions ``audit_events`` by calendar month on ``recorded_at``
(the database clock; append-only and forced by trigger, so it only ever moves
forward) and gives old partitions a life cycle:

    LIVE (attached to ``audit_events``)
      -> ARCHIVED (detached, attached to ``audit_events_archive`` instead)
        -> PURGED (detached and dropped; only if ``RetentionPolicy.purge_after_days``
           is set)

The rows in every partition, at every stage, stay append-only: Migration 0086
puts the same reject-update/delete/truncate triggers on both parents, and
PostgreSQL clones the row-level ones onto every partition that is ever created
under or attached to them (``service.py`` adds the truncate one explicitly,
since PostgreSQL does not clone statement-level triggers — see its docstring).

A rule function here

* never touches a database, the clock or a file;
* never changes its arguments (every value here is frozen);
* raises ``TypeError`` for a non-``int``/naive-``datetime`` argument, before
  anything else is looked at (mirrors ``memory/shared/lifecycle.py``);
* takes only already-validated values (a ``RetentionPolicy`` cannot be built
  invalid — see ``records.py``).

``service.py`` calls these with what it read from the ``audit_retention_partitions``
bookkeeping table and does the SQL each answer implies.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from paw_backend.authz.retention.records import (
    PartitionStatus,
    PartitionWindow,
    RetentionPolicy,
)

# The two tables partitions live under, and the one row-per-partition table that
# tracks where each one currently is. Not partitions themselves.
LIVE_PARENT_TABLE = "audit_events"
ARCHIVE_PARENT_TABLE = "audit_events_archive"
BOOKKEEPING_TABLE = "audit_retention_partitions"

# The partition every row recorded before Migration 0086 lives in
# (``FOR VALUES FROM (MINVALUE) TO (<cutover>)``): it has no calendar-month name
# and no concrete lower bound (see ``PartitionWindow.lower``).
LEGACY_PARTITION_NAME = "audit_events_p_legacy"


def _require_aware(moment: datetime, field: str = "moment") -> None:
    if not isinstance(moment, datetime):
        raise TypeError(f"{field} must be a datetime, not {type(moment).__name__}")
    if moment.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware, not naive")


def month_start(moment: datetime) -> datetime:
    """The start (00:00:00 UTC on the 1st) of the calendar month containing ``moment``.

    ``moment`` is converted to UTC first, so a month boundary is always a UTC one
    (matching ``recorded_at``, a ``timestamptz`` the database always writes in UTC
    from ``now()``). Every other function in this module measures months this way.
    """
    _require_aware(moment)
    at_utc = moment.astimezone(UTC)
    return datetime(at_utc.year, at_utc.month, 1, tzinfo=UTC)


def next_month_start(moment: datetime) -> datetime:
    """``month_start`` of the calendar month after the one containing ``moment``."""
    start = month_start(moment)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def partition_name(period_start: datetime) -> str:
    """The partition name of the calendar month starting at ``period_start``.

    ``audit_events_p<YYYY>_<MM>``, e.g. ``audit_events_p2026_09``. Raises
    ``ValueError`` if ``period_start`` is not exactly a month start (the caller
    always has one: ``month_start`` or the previous call's ``next_month_start``).
    """
    _require_aware(period_start, "period_start")
    if period_start != month_start(period_start):
        raise ValueError(f"period_start must be a month start, got {period_start!r}")
    at_utc = period_start.astimezone(UTC)
    return f"audit_events_p{at_utc.year:04d}_{at_utc.month:02d}"


def plan_missing_partitions(
    now: datetime, existing: Sequence[PartitionWindow], policy: RetentionPolicy
) -> list[PartitionWindow]:
    """The calendar-month partitions that must exist but do not, oldest first.

    "Must exist": the month containing ``now``, and ``policy.horizon_months``
    months after it — so there is always a partition ready for at least that far
    in the future, and an insert at any point before then never lacks a
    partition to land in. Idempotent: a name already present in ``existing``
    (matched by ``PartitionWindow.name`` alone, whatever its status) is never
    planned again, so calling this repeatedly as time passes only ever returns
    the ones truly still missing.

    Example: ``now`` is 2026-09-26, ``policy.horizon_months`` is 3, and
    ``existing`` already has ``audit_events_p2026_09``. This returns the three
    partitions for October, November and December 2026 (in that order), each
    ``PartitionWindow(status=LIVE)`` with its calendar-month bound.
    """
    _require_aware(now)
    already = {window.name for window in existing}
    planned: list[PartitionWindow] = []
    period_start = month_start(now)
    for _ in range(policy.horizon_months + 1):
        period_end = next_month_start(period_start)
        name = partition_name(period_start)
        if name not in already:
            planned.append(
                PartitionWindow(name, period_start, period_end, PartitionStatus.LIVE)
            )
        period_start = period_end
    return planned


def partitions_due_for_archive(
    now: datetime, existing: Sequence[PartitionWindow], policy: RetentionPolicy
) -> list[PartitionWindow]:
    """The ``LIVE`` partitions whose window ended at least ``archive_after_days`` ago.

    Oldest (smallest ``upper``) first. A partition whose window has not ended yet
    (including the one ``now`` currently falls in) never qualifies, whatever
    ``archive_after_days`` is: its ``upper`` is in the future, always greater than
    ``now - archive_after_days`` days.
    """
    _require_aware(now)
    threshold = now - timedelta(days=policy.archive_after_days)
    due = [
        window
        for window in existing
        if window.status == PartitionStatus.LIVE and window.upper <= threshold
    ]
    due.sort(key=lambda window: window.upper)
    return due


def partitions_due_for_purge(
    now: datetime, existing: Sequence[PartitionWindow], policy: RetentionPolicy
) -> list[PartitionWindow]:
    """The ``ARCHIVED`` partitions old enough to drop, oldest first.

    ``[]`` whenever ``policy.purge_after_days`` is ``None`` (the default
    recommendation of Decision 0027: never purge automatically). When it is set,
    a partition qualifies once its window ended at least that many days ago,
    the same way ``partitions_due_for_archive`` measures archiving.
    """
    if policy.purge_after_days is None:
        return []
    _require_aware(now)
    threshold = now - timedelta(days=policy.purge_after_days)
    due = [
        window
        for window in existing
        if window.status == PartitionStatus.ARCHIVED and window.upper <= threshold
    ]
    due.sort(key=lambda window: window.upper)
    return due
