"""``AuditRetentionService``: the Store side of ``audit_events`` retention (Issue #86).

``rules.py`` decides *which* partitions must be created, archived or purged;
this module is the only place that runs the SQL. It needs a **privileged**
connection — the same role migrations run as (``PAW_MIGRATION_DATABASE_URL``,
the table's owner), never the application's low-privilege role
(``PAW_APP_DATABASE_ROLE``, granted only ``INSERT`` / ``SELECT`` on
``audit_events`` and ``SELECT`` on ``audit_events_archive`` — see Migration
0086): creating a partition, detaching or attaching one, and dropping one are
all DDL that only the owner may run (proved by ``tests/test_retention_postgres.
py``'s non-superuser role tests). Nothing in this module checks that itself
(same as every other migration-adjacent operation in this codebase); running it
as the application role fails with a PostgreSQL permission error on the first
statement, inside the transaction, so nothing is half-done.

There is no scheduler here (Decision 0027 explicitly leaves one out): call
``run_maintenance()`` from whatever invokes it. The scheduled caller is
``runner.run_scheduled_maintenance`` (Issue #117, Decision 0031), which adds the
lock (``maintenance_lock``), the coverage check and the run's own audit row
(``record_maintenance_outcome``), run by ``python -m paw_backend.cli
audit-retention-run`` from a systemd timer. Each call is idempotent to run
again immediately after a partial failure: a step already done (a partition
already created, already archived, already purged) is simply not in
``rules.py``'s next answer.
"""

import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.retention.audit import (
    RetentionAction,
    RetentionActor,
    record_maintenance_event,
    record_partition_event,
)
from paw_backend.authz.retention.errors import (
    MaintenanceAlreadyRunningError,
    PartitionAlreadyExistsError,
    PartitionNotArchivedError,
    PartitionNotLiveError,
)
from paw_backend.authz.retention.models import AuditRetentionPartitionRecord
from paw_backend.authz.retention.records import (
    MaintenanceReport,
    PartitionStatus,
    PartitionWindow,
    RetentionPolicy,
    default_policy,
)
from paw_backend.authz.retention.rules import (
    ARCHIVE_PARENT_TABLE,
    LIVE_PARENT_TABLE,
    month_start,
    partition_name,
    partitions_due_for_archive,
    partitions_due_for_purge,
    plan_missing_partitions,
)
from paw_backend.db import Database

_logger = logging.getLogger(__name__)

# Every name this service ever builds SQL identifiers from matches this: either
# the fixed legacy name or ``partition_name()``'s own pattern. Defense in depth
# against a bookkeeping row this service did not itself write (there is no other
# writer today, but the check is cheap and the alternative is string-formatting
# an identifier from the database into DDL unchecked).
_SAFE_PARTITION_NAME = re.compile(r"^audit_events_p(?:_legacy|\d{4}_\d{2})$")


# The key of the session-level advisory lock that serializes maintenance runs
# (Issue #117): ``b"paw_ret1"`` read as a big-endian signed 64-bit integer. Any
# fixed value works; it only has to be the same for every run and unlikely to
# collide with another application's advisory lock on the same database.
MAINTENANCE_LOCK_KEY = 0x7061775F72657431


def _quoted(name: str) -> str:
    if not _SAFE_PARTITION_NAME.match(name):
        raise ValueError(f"not a partition name this service manages: {name!r}")
    return '"' + name + '"'


def _bound_literal(moment: datetime | None) -> str:
    """``moment`` as a SQL ``FOR VALUES`` bound: a literal, or ``MINVALUE``."""
    if moment is None:
        return "MINVALUE"
    return "'" + moment.astimezone(UTC).isoformat() + "'::timestamptz"


def _protected_partition_name() -> str:
    """The one partition ``archive_due_partitions`` / ``purge_due_partitions``

    never touch, whatever ``policy`` and ``clock`` say: the calendar-month
    partition that covers *this process's real wall-clock moment* right now.
    Every row this service (and every other writer of ``audit_events``) ever
    inserts gets its ``recorded_at`` forced to the database's real clock, never
    to a caller's injected one (Migration 0025's trigger; Decision 0027) — so a
    policy or a test clock set far enough ahead to make its own partition due
    would, without this, also make whichever partition holds *this instant*
    due, and archiving (``DETACH``) or purging (``DROP``) it would leave
    nothing for the very next INSERT — including this service's own audit row
    for the operation that just did it — to land in. Named independently of
    ``clock``/``policy`` on purpose: it must protect the same partition
    regardless of what either one claims "now" is.
    """
    return partition_name(month_start(datetime.now(UTC)))


