"""The usage report of the Usage screen (issue #187, Decision 0069, Proposed).

A read model over ``connection_usage`` (the calls through the shared Codex / Claude
connections): totals, tasks per calendar day and connection kind, per kind, per
usage category and, for the whole workspace, per user. It holds counts, closed
enums, ids and login names only: no prompt, answer, model name or text (Decision
0016, section 6).

What it counts (the quota metrics of Decision 0016, section 5)
-------------------------------------------------------------
* ``tasks``: distinct tasks with at least one call in the period (every status,
  ``in_flight`` too: the ``tasks`` metric of a quota). A task that used both kinds is
  one task in the total and one in each kind; a task with calls on two days is one
  on each day.
* ``tokens``: input plus output tokens the adapters reported (an unknown count is
  0), of the calls that STARTED in the period.

The local agent records no usage yet (issue #187, item 5): the report has no local
figure, and the HTTP layer says "not recorded" for it.

The periods (Decision 0069, point 2)
------------------------------------
Calendar days in the configured zone (``Asia/Tokyo``, Decision 0016 section 4):
``last14`` / ``last30`` are today and the 13 / 29 days before it, ``month`` is the
calendar month up to today. The PREVIOUS period, for the comparison, is the same
number of days just before (``last14`` / ``last30``), or the previous calendar
month up to the same day of the month (``month``: the 1st to the 17th of the last
month when today is the 17th; cut at the end of a shorter month).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from paw_backend.connections.domain import ConnectionKind, UsagePurpose
from paw_backend.connections.records import QuotaUsage


class UsageRange(StrEnum):
    """The periods of the Usage screen's menu (直近 14 日 / 直近 30 日 / 今月)."""

    LAST_14 = "last14"
    LAST_30 = "last30"
    MONTH = "month"


_DAYS = {UsageRange.LAST_14: 14, UsageRange.LAST_30: 30}


@dataclass(frozen=True, slots=True)
class ReportWindow:
    """The calendar days of a report and the instants (UTC) they start at.

    ``starts`` has one more item than ``days``: the end of the last day. The
    previous period is ``[previous_start, previous_end)``.
    """

    days: tuple[date, ...]
    starts: tuple[datetime, ...]
    previous_start: datetime
    previous_end: datetime

    @property
    def start(self) -> datetime:
        return self.starts[0]

    @property
    def end(self) -> datetime:
        return self.starts[-1]


def report_window(usage_range: UsageRange, now: datetime, zone: tzinfo) -> ReportWindow:
    """The days of ``usage_range`` that end with the day of ``now`` in ``zone``."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    today = now.astimezone(zone).date()
    if usage_range is UsageRange.MONTH:
        first = today.replace(day=1)
        previous_first = (first - timedelta(days=1)).replace(day=1)
        # The same days of the previous month, cut at its end.
        previous_last = min(previous_first + timedelta(days=today.day), first)
    else:
        length = _DAYS[usage_range]
        first = today - timedelta(days=length - 1)
        previous_first = first - timedelta(days=length)
        previous_last = first
    days = tuple(
        first + timedelta(days=offset) for offset in range((today - first).days + 1)
    )
    starts = tuple(
        _at_midnight(day, zone) for day in (*days, today + timedelta(days=1))
    )
    return ReportWindow(
        days,
        starts,
        _at_midnight(previous_first, zone),
        _at_midnight(previous_last, zone),
    )


def _at_midnight(day: date, zone: tzinfo) -> datetime:
    """Midnight at the start of ``day`` in ``zone``, as a UTC instant."""
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DailyTasks:
    """The tasks of one calendar day that used one connection kind."""

    day: date
    kind: ConnectionKind
    tasks: int


@dataclass(frozen=True, slots=True)
class KindTotal:
    kind: ConnectionKind
    tasks: int
    tokens: int


@dataclass(frozen=True, slots=True)
class PurposeTotal:
    purpose: UsagePurpose
    tasks: int
    tokens: int


@dataclass(frozen=True, slots=True)
class UserTotal:
    """One user of the workspace report: totals of the period and current quotas."""

    user_id: uuid.UUID
    login_name: str
    system_role: str
    status: str
    tasks: int
    tokens: int
    quotas: tuple[QuotaUsage, ...]


@dataclass(frozen=True, slots=True)
class UsageReport:
    """The usage of one user (``users`` is ``None``) or of the workspace.

    ``daily`` lists only the (day, kind) pairs with a task; ``days`` every day of
    the period, in order. ``quotas`` are the quotas of the user the report is of,
    or, for the workspace, of the user who asked.
    """

    range: UsageRange
    days: tuple[date, ...]
    window_start: datetime
    window_end: datetime
    tasks: int
    previous_tasks: int
    tokens: int
    daily: tuple[DailyTasks, ...]
    kinds: tuple[KindTotal, ...]
    purposes: tuple[PurposeTotal, ...]
    quotas: tuple[QuotaUsage, ...]
    users: tuple[UserTotal, ...] | None
