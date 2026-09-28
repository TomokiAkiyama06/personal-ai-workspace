"""The erasure of a deleted user's personal data, 30 days on (Issue #127).

REQUIREMENTS.md "User Deletion Retention": a deleted user is kept ``Pending
deletion`` for 30 days (only the Owner restores meanwhile); after that the personal
data is erased, and the account is not shown as ``Deleted`` until the erasure is
done and verified. Decision 0033 (Approved) left this to a later issue and fixed two
things it must respect: the ``users`` row is NOT hard-deleted (a tombstone; the
foreign keys to it stay RESTRICT, point 11), and the login name stays reserved
(point 10). Decision 0043 (Proposed) records the choices below.

Who runs it
-----------
``python -m paw_backend.cli user-erasure-run`` (``paw_backend.cli.erasure``), once a
day from the systemd timer of ``apps/backend/deploy/systemd``, connected as the
table owner (``PAW_MIGRATION_DATABASE_URL``), the way the audit retention job is.
The web role cannot do it and must not be able to: it has no DELETE on most of the
tables below, and ``paw_change_user_status`` (its only way to change a status) does
not allow ``pending_deletion`` to ``deleted``. So a compromised web process can
neither erase an account early nor mark one ``deleted`` without the erasure.

Which users
-----------
Users that are ``pending_deletion`` (never the Owner) whose latest change to that
status (``user_status_changes``) is at least ``RETENTION_HOURS`` (720) old at
``greatest(now, clock_timestamp())``: exactly the users whose restore
``UserLifecycleService.restore_user`` refuses (``RetentionExpiredError``), so no
user is both restorable and erasable.

What one erasure does (ONE transaction per user)
-----------------------------------------------
1. Locks the user's row (``FOR NO KEY UPDATE``, the lock of delete and restore, with
   a bounded wait) and checks again that the user is due.
2. Refuses, and changes nothing, while
   * a task the user created is still active or has an active queue entry
     (``tasks_active``): the user task sweep (``paw_backend.orchestrator.user_sweep``)
     stops them; the erasure never races a running Agent;
   * the user still has managed checkouts (``repository_checkouts``,
     ``checkouts_remaining``): those are clones in the user's own Linux account,
     which this job cannot reach and must not claim erased. The operator removes
     the directories and says so with ``--checkouts-removed <user id>``; the job
     then deletes those rows (audited ``checkouts_released``) and goes on.
3. Deletes the personal data (the tables of :data:`PERSONAL_TABLES`): password hash,
   Passkeys and their challenges, sessions, pairings, invitations, setup / reset
   tokens (credentials); the private conversations with their messages, session
   state and journal (Private Chat; the Memory sources that cited them are marked
   ``source_deleted_at`` and lose the reference through their foreign keys); the
   ``user`` scope Memory versions and the memories left without a version, the
   consolidation keys (Private Memory); the per-user connection quotas (personal
   settings); the project memberships (the account can never come back).
4. Verifies, in the same transaction, that no row of those tables is left for the
   user; otherwise the transaction is rolled back (``verification_failed``).
5. Sets ``users.status`` to ``deleted`` and appends the ``user_status_changes`` row
   (``changed_by`` NULL: the system), and writes ``auth.user.erase`` / ``erased``.
   The status, the history and the audit row commit with the erasure or not at all.

Kept (Decision 0043): the ``users`` row (id, login name, role, timestamps) as the
minimal deletion record, the status history, ``audit_events`` (ids only), and what
belongs to a project rather than to the person (tasks, their logs, research scratch
items, non-private Memory versions a user wrote, connection usage accounting, tool
approvals). A later restore from a backup must re-apply ``deleted`` (the
requirements' "削除記録"); that is the recovery feature's job.

A refusal or failure writes ``auth.user.erase`` / deny with the reason in a short
transaction of its own (best effort), leaves the user ``pending_deletion`` (still
without access) and makes the run fail (exit 3, the systemd ``OnFailure=`` unit
tells the Owner), so the next daily run tries again. Re-running is safe: an erased
user is ``deleted`` and not listed any more; a refused one is tried again.
"""

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import psycopg.errors
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.audit import AuthAction, AuthAudit, AuthReason
from paw_backend.auth.onboarding.lifecycle import RETENTION_HOURS
from paw_backend.authz import PostgresAuditSink
from paw_backend.db import Database
from paw_backend.tasks import TERMINAL_STATES, TaskState
from paw_backend.tasks.queueing import ACTIVE_QUEUE_STATUSES