@dataclass(frozen=True, slots=True)
class AuditRetentionService:
    """Creates, archives and purges partitions of ``audit_events``.

    ``database`` must be connected as the migration / table-owner role (see the
    module docstring). ``clock`` defaults to the wall clock; tests inject a
    fixed one, the same convention as ``paw_backend.auth.audit.AuthAudit``.
    """

    database: Database
    clock: Callable[[], datetime] | None = None
    # How long a step's statements may wait for a lock, in milliseconds
    # (``SET LOCAL lock_timeout`` in each step's transaction); ``None`` keeps
    # the server's setting (no limit by default). The scheduled command sets it
    # (Decision 0031): ``DETACH PARTITION`` / ``CREATE TABLE ... PARTITION OF``
    # queued behind a long reader of ``audit_events`` would otherwise make every
    # INSERT into ``audit_events`` queue behind them for as long as it waits. A
    # step that times out raises (``OperationalError``, SQLSTATE 55P03) and
    # rolls back; the next run does it again.
    lock_timeout_ms: int | None = None

    def __post_init__(self) -> None:
        if self.lock_timeout_ms is not None and (
            isinstance(self.lock_timeout_ms, bool)
            or not isinstance(self.lock_timeout_ms, int)
            or self.lock_timeout_ms <= 0
        ):
            raise ValueError("lock_timeout_ms must be a positive integer or None")

    async def _limit_lock_wait(self, session: AsyncSession) -> None:
        if self.lock_timeout_ms is None:
            return
        await session.execute(
            text("SELECT set_config('lock_timeout', :value, true)"),
            {"value": f"{self.lock_timeout_ms}ms"},
        )

    def _now(self) -> datetime:
        now = self.clock() if self.clock is not None else datetime.now(UTC)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    async def existing_partitions(
        self, session: AsyncSession | None = None
    ) -> list[PartitionWindow]:
        """Every partition the bookkeeping table knows about, oldest first."""

        async def read(active_session: AsyncSession) -> list[PartitionWindow]:
            query = select(AuditRetentionPartitionRecord).order_by(
                AuditRetentionPartitionRecord.upper_bound
            )
            rows = (await active_session.execute(query)).scalars()
            return [
                PartitionWindow(
                    row.name,
                    row.lower_bound,
                    row.upper_bound,
                    PartitionStatus(row.status),
                )
                for row in rows
            ]

        if session is not None:
            return await read(session)
        async with self.database.session() as new_session:
            return await read(new_session)

    async def ensure_partitions(
        self,
        policy: RetentionPolicy | None = None,
        *,
        actor: RetentionActor | None = None,
    ) -> list[PartitionWindow]:
        """Create every calendar-month partition ``plan_missing_partitions`` wants.

        One transaction for the whole call: either every missing partition (and
        its audit row) is created, or none is. Returns the created windows,
        oldest first (``[]`` when nothing was missing).
        """
        policy = policy or default_policy()
        now = self._now()
        async with self.database.session() as session, session.begin():
            await self._limit_lock_wait(session)
            existing = await self.existing_partitions(session)
            missing = plan_missing_partitions(now, existing, policy)
            for window in missing:
                await self._create_partition(
                    session, window, occurred_at=now, actor=actor
                )
            return missing

    async def _create_partition(
        self,
        session: AsyncSession,
        window: PartitionWindow,
        *,
        occurred_at: datetime,
        actor: RetentionActor | None,
    ) -> None:
        name = _quoted(window.name)
        existing = await session.get(AuditRetentionPartitionRecord, window.name)
        if existing is not None:
            raise PartitionAlreadyExistsError(window.name)
        await session.execute(
            text(
                f"CREATE TABLE {name} PARTITION OF {LIVE_PARENT_TABLE} "
                f"FOR VALUES FROM ({_bound_literal(window.lower)}) "
                f"TO ({_bound_literal(window.upper)})"
            )
        )
        # Statement-level triggers are not cloned to a new partition by PostgreSQL
        # (unlike the row-level ones, which clone automatically from the parent):
        # this one must be created explicitly, every time, or a direct
        # ``TRUNCATE`` of this one partition would silently bypass the
        # append-only guarantee (proved empirically; see Decision 0027).
        await session.execute(
            text(
                f"CREATE TRIGGER tr_reject_truncate BEFORE TRUNCATE ON {name} "
                "FOR EACH STATEMENT EXECUTE FUNCTION paw_reject_audit_events_change()"
            )
        )
        await session.execute(
            text(f"ALTER TABLE {name} ENABLE ALWAYS TRIGGER tr_reject_truncate")
        )
        session.add(
            AuditRetentionPartitionRecord(
                name=window.name,
                lower_bound=window.lower,
                upper_bound=window.upper,
                status=PartitionStatus.LIVE.value,
                created_at=occurred_at,
            )
        )
        await record_partition_event(
            session,
            RetentionAction.PARTITION_CREATED,
            window,
            occurred_at=occurred_at,
            actor=actor,
        )

    async def archive_due_partitions(
        self,
        policy: RetentionPolicy | None = None,
        *,
        actor: RetentionActor | None = None,
    ) -> list[PartitionWindow]:
        """Move every ``LIVE`` partition due for archiving into the archive parent.

        Each partition is detached from ``audit_events`` and attached to
        ``audit_events_archive`` with the same bound: no row is read, copied or
        rewritten (a metadata-only operation, however many rows the partition
        holds). One transaction for the whole call. Returns the archived
        windows (``status`` now ``ARCHIVED``), oldest first.
        """
        policy = policy or default_policy()
        now = self._now()
        protected = _protected_partition_name()
        async with self.database.session() as session, session.begin():
            await self._limit_lock_wait(session)
            existing = await self.existing_partitions(session)
            due = [
                window
                for window in partitions_due_for_archive(now, existing, policy)
                if window.name != protected
            ]
            archived = []
            for window in due:
                await self._archive_partition(
                    session, window, occurred_at=now, actor=actor
                )
                archived.append(
                    PartitionWindow(
                        window.name,
                        window.lower,
                        window.upper,
                        PartitionStatus.ARCHIVED,
                    )
                )
            return archived

    async def _archive_partition(
        self,
        session: AsyncSession,
        window: PartitionWindow,
        *,
        occurred_at: datetime,
        actor: RetentionActor | None,
    ) -> None:
        record = await session.get(AuditRetentionPartitionRecord, window.name)
        if record is None or PartitionStatus(record.status) != PartitionStatus.LIVE:
            status = record.status if record is not None else "unknown"
            raise PartitionNotLiveError(window.name, status)
        name = _quoted(window.name)
        await session.execute(
            text(f"ALTER TABLE {LIVE_PARENT_TABLE} DETACH PARTITION {name}")
        )
        await session.execute(
            text(
                f"ALTER TABLE {ARCHIVE_PARENT_TABLE} ATTACH PARTITION {name} "
                f"FOR VALUES FROM ({_bound_literal(window.lower)}) "
                f"TO ({_bound_literal(window.upper)})"
            )
        )
        record.status = PartitionStatus.ARCHIVED.value
        record.archived_at = occurred_at
        await record_partition_event(
            session,
            RetentionAction.PARTITION_ARCHIVED,
            window,
            occurred_at=occurred_at,
            actor=actor,
        )

    async def purge_due_partitions(
        self,
        policy: RetentionPolicy | None = None,
        *,
        actor: RetentionActor | None = None,
    ) -> list[PartitionWindow]:
        """Drop every ``ARCHIVED`` partition due for purging. ``[]`` unless enabled.

        ``rules.partitions_due_for_purge`` already returns ``[]`` whenever
        ``policy.purge_after_days`` is ``None`` (the default): this never drops
        anything unless an operator explicitly set that field. The audit row is
        written **before** the ``DROP TABLE`` in the same transaction, so a
        reader never sees a purge recorded that did not happen, and a purge
        that did happen is never silently unrecorded.
        """
        policy = policy or default_policy()
        now = self._now()
        protected = _protected_partition_name()
        async with self.database.session() as session, session.begin():
            await self._limit_lock_wait(session)
            existing = await self.existing_partitions(session)
            due = [
                window
                for window in partitions_due_for_purge(now, existing, policy)
                if window.name != protected
            ]
            purged = []
            for window in due:
                await self._purge_partition(
                    session, window, occurred_at=now, actor=actor
                )
                purged.append(
                    PartitionWindow(
                        window.name, window.lower, window.upper, PartitionStatus.PURGED
                    )
                )
            return purged

    async def _purge_partition(
        self,
        session: AsyncSession,
        window: PartitionWindow,
        *,
        occurred_at: datetime,
        actor: RetentionActor | None,
    ) -> None:
        record = await session.get(AuditRetentionPartitionRecord, window.name)
        if record is None or PartitionStatus(record.status) != PartitionStatus.ARCHIVED:
            status = record.status if record is not None else "unknown"
            raise PartitionNotArchivedError(window.name, status)
        name = _quoted(window.name)
        await record_partition_event(
            session,
            RetentionAction.PARTITION_PURGED,
            window,
            occurred_at=occurred_at,
            actor=actor,
        )
        await session.execute(
            text(f"ALTER TABLE {ARCHIVE_PARENT_TABLE} DETACH PARTITION {name}")
        )
        await session.execute(text(f"DROP TABLE {name}"))
        record.status = PartitionStatus.PURGED.value
        record.purged_at = occurred_at

    @contextlib.asynccontextmanager
    async def maintenance_lock(self) -> AsyncIterator[None]:
        """Hold the maintenance advisory lock for the ``async with`` block.

        A session-level ``pg_try_advisory_lock`` on a connection of its own,
        held while the steps run on their own sessions: a second run (another
        timer, a manual run, a second host) that tries meanwhile gets
        ``MaintenanceAlreadyRunningError`` at once instead of waiting or
        racing. Released on exit, and by PostgreSQL itself if this process
        dies (the connection closes). If the server closes this idle
        connection mid-run, the lock goes with it and the rest of the run is
        unguarded (Decision 0031 "リスク"); the unlock failure is only
        logged. Decision 0027's "リスク" named this lock as the job of the
        Issue that adds a scheduler (#117).
        """
        async with self.database.engine.connect() as connection:
            acquired = (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": MAINTENANCE_LOCK_KEY},
                )
            ).scalar()
            await connection.commit()
            if not acquired:
                raise MaintenanceAlreadyRunningError()
            try:
                yield
            finally:
                # If the server closed this connection meanwhile (an idle
                # timeout, a dropped TCP connection), PostgreSQL has already
                # released the lock with it; failing to unlock on the dead
                # connection must not replace the run's own outcome.
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": MAINTENANCE_LOCK_KEY},
                    )
                    await connection.commit()
                except SQLAlchemyError as error:
                    _logger.warning(
                        "audit retention: releasing the maintenance lock failed "
                        "(%s); the server releases it with the connection",
                        type(error).__name__,
                    )

    async def record_maintenance_outcome(
        self,
        action: RetentionAction,
        reason: str,
        *,
        actor: RetentionActor | None = None,
    ) -> None:
        """Write one run-level audit row in a transaction of its own.

        Separate from the steps' transactions on purpose: a failed step has
        rolled its own transaction back, and its failure must still be
        recorded (the same reason ``PostgresAuditSink`` records a denial in a
        short transaction of its own).
        """
        now = self._now()
        async with self.database.session() as session, session.begin():
            await record_maintenance_event(
                session, action, reason, occurred_at=now, actor=actor
            )

    async def run_maintenance(
        self,
        policy: RetentionPolicy | None = None,
        *,
        actor: RetentionActor | None = None,
    ) -> MaintenanceReport:
        """Create, then archive, then purge — each via its own method above.

        Three separate transactions (one per step, as each method already is),
        not one: a failure archiving does not undo partitions this call already
        created, and both stand whatever ``purge_due_partitions`` does next.
        """
        policy = policy or default_policy()
        created = await self.ensure_partitions(policy, actor=actor)
        archived = await self.archive_due_partitions(policy, actor=actor)
        purged = await self.purge_due_partitions(policy, actor=actor)
        return MaintenanceReport(
            created=tuple(created), archived=tuple(archived), purged=tuple(purged)
        )


__all__ = [
    "AuditRetentionService",
]
