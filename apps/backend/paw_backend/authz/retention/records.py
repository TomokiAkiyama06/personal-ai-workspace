"""Value objects of ``audit_events`` retention / partitioning (Decision 0027, #86).

Frozen, no database, no clock: what a ``PartitionWindow`` is and what a
``RetentionPolicy`` allows are pure Python. ``rules.py`` decides what to do with
them; ``service.py`` does the SQL. See the module docstring of ``rules.py`` for
the vocabulary (live / archived / purged, the legacy partition, calendar months).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PartitionStatus(StrEnum):
    """Where a partition of ``audit_events`` currently is. Closed set."""

    # Attached to the live ``audit_events`` parent: the application can insert
    # into it (if it is today's window) and read it through ``audit_events``.
    LIVE = "live"
    # Detached from ``audit_events`` and attached to ``audit_events_archive``
    # instead: no longer reachable through ``audit_events``, still a table with
    # the same append-only triggers, still readable through the archive parent.
    ARCHIVED = "archived"
    # Detached from ``audit_events_archive`` and dropped: gone. Only reached
    # through an explicit, separately-approved purge (``RetentionPolicy.
    # purge_after_days`` is not ``None``); never the default.
    PURGED = "purged"


@dataclass(frozen=True, slots=True)
class PartitionWindow:
    """One partition: its name, its ``[lower, upper)`` bound and its status.

    ``lower`` is ``None`` only for the legacy partition (``audit_events_p_legacy``,
    ``FOR VALUES FROM (MINVALUE)``): every row that existed before Migration 0086
    converted the table, whenever it was recorded. Every other partition has a
    concrete ``lower`` (a calendar month's start). ``upper`` is always concrete:
    it is what makes a partition eventually old enough to archive or purge.
    Neither bound is compared to a naive ``datetime`` (a rule function raises
    ``TypeError`` first; see ``rules.py``).
    """

    name: str
    lower: datetime | None
    upper: datetime
    status: PartitionStatus


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long a partition stays live, then archived, and how far ahead to plan.

    - ``archive_after_days``: a **live** partition becomes eligible for archiving
      once its window has been over for this many days (``upper + this <= now``).
      ``0`` archives it the moment its window ends. Must be ``>= 0``.
    - ``purge_after_days``: an **archived** partition becomes eligible for a hard
      delete once its window has been over for this many days. ``None`` (the
      default) means never: archived data is kept forever unless an operator
      later sets this explicitly. When set, it must be ``>= archive_after_days``
      (a partition cannot be purged before it could even have been archived).
    - ``horizon_months``: how many calendar months beyond the one containing
      "now" should always have a live partition ready (``plan_missing_partitions``
      in ``rules.py``). Must be ``>= 1``: at least next month's partition exists
      before this one ends, so an insert is never refused for lack of a partition.

    Raises ``ValueError`` (never silently clamps) for a value outside its range;
    ``TypeError`` for a non-``int`` (``bool`` counts as not an ``int`` here, since
    ``True``/``False`` as a day count is always a mistake).
    """

    archive_after_days: int
    purge_after_days: int | None
    horizon_months: int

    def __post_init__(self) -> None:
        _check_int("archive_after_days", self.archive_after_days, minimum=0)
        if self.purge_after_days is not None:
            _check_int("purge_after_days", self.purge_after_days, minimum=0)
            if self.purge_after_days < self.archive_after_days:
                raise ValueError(
                    "purge_after_days must be >= archive_after_days "
                    f"({self.purge_after_days} < {self.archive_after_days})"
                )
        _check_int("horizon_months", self.horizon_months, minimum=1)


def _check_int(field: str, value: object, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, not {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{field} must be >= {minimum}, got {value}")


def default_policy() -> RetentionPolicy:
    """The recommended policy of Decision 0027 (see the Decision for the reasoning).

    180 days (about six months) of live retention, no automatic purge, three
    months of live partitions always planned ahead. Not enforced anywhere by
    itself: a caller of ``AuditRetentionService`` chooses which policy to run
    with, and this is only the suggested default when it does not.
    """
    return RetentionPolicy(
        archive_after_days=180, purge_after_days=None, horizon_months=3
    )


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """What one ``AuditRetentionService.run_maintenance`` call did.

    Each list holds the ``PartitionWindow`` values *after* the action (so a
    created partition's ``status`` is ``LIVE``, an archived one's is
    ``ARCHIVED``, a purged one's is ``PURGED``), oldest window first.
    """

    created: tuple[PartitionWindow, ...]
    archived: tuple[PartitionWindow, ...]
    purged: tuple[PartitionWindow, ...]