logger = logging.getLogger(__name__)

# How long the user's row lock may be waited for (delete / restore hold it briefly).
LOCK_TIMEOUT_MS = 5000
# How many due users one listing returns; ``run`` pages through all of them.
PAGE_SIZE = 100
# The key of the session-level advisory lock that serialises runs: b"paw_era1".
ERASURE_LOCK_KEY = 0x7061775F65726131

# (table, the column naming the user): every row of these is personal data of that
# user and is deleted, then counted again (must be 0). The order is the order of
# the deletes (children before the rows their foreign keys point at).
PERSONAL_TABLES: tuple[tuple[str, str], ...] = (
    ("passkey_challenges", "user_id"),
    ("device_pairings", "user_id"),
    ("auth_sessions", "user_id"),
    ("user_passkeys", "user_id"),
    ("password_credentials", "user_id"),
    ("user_invitations", "user_id"),
    ("setup_tokens", "user_id"),
    ("conversations", "owner_user_id"),
    ("memory_journal_entries", "owner_user_id"),
    ("memory_consolidation_keys", "owner_user_id"),
    ("connection_quotas", "user_id"),
    ("project_members", "user_id"),
)


class ErasureOutcome(StrEnum):
    """What happened to one due user."""

    ERASED = "erased"
    TASKS_ACTIVE = "tasks_active"
    CHECKOUTS_REMAINING = "checkouts_remaining"
    VERIFICATION_FAILED = "verification_failed"
    # The user's row stayed locked longer than ``LOCK_TIMEOUT_MS``.
    BUSY = "busy"
    # Anything else raised (the error type is kept, never its message).
    FAILED = "failed"
    # Not due (any more): restored, already erased, or not old enough.
    NOT_DUE = "not_due"


_DENY_REASONS = {
    ErasureOutcome.TASKS_ACTIVE: AuthReason.TASKS_ACTIVE,
    ErasureOutcome.CHECKOUTS_REMAINING: AuthReason.CHECKOUTS_REMAINING,
    ErasureOutcome.VERIFICATION_FAILED: AuthReason.VERIFICATION_FAILED,
    ErasureOutcome.BUSY: AuthReason.ERASURE_FAILED,
    ErasureOutcome.FAILED: AuthReason.ERASURE_FAILED,
}


@dataclass(frozen=True, slots=True)
class UserErasureResult:
    user_id: uuid.UUID
    outcome: ErasureOutcome
    # The type name of the error of a ``FAILED`` erasure; never its message.
    error_type: str | None = None
    released_checkouts: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome in (ErasureOutcome.ERASED, ErasureOutcome.NOT_DUE)


@dataclass(frozen=True, slots=True)
class ErasureRunReport:
    results: tuple[UserErasureResult, ...]

    @property
    def ok(self) -> bool:
        """Every due user was erased (also true when nobody was due)."""
        return all(result.ok for result in self.results)

    def count(self, outcome: ErasureOutcome) -> int:
        return sum(result.outcome is outcome for result in self.results)


class ErasureAlreadyRunningError(Exception):
    """Another run holds the advisory lock; nothing was done."""


