"""PostgreSQL access of the shared connections (PAW-030).

Every call runs on ``Database.transact_abortable`` (a write, and the admission: all
or nothing) or ``Database.fetch_abortable`` (a read, and ``settle``, which is one
atomic statement whose late application is harmless): a connection outside the pool,
ONE deadline for the whole call that includes the wait for a free connection and for
row locks, and the socket shut down at the deadline. A write that ran out of time is
never applied later: its transaction is abandoned before the ``COMMIT`` and the
server is given the same limit (``transact_abortable``), so ``TimeoutError`` (which
the service turns into
:class:`~paw_backend.connections.errors.ConnectionBusyError`) means nothing was
changed, except when the time ran out during the ``COMMIT`` itself. SQL is written
here as constants; the only text formatted into it is a fixed fragment of this module
(the clock, see below). Values are always bound by the driver.

The admission (``admit``): one transaction that makes "may this call start?" and
"record that it started" one atomic step
------------------------------------------------------------------------------
1. the task's row ``FOR SHARE`` (it must exist, belong to the user, not have ended
   and be in the run of the caller: a transition and this admission are ordered,
   never crossed; the same rule as the approvals of the Tool Broker);
2. the connection's row ``FOR SHARE`` (an admin's ``disable`` or ``replace``
   waits for the admissions in flight, so once it returned no new call starts);
3. the user's quota rows for this kind ``FOR UPDATE``, in one fixed order: they are
   the LOCK that serialises all admissions of one user and kind. Different users
   and kinds do not wait for each other. Only after the lock is held does the
   transaction read anything it decides on: in READ COMMITTED each statement takes
   its own snapshot, so the usage sums read next include every admission that
   committed before the lock was released (a count and a lock in ONE statement
   would decide on a snapshot taken before the wait);
4. the clock, read ONCE, after the lock: the DATABASE's ``clock_timestamp()`` (the
   test clock, if any). The same instant starts the windows of the checks and is
   the ``started_at`` of the row that is inserted, so two admissions that the lock
   ordered have ordered ``started_at`` and no call falls between two windows;
5. the checks (``used >= limit`` per quota, for a call that starts a NEW task) and
   the ``INSERT`` of the ``in_flight`` usage row, in the same transaction.

Lock order everywhere: tasks, shared_connections, connection_quotas,
connection_usage. ``set_quota`` and the connection statements each touch one of the
tables only, so no cycle exists.

The clock
---------
Every instant is the DATABASE's (Decision 0007, 10: one clock; workers on hosts
whose clocks disagree cannot change a window or a duration). It is read INSIDE the
statement (``WITH clock AS MATERIALIZED (SELECT clock_timestamp() ...)``: evaluated
once per statement however often it is used) or, in the admission, by one
``SELECT clock_timestamp()`` after the locks are held. ``ConnectionStore(clock=...,
allow_explicit_clock=True)`` is a TEST SEAM that stands in for it so that a test
moves time without sleeping; production code builds ``ConnectionStore(database)``.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any

import psycopg

from paw_backend.connections.domain import (
    UNLIMITED,
    ConnectionKind,
    ConnectionStatus,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    RefusalReason,
    Unlimited,
    UsagePurpose,
    UsageStatus,
    window_end,
    window_start,
)
from paw_backend.connections.errors import InputProblem, InvalidConnectionInputError
from paw_backend.connections.limits import (
    DEFAULT_DATABASE_TIMEOUT_SECONDS,
    MAX_DATABASE_TIMEOUT_SECONDS,
)
from paw_backend.connections.records import (
    ConnectionInfo,
    Quota,
    QuotaUsage,
    UsageRecord,
)
from paw_backend.connections.validation import (
    validate_bool,
    validate_seconds,
)
from paw_backend.db import Database
from paw_backend.tasks.domain import TaskRun
from paw_backend.tools.task_state import TaskActivity, activity_of

_CONNECTION_COLUMNS = "id, kind, status, enabled, checked_at, created_at, updated_at"
_USAGE_COLUMNS = (
    "id, user_id, task_id, project_id, kind, model, purpose, status, failure_code,"
    " input_tokens, output_tokens, started_at, finished_at, duration_ms"
)
_QUOTA_COLUMNS = "user_id, kind, metric, period, limit_value, updated_at"
# The users a quota may be set for: not a deleted one, not one waiting to be.
_QUOTA_USER_STATUSES = ("invited", "active")

_METRICS = tuple(QuotaMetric)
_PERIODS = tuple(QuotaPeriod)

# The statements of the admission, as constants: ``tests/test_connections_plan.py``
# plans them (an index is only useful if the statement, as a prepared statement with
# bound values, can use it).
TASK_LOCK_SQL = (
    "SELECT state, attempt, retry_count, created_by, project_id FROM tasks"
    " WHERE id = %(task)s FOR SHARE"
)
CONNECTION_LOCK_SQL = (
    "SELECT id, secret_handle, status, enabled FROM shared_connections"
    " WHERE kind = %(kind)s FOR SHARE"
)
# The lock of the quota check: every admission of one user and kind takes it.
QUOTA_LOCK_SQL = (
    "SELECT metric, period, limit_value FROM connection_quotas"
    " WHERE user_id = %(user)s AND kind = %(kind)s ORDER BY metric, period FOR UPDATE"
)
CONTINUING_SQL = (
    "SELECT EXISTS (SELECT 1 FROM connection_usage"
    " WHERE task_id = %(task)s AND kind = %(kind)s)"
)
SUMS_SQL = (
    "SELECT count(*), count(DISTINCT task_id),"
    " COALESCE(sum(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0),"
    " COALESCE(sum(duration_ms), 0) FROM connection_usage"
    " WHERE user_id = %(user)s AND kind = %(kind)s AND started_at >= %(since)s"
)


@dataclass(frozen=True, slots=True)
class Admitted:
    """The call may start: its usage row exists (``in_flight``) and the handle of the
    credential to resolve. The handle is read here, once, and goes to the resolver
    and nowhere else; ``__repr__`` does not show it."""

    usage_id: uuid.UUID
    connection_id: uuid.UUID
    project_id: uuid.UUID
    secret_handle: str

    def __repr__(self) -> str:
        return f"Admitted(usage_id={self.usage_id})"


@dataclass(frozen=True, slots=True)
class Refused:
    """The call may not start; nothing was written. ``metric`` / ``period`` /
    ``resets_at`` are set for ``QUOTA_EXCEEDED`` only; ``project_id`` when the task
    was found (it belongs to the user)."""

    reason: RefusalReason
    metric: QuotaMetric | None = None
    period: QuotaPeriod | None = None
    resets_at: datetime | None = None
    project_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class StatusChange:
    """A health verdict was stored: which connection and what it was before."""

    connection_id: uuid.UUID
    previous: ConnectionStatus


@dataclass(frozen=True, slots=True)
class _Sums:
    requests: int
    tasks: int
    tokens: int
    runtime_ms: int

    def used(self, metric: QuotaMetric) -> int:
        match metric:
            case QuotaMetric.REQUESTS:
                return self.requests
            case QuotaMetric.TASKS:
                return self.tasks
            case QuotaMetric.TOKENS:
                return self.tokens
            case _:
                return self.runtime_ms // 1000  # whole seconds, rounded down


def _limit_of(value: int | None) -> int | Unlimited:
    return UNLIMITED if value is None else int(value)


def _info(row: tuple) -> ConnectionInfo:
    id_, kind, status, enabled, checked_at, created_at, updated_at = row
    return ConnectionInfo(
        id_,
        ConnectionKind(kind),
        ConnectionStatus(status),
        enabled,
        checked_at,
        created_at,
        updated_at,
    )


def _quota(row: tuple) -> Quota:
    user_id, kind, metric, period, limit_value, updated_at = row
    return Quota(
        user_id,
        ConnectionKind(kind),
        QuotaMetric(metric),
        QuotaPeriod(period),
        _limit_of(limit_value),
        updated_at,
    )


def usage_record(row: tuple) -> UsageRecord:
    (
        id_,
        user_id,
        task_id,
        project_id,
        kind,
        model,
        purpose,
        status,
        failure_code,
        input_tokens,
        output_tokens,
        started_at,
        finished_at,
        duration_ms,
    ) = row
    return UsageRecord(
        id_,
        user_id,
        task_id,
        project_id,
        ConnectionKind(kind),
        model,
        UsagePurpose(purpose),
        UsageStatus(status),
        None if failure_code is None else FailureCode(failure_code),
        input_tokens,
        output_tokens,
        started_at,
        finished_at,
        duration_ms,
    )


class ConnectionStore:
    """The SQL of the module. Arguments are validated by the service, not here."""

    def __init__(
        self,
        database: Database,
        *,
        timeout_seconds: float = DEFAULT_DATABASE_TIMEOUT_SECONDS,
        zone: tzinfo = UTC,
        clock: Callable[[], datetime] | None = None,
        allow_explicit_clock: bool = False,
    ) -> None:
        """Production code passes ``database`` (and perhaps a time zone) only.

        ``clock`` is the TEST SEAM of the module docstring: accepted only with
        ``allow_explicit_clock=True`` and then it must be callable. A ``clock`` that
        returns anything but a timezone-aware ``datetime`` makes the calling method
        raise ``InvalidConnectionInputError("clock", ...)``.
        """
        if not isinstance(database, Database):
            raise InvalidConnectionInputError("database", InputProblem.WRONG_TYPE)
        validate_seconds(
            "timeout_seconds", timeout_seconds, MAX_DATABASE_TIMEOUT_SECONDS
        )
        if not isinstance(zone, tzinfo):
            raise InvalidConnectionInputError("zone", InputProblem.WRONG_TYPE)
        validate_bool("allow_explicit_clock", allow_explicit_clock)
        if clock is not None:
            if not allow_explicit_clock:
                raise InvalidConnectionInputError("clock", InputProblem.NOT_ALLOWED)
            if not callable(clock):
                raise InvalidConnectionInputError("clock", InputProblem.NOT_CALLABLE)
        self._database = database
        self._timeout = float(timeout_seconds)
        self._zone = zone
        self._clock = clock

    # --- the clock ------------------------------------------------------------------

    def _clock_cte(self) -> tuple[str, dict[str, Any]]:
        """``clock AS MATERIALIZED (SELECT <instant> AS ts)`` and its parameters."""
        if self._clock is None:
            return "clock AS MATERIALIZED (SELECT clock_timestamp() AS ts)", {}
        return (
            "clock AS MATERIALIZED (SELECT %(now)s::timestamptz AS ts)",
            {"now": self._test_instant()},
        )

    def _test_instant(self) -> datetime:
        assert self._clock is not None
        now = self._clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise InvalidConnectionInputError("clock", InputProblem.WRONG_TYPE)
        return now

    async def _instant(self, connection: psycopg.AsyncConnection) -> datetime:
        """The current instant: read once, by the caller, after its locks are held."""
        if self._clock is not None:
            return self._test_instant()
        cursor = await connection.execute("SELECT clock_timestamp()")
        row = await cursor.fetchone()
        assert row is not None
        return row[0]

    async def _fetch(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        """A read (or a write whose late application would be harmless: ``settle``)
        on its own autocommit connection."""
        return await self._database.fetch_abortable(
            sql, params, timeout_seconds=self._timeout
        )

    async def _write(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        """A write that must be all or nothing when the time runs out.

        One statement in one TRANSACTION (``transact_abortable``): at the deadline
        the caller stops waiting AND the server is told to give up (``lock_timeout``,
        ``statement_timeout``, ``transaction_timeout``), and the abandoned
        transaction never receives its ``COMMIT``. A single autocommit statement
        (``fetch_abortable``) would be applied by the server as soon as the lock it
        waits for is released, after the caller has long been told it did not
        finish. So ``ConnectionBusyError`` means "nothing was changed" (only an
        abort during the COMMIT itself leaves the outcome unknown).
        """

        async def work(connection: psycopg.AsyncConnection) -> list[tuple]:
            cursor = await connection.execute(sql, params)
            return await cursor.fetchall() if cursor.description else []

        return await self._database.transact_abortable(
            work, timeout_seconds=self._timeout
        )

    # --- connections ----------------------------------------------------------------

    async def insert_connection(
        self, kind: ConnectionKind, handle: str
    ) -> ConnectionInfo | None:
        """Create the connection of ``kind`` (status ``unavailable`` until a health
        check says otherwise); ``None`` if one exists."""
        clock, params = self._clock_cte()
        rows = await self._write(
            f"WITH {clock} INSERT INTO shared_connections"
            " (kind, secret_handle, status, enabled, created_at, updated_at)"
            " SELECT %(kind)s, %(handle)s, 'unavailable', true, ts, ts FROM clock"
            " ON CONFLICT (kind) DO NOTHING"
            f" RETURNING {_CONNECTION_COLUMNS}",
            {**params, "kind": kind.value, "handle": handle},
        )
        return _info(rows[0]) if rows else None

    async def replace_handle(
        self, kind: ConnectionKind, handle: str
    ) -> ConnectionInfo | None:
        """Point the connection at a new credential. Its status goes back to
        ``unavailable`` and the last check is forgotten: the new credential has not
        been verified. ``None`` if there is no connection."""
        clock, params = self._clock_cte()
        rows = await self._write(
            f"WITH {clock} UPDATE shared_connections SET secret_handle = %(handle)s,"
            " status = 'unavailable', checked_at = NULL,"
            " updated_at = (SELECT ts FROM clock) WHERE kind = %(kind)s"
            f" RETURNING {_CONNECTION_COLUMNS}",
            {**params, "kind": kind.value, "handle": handle},
        )
        return _info(rows[0]) if rows else None

    async def set_enabled(
        self, kind: ConnectionKind, enabled: bool
    ) -> ConnectionInfo | None:
        clock, params = self._clock_cte()
        rows = await self._write(
            f"WITH {clock} UPDATE shared_connections SET enabled = %(enabled)s,"
            " updated_at = (SELECT ts FROM clock) WHERE kind = %(kind)s"
            f" RETURNING {_CONNECTION_COLUMNS}",
            {**params, "kind": kind.value, "enabled": enabled},
        )
        return _info(rows[0]) if rows else None

    async def delete_connection(self, kind: ConnectionKind) -> bool:
        rows = await self._write(
            "DELETE FROM shared_connections WHERE kind = %(kind)s RETURNING id",
            {"kind": kind.value},
        )
        return bool(rows)

    async def get_connection(self, kind: ConnectionKind) -> ConnectionInfo | None:
        rows = await self._fetch(
            f"SELECT {_CONNECTION_COLUMNS} FROM shared_connections"
            " WHERE kind = %(kind)s",
            {"kind": kind.value},
        )
        return _info(rows[0]) if rows else None

    async def list_connections(self) -> tuple[ConnectionInfo, ...]:
        rows = await self._fetch(
            f"SELECT {_CONNECTION_COLUMNS} FROM shared_connections ORDER BY kind", {}
        )
        by_kind = {row[1]: _info(row) for row in rows}
        return tuple(
            by_kind[kind.value] for kind in ConnectionKind if kind.value in by_kind
        )

    async def read_handle(self, kind: ConnectionKind) -> str | None:
        """The credential handle of ``kind`` (for the health check), or ``None``."""
        rows = await self._fetch(
            "SELECT secret_handle FROM shared_connections WHERE kind = %(kind)s",
            {"kind": kind.value},
        )
        return rows[0][0] if rows else None

    async def record_health(
        self, kind: ConnectionKind, handle: str, status: ConnectionStatus
    ) -> StatusChange | None:
        """Store a verdict about the credential ``handle`` of ``kind``.

        It applies only while the connection still points at that very handle: a
        verdict about a credential that has been replaced since is dropped
        (``None``), so a slow check cannot mark the new credential expired.
        """
        clock, params = self._clock_cte()
        rows = await self._write(
            f"WITH {clock}, old AS (SELECT id, status FROM shared_connections"
            " WHERE kind = %(kind)s AND secret_handle = %(handle)s FOR UPDATE)"
            " UPDATE shared_connections c SET status = %(status)s,"
            " checked_at = (SELECT ts FROM clock),"
            " updated_at = (SELECT ts FROM clock) FROM old WHERE c.id = old.id"
            " RETURNING c.id, old.status",
            {**params, "kind": kind.value, "handle": handle, "status": status.value},
        )
        if not rows:
            return None
        return StatusChange(rows[0][0], ConnectionStatus(rows[0][1]))

    # --- quotas ---------------------------------------------------------------------

    async def upsert_quota(
        self,
        user_id: uuid.UUID,
        kind: ConnectionKind,
        metric: QuotaMetric,
        period: QuotaPeriod,
        limit: int | Unlimited,
    ) -> Quota | None:
        """Set the limit; ``None`` when the user does not exist or is deleted."""
        clock, params = self._clock_cte()
        rows = await self._write(
            f"WITH {clock} INSERT INTO connection_quotas (user_id, kind, metric,"
            " period, limit_value, created_at, updated_at)"
            " SELECT %(user)s, %(kind)s, %(metric)s, %(period)s, %(limit)s, ts, ts"
            " FROM clock WHERE EXISTS (SELECT 1 FROM users WHERE id = %(user)s"
            " AND status = ANY(%(statuses)s))"
            " ON CONFLICT (user_id, kind, metric, period) DO UPDATE SET"
            " limit_value = EXCLUDED.limit_value, updated_at = EXCLUDED.updated_at"
            f" RETURNING {_QUOTA_COLUMNS}",
            {
                **params,
                "user": user_id,
                "kind": kind.value,
                "metric": metric.value,
                "period": period.value,
                "limit": None if isinstance(limit, Unlimited) else limit,
                "statuses": list(_QUOTA_USER_STATUSES),
            },
        )
        return _quota(rows[0]) if rows else None

    async def delete_quota(
        self,
        user_id: uuid.UUID,
        kind: ConnectionKind,
        metric: QuotaMetric,
        period: QuotaPeriod,
    ) -> bool:
        rows = await self._write(
            "DELETE FROM connection_quotas WHERE user_id = %(user)s AND kind = %(kind)s"
            " AND metric = %(metric)s AND period = %(period)s RETURNING 1",
            {
                "user": user_id,
                "kind": kind.value,
                "metric": metric.value,
                "period": period.value,
            },
        )
        return bool(rows)

    async def quota_status(
        self, user_id: uuid.UUID, kind: ConnectionKind | None
    ) -> tuple[QuotaUsage, ...]:
        """Every configured quota of the user with what its current window has used.

        One transaction, one instant (the database's): the sums and the windows are
        those of the same moment. Ordered by kind, metric and period (declaration
        order).
        """
        kinds = None if kind is None else kind.value

        async def work(connection: psycopg.AsyncConnection) -> tuple[QuotaUsage, ...]:
            now = await self._instant(connection)
            cursor = await connection.execute(
                "SELECT kind, metric, period, limit_value FROM connection_quotas"
                " WHERE user_id = %(user)s AND (%(kind)s::text IS NULL"
                " OR kind = %(kind)s)",
                {"user": user_id, "kind": kinds},
            )
            configured = sorted(
                (
                    (ConnectionKind(k), QuotaMetric(m), QuotaPeriod(p), limit)
                    for k, m, p, limit in await cursor.fetchall()
                ),
                key=lambda item: (
                    tuple(ConnectionKind).index(item[0]),
                    _METRICS.index(item[1]),
                    _PERIODS.index(item[2]),
                ),
            )
            sums: dict[tuple[ConnectionKind, datetime], _Sums] = {}
            result = []
            for item_kind, metric, period, limit in configured:
                since = window_start(period, now, self._zone)
                if (item_kind, since) not in sums:
                    sums[(item_kind, since)] = await self._sums(
                        connection, user_id, item_kind, since
                    )
                result.append(
                    QuotaUsage(
                        item_kind,
                        metric,
                        period,
                        _limit_of(limit),
                        sums[(item_kind, since)].used(metric),
                        since,
                        window_end(period, now, self._zone),
                    )
                )
            return tuple(result)

        return await self._database.transact_abortable(
            work, timeout_seconds=self._timeout
        )

    # --- usage ----------------------------------------------------------------------

    async def list_usage(
        self, user_id: uuid.UUID, kind: ConnectionKind | None, limit: int, offset: int
    ) -> tuple[UsageRecord, ...]:
        """The user's usage records, newest first (``started_at``, then ``id``)."""
        rows = await self._fetch(
            f"SELECT {_USAGE_COLUMNS} FROM connection_usage WHERE user_id = %(user)s"
            " AND (%(kind)s::text IS NULL OR kind = %(kind)s)"
            " ORDER BY started_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
            {
                "user": user_id,
                "kind": None if kind is None else kind.value,
                "limit": limit,
                "offset": offset,
            },
        )
        return tuple(usage_record(row) for row in rows)

    async def _sums(
        self,
        connection: psycopg.AsyncConnection,
        user_id: uuid.UUID,
        kind: ConnectionKind,
        since: datetime,
    ) -> _Sums:
        cursor = await connection.execute(
            SUMS_SQL, {"user": user_id, "kind": kind.value, "since": since}
        )
        row = await cursor.fetchone()
        assert row is not None
        return _Sums(*(int(value) for value in row))

    async def admit(
        self,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        run: TaskRun,
        kind: ConnectionKind,
        model: str,
        purpose: UsagePurpose,
    ) -> Admitted | Refused:
        """Decide whether the call may start and, if so, record that it did.

        See the module docstring for the steps and their order. A call for a
        task that has used this connection before is a CONTINUING task: it is
        admitted whatever the quotas say (a running task is not cut off, Decision
        0016, section 3) and still recorded. A call that starts a new task needs
        at least one quota for the user and kind (else ``QUOTA_NOT_CONFIGURED``:
        no quota is not unlimited) and every limited quota must be below its limit.
        """

        async def work(connection: psycopg.AsyncConnection) -> Admitted | Refused:
            cursor = await connection.execute(TASK_LOCK_SQL, {"task": task_id})
            task = await cursor.fetchone()
            if task is None or task[3] != user_id:  # unknown, or somebody else's
                return Refused(RefusalReason.TASK_NOT_FOUND)
            state, attempt, retry_count, _, project_id = task
            activity = activity_of(state, attempt, retry_count, run)
            if activity is not TaskActivity.ACTIVE:
                reason = {
                    TaskActivity.ENDED: RefusalReason.TASK_ENDED,
                    TaskActivity.SUPERSEDED: RefusalReason.TASK_SUPERSEDED,
                }.get(activity, RefusalReason.TASK_NOT_FOUND)
                return Refused(reason, project_id=project_id)

            cursor = await connection.execute(CONNECTION_LOCK_SQL, {"kind": kind.value})
            found = await cursor.fetchone()
            if (
                found is None
                or not found[3]
                or found[2] != ConnectionStatus.CONNECTED.value
            ):
                return Refused(
                    RefusalReason.CONNECTION_UNAVAILABLE, project_id=project_id
                )
            connection_id, handle = found[0], found[1]

            # The lock of the quota check. Every admission of this user and kind
            # takes it, in this order, and holds it until it commits.
            cursor = await connection.execute(
                QUOTA_LOCK_SQL, {"user": user_id, "kind": kind.value}
            )
            quotas = sorted(
                (
                    (QuotaMetric(metric), QuotaPeriod(period), limit)
                    for metric, period, limit in await cursor.fetchall()
                ),
                key=lambda item: (_METRICS.index(item[0]), _PERIODS.index(item[1])),
            )
            cursor = await connection.execute(
                CONTINUING_SQL, {"task": task_id, "kind": kind.value}
            )
            continuing = (await cursor.fetchone())[0]  # type: ignore[index]

            now = await self._instant(connection)  # after the locks
            if not continuing:
                if not quotas:
                    return Refused(
                        RefusalReason.QUOTA_NOT_CONFIGURED, project_id=project_id
                    )
                sums: dict[datetime, _Sums] = {}
                for metric, period, limit in quotas:
                    if limit is None:  # Unlimited
                        continue
                    since = window_start(period, now, self._zone)
                    if since not in sums:
                        sums[since] = await self._sums(connection, user_id, kind, since)
                    if sums[since].used(metric) >= limit:
                        return Refused(
                            RefusalReason.QUOTA_EXCEEDED,
                            metric,
                            period,
                            window_end(period, now, self._zone),
                            project_id,
                        )

            usage_id = uuid.uuid4()
            await connection.execute(
                "INSERT INTO connection_usage (id, user_id, task_id, project_id, kind,"
                " model, purpose, status, started_at) VALUES (%(id)s, %(user)s,"
                " %(task)s, %(project)s, %(kind)s, %(model)s, %(purpose)s,"
                " 'in_flight', %(now)s)",
                {
                    "id": usage_id,
                    "user": user_id,
                    "task": task_id,
                    "project": project_id,
                    "kind": kind.value,
                    "model": model,
                    "purpose": purpose.value,
                    "now": now,
                },
            )
            return Admitted(usage_id, connection_id, project_id, handle)

        return await self._database.transact_abortable(
            work, timeout_seconds=self._timeout
        )

    async def settle(
        self,
        usage_id: uuid.UUID,
        status: UsageStatus,
        failure: FailureCode | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> UsageRecord | None:
        """End an ``in_flight`` usage row: its outcome, tokens, end and duration.

        One statement, so it is atomic, and conditional on ``status = 'in_flight'``:
        of any number of settlements of the same call exactly one changes the row
        and the others match nothing (``None``). The duration is measured by the
        database's clock, from the ``started_at`` the admission wrote to now, in
        milliseconds and never negative.
        """
        clock, params = self._clock_cte()
        rows = await self._fetch(
            f"WITH {clock} UPDATE connection_usage SET status = %(status)s,"
            " failure_code = %(failure)s, input_tokens = %(input)s,"
            " output_tokens = %(output)s, finished_at = GREATEST("
            "(SELECT ts FROM clock), started_at),"
            " duration_ms = GREATEST(0, floor(extract(epoch FROM"
            " ((SELECT ts FROM clock) - started_at)) * 1000))::bigint"
            " WHERE id = %(id)s AND status = 'in_flight'"
            f" RETURNING {_USAGE_COLUMNS}",
            {
                **params,
                "id": usage_id,
                "status": status.value,
                "failure": None if failure is None else failure.value,
                "input": input_tokens,
                "output": output_tokens,
            },
        )
        return usage_record(rows[0]) if rows else None