class _Refused(Exception):
    def __init__(self, outcome: ErasureOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


def _literals(values: Iterable[str]) -> str:
    """``'a', 'b'`` of a closed set of enum constants (never caller input)."""
    return ", ".join(f"'{value}'" for value in sorted(values))


_ACTIVE_TASK_STATES = _literals(
    state.value for state in set(TaskState) - TERMINAL_STATES
)
_ACTIVE_ENTRIES = _literals(status.value for status in ACTIVE_QUEUE_STATUSES)

# Due: pending deletion for at least RETENTION_HOURS (the restore's rule, negated).
_DUE_CONDITION = """
    u.status = 'pending_deletion' AND u.system_role <> 'owner'
    AND (SELECT max(c.changed_at) FROM user_status_changes c
          WHERE c.user_id = u.id AND c.new_status = 'pending_deletion')
        + make_interval(hours => :hours)
        <= greatest(CAST(:now AS timestamptz), clock_timestamp())
"""
_DUE_USERS = text(
    f"SELECT u.id FROM users u WHERE {_DUE_CONDITION} "
    "AND (CAST(:after AS uuid) IS NULL OR u.id > CAST(:after AS uuid)) "
    "ORDER BY u.id LIMIT :limit"
)
_LOCK_DUE_USER = text(
    f"SELECT u.id FROM users u WHERE u.id = :id AND {_DUE_CONDITION} "
    "FOR NO KEY UPDATE OF u"
)
_ANYTHING_ACTIVE = text(
    f"""
    SELECT EXISTS (SELECT 1 FROM tasks t WHERE t.created_by = :id
                      AND t.state IN ({_ACTIVE_TASK_STATES}))
        OR EXISTS (SELECT 1 FROM tasks t JOIN queue_entries q ON q.task_id = t.id
                    WHERE t.created_by = :id AND q.status IN ({_ACTIVE_ENTRIES}))
    """
)
_PRIVATE_MEMORY_LEFT = text(
    "SELECT count(*) FROM memory_versions WHERE scope = 'user' AND owner_user_id = :id"
)


class UserErasureService:
    """Erases the personal data of users 30 days after their deletion (module)."""

    def __init__(
        self,
        database: Database,
        *,
        lock_timeout_ms: int = LOCK_TIMEOUT_MS,
        clock: Callable[[], datetime] | None = None,
        audit_timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if (
            isinstance(lock_timeout_ms, bool)
            or not isinstance(lock_timeout_ms, int)
            or not 1 <= lock_timeout_ms <= 60_000
        ):
            raise ValueError("lock_timeout_ms must be an int in [1, 60000]")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._database = database
        self._lock_timeout_ms = lock_timeout_ms
        self._clock = clock or (lambda: datetime.now(UTC))
        self._audit = AuthAudit(
            PostgresAuditSink(database),
            timeout_seconds=audit_timeout_seconds,
            clock=self._clock,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    # -- the run ---------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def run_lock(self) -> AsyncIterator[None]:
        """A session-level advisory lock for the ``async with`` block.

        A second run meanwhile (a manual run beside the timer) gets
        ``ErasureAlreadyRunningError`` at once. PostgreSQL releases the lock if
        this process dies (the connection closes).
        """
        async with self._database.engine.connect() as connection:
            acquired = (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": ERASURE_LOCK_KEY},
                )
            ).scalar()
            await connection.commit()
            if not acquired:
                raise ErasureAlreadyRunningError()
            try:
                yield
            finally:
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": ERASURE_LOCK_KEY},
                    )
                    await connection.commit()
                except Exception as error:  # the connection's end releases it too
                    logger.warning(
                        "Releasing the erasure lock failed (%s)", type(error).__name__
                    )

    async def run(
        self, *, checkouts_removed: Iterable[uuid.UUID] = ()
    ) -> ErasureRunReport:
        """Erase every due user, under the run lock; one result per due user.

        ``checkouts_removed``: users whose managed checkouts the operator removed
        from disk (their ``repository_checkouts`` rows are then deleted).
        """
        released = frozenset(checkouts_removed)
        for user_id in released:
            if not isinstance(user_id, uuid.UUID):
                raise TypeError("checkouts_removed must hold uuid.UUID values")
        results: list[UserErasureResult] = []
        async with self.run_lock():
            after: uuid.UUID | None = None
            while True:
                page = await self.due_user_ids(after=after)
                for user_id in page:
                    results.append(
                        await self.erase_user(
                            user_id, checkouts_removed=user_id in released
                        )
                    )
                if len(page) < PAGE_SIZE:
                    break
                after = page[-1]
        return ErasureRunReport(tuple(results))

    async def due_user_ids(
        self, *, after: uuid.UUID | None = None, limit: int = PAGE_SIZE
    ) -> tuple[uuid.UUID, ...]:
        """The users whose erasure is due, in id order (after ``after``)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        async with self._database.session() as session, session.begin():
            rows = await session.execute(
                _DUE_USERS,
                {
                    "hours": RETENTION_HOURS,
                    "now": self._now(),
                    "after": after,
                    "limit": limit,
                },
            )
            return tuple(rows.scalars())

    # -- one user --------------------------------------------------------------------

    async def erase_user(
        self, user_id: uuid.UUID, *, checkouts_removed: bool = False
    ) -> UserErasureResult:
        """Erase one user if due (see the module docstring). Never raises for a
        refusal or a database error: the outcome says what happened."""
        if not isinstance(user_id, uuid.UUID):
            raise TypeError("user_id must be a uuid.UUID")
        correlation_id = uuid.uuid4()
        try:
            released = await self._erase(user_id, checkouts_removed, correlation_id)
        except _Refused as refusal:
            if refusal.outcome is ErasureOutcome.NOT_DUE:
                return UserErasureResult(user_id, ErasureOutcome.NOT_DUE)
            await self._deny(user_id, refusal.outcome, correlation_id)
            return UserErasureResult(user_id, refusal.outcome)
        except DBAPIError as error:
            outcome = (
                ErasureOutcome.BUSY
                if isinstance(error.orig, psycopg.errors.LockNotAvailable)
                else ErasureOutcome.FAILED
            )
            error_type = type(error.orig or error).__name__
            logger.error("Erasing a user failed (%s)", error_type)
            await self._deny(user_id, outcome, correlation_id)
            return UserErasureResult(user_id, outcome, error_type)
        except Exception as error:
            logger.error("Erasing a user failed (%s)", type(error).__name__)
            await self._deny(user_id, ErasureOutcome.FAILED, correlation_id)
            return UserErasureResult(
                user_id, ErasureOutcome.FAILED, type(error).__name__
            )
        return UserErasureResult(
            user_id, ErasureOutcome.ERASED, released_checkouts=released
        )

    async def _erase(
        self, user_id: uuid.UUID, checkouts_removed: bool, correlation_id: uuid.UUID
    ) -> int:
        now = self._now()
        async with self._database.session() as session, session.begin():
            await session.execute(
                select(
                    func.set_config("lock_timeout", str(self._lock_timeout_ms), True)
                )
            )
            locked = (
                await session.execute(
                    _LOCK_DUE_USER,
                    {"id": user_id, "hours": RETENTION_HOURS, "now": now},
                )
            ).first()
            if locked is None:
                raise _Refused(ErasureOutcome.NOT_DUE)
            if (await session.execute(_ANYTHING_ACTIVE, {"id": user_id})).scalar():
                raise _Refused(ErasureOutcome.TASKS_ACTIVE)
            checkouts = await self._count(
                session, "repository_checkouts", "user_id", user_id
            )
            if checkouts and not checkouts_removed:
                raise _Refused(ErasureOutcome.CHECKOUTS_REMAINING)
            released = 0
            if checkouts:
                released = (
                    await session.execute(
                        text("DELETE FROM repository_checkouts WHERE user_id = :id"),
                        {"id": user_id},
                    )
                ).rowcount
                await self._record(
                    session, AuthReason.CHECKOUTS_RELEASED, user_id, correlation_id
                )
            await self._delete_personal_data(session, user_id, now)
            await self._verify(session, user_id)
            await self._mark_deleted(session, user_id, now)
            await self._record(session, AuthReason.ERASED, user_id, correlation_id)
            return released

    async def _delete_personal_data(
        self, session: AsyncSession, user_id: uuid.UUID, now: datetime
    ) -> None:
        params = {"id": user_id, "now": now}
        # Memory sources that cite a conversation about to go: mark them, the
        # foreign keys then clear the reference (the schema's own "deleted source").
        await session.execute(
            text(
                "UPDATE memory_sources SET source_deleted_at = :now "
                "WHERE source_deleted_at IS NULL AND conversation_id IN "
                "(SELECT id FROM conversations WHERE owner_user_id = :id)"
            ),
            params,
        )
        # Private Memory: the ``user`` scope versions (their embeddings, relations,
        # sources and metadata history cascade), then each memory left without any
        # version. A memory that was widened later keeps its wider versions.
        memory_ids = list(
            (
                await session.execute(
                    text(
                        "DELETE FROM memory_versions "
                        "WHERE scope = 'user' AND owner_user_id = :id "
                        "RETURNING memory_id"
                    ),
                    params,
                )
            ).scalars()
        )
        if memory_ids:
            await session.execute(
                text(
                    "DELETE FROM memories m WHERE m.id = ANY(:ids) AND NOT EXISTS "
                    "(SELECT 1 FROM memory_versions v WHERE v.memory_id = m.id)"
                ),
                {"ids": list(set(memory_ids))},
            )
        for table, column in PERSONAL_TABLES:
            # Table and column names are the constants above, never input.
            await session.execute(
                text(f"DELETE FROM {table} WHERE {column} = :id"), params
            )

    async def _verify(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        left = (
            await session.execute(_PRIVATE_MEMORY_LEFT, {"id": user_id})
        ).scalar_one()
        for table, column in PERSONAL_TABLES + (("repository_checkouts", "user_id"),):
            left += await self._count(session, table, column, user_id)
        if left:
            raise _Refused(ErasureOutcome.VERIFICATION_FAILED)

    @staticmethod
    async def _count(
        session: AsyncSession, table: str, column: str, user_id: uuid.UUID
    ) -> int:
        return (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :id"),
                {"id": user_id},
            )
        ).scalar_one()

    @staticmethod
    async def _mark_deleted(
        session: AsyncSession, user_id: uuid.UUID, now: datetime
    ) -> None:
        changed = (
            await session.execute(
                text(
                    "UPDATE users SET status = 'deleted', updated_at = :now "
                    "WHERE id = :id AND status = 'pending_deletion' "
                    "AND system_role <> 'owner'"
                ),
                {"id": user_id, "now": now},
            )
        ).rowcount
        if changed != 1:  # cannot happen under the row lock
            raise _Refused(ErasureOutcome.NOT_DUE)
        await session.execute(
            text(
                "INSERT INTO user_status_changes (id, user_id, old_status, "
                "new_status, changed_at, changed_by, recorded_at) VALUES "
                "(gen_random_uuid(), :id, 'pending_deletion', 'deleted', :now, "
                "NULL, clock_timestamp())"
            ),
            {"id": user_id, "now": now},
        )

    async def _record(
        self,
        session: AsyncSession,
        reason: AuthReason,
        user_id: uuid.UUID,
        correlation_id: uuid.UUID,
    ) -> None:
        await self._audit.record_in(
            session,
            self._audit.event(
                AuthAction.USER_ERASE,
                reason,
                allowed=True,
                correlation_id=correlation_id,
                resource_kind="user",
                resource_id=user_id,
            ),
        )

    async def _deny(
        self, user_id: uuid.UUID, outcome: ErasureOutcome, correlation_id: uuid.UUID
    ) -> None:
        await self._audit.record_best_effort(
            self._audit.event(
                AuthAction.USER_ERASE,
                _DENY_REASONS[outcome],
                allowed=False,
                correlation_id=correlation_id,
                resource_kind="user",
                resource_id=user_id,
            )
        )


__all__ = [
    "ERASURE_LOCK_KEY",
    "PERSONAL_TABLES",
    "ErasureAlreadyRunningError",
    "ErasureOutcome",
    "ErasureRunReport",
    "UserErasureResult",
    "UserErasureService",
]
