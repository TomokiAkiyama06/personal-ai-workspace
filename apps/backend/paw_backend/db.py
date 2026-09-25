"""PostgreSQL access: metadata base, lazy async engine and readiness check."""

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any

import psycopg
from sqlalchemy import MetaData
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from paw_backend.config import Settings

logger = logging.getLogger(__name__)

# How long the SERVER keeps an abortable transaction (and so whatever it waits
# for, and the locks it holds) after the caller's own deadline, once the caller
# has abandoned it (see ``Database.transact_abortable``).
_SERVER_GRACE_SECONDS = 1.0

# Constraint names must be deterministic so that Alembic can drop / alter them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model; also Alembic's ``target_metadata``."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class DatabaseStatus(StrEnum):
    """Stable, credential-free result of a database readiness check."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


class DatabaseNotConfiguredError(RuntimeError):
    """``PAW_DATABASE_URL`` is not set."""


class DatabaseDisposedError(RuntimeError):
    """``dispose()`` ran (or is running): abortable work is not started.

    Raised by ``fetch_abortable`` / ``run_abortable`` to a call that was waiting
    for a connection slot when ``dispose()`` began, or that started while it ran.
    Nothing of the call was executed.
    """


class _Slots:
    """A bounded counting gate whose waiters can be failed all at once.

    Like ``asyncio.BoundedSemaphore`` (first come, first served; a cancelled
    waiter neither keeps nor loses a slot; releasing a slot nobody holds is an
    error), plus ``fail_waiters``, which ``Database.dispose()`` uses to end
    every wait with an error instead of leaving the waiters to start work once a
    running call gives its slot back.
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._in_use = 0  # slots taken, including one handed to a waiter
        # The waiting callers, oldest first (a dict is an ordered set here).
        self._waiters: dict[asyncio.Future[None], None] = {}

    @property
    def free(self) -> int:
        return self._size - self._in_use

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self) -> None:
        # A slot that is handed to a waiter stays "in use", so there is never a
        # free slot while somebody waits: a new call cannot overtake a waiter.
        if self._in_use < self._size:
            self._in_use += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters[waiter] = None
        try:
            await waiter
        except asyncio.CancelledError:
            if (
                waiter.done()
                and not waiter.cancelled()
                and waiter.exception() is None  # not one that was failed
            ):
                # The slot was handed to this waiter just before the
                # cancellation: nobody will use it, so give it back (once).
                self.release()
            raise
        finally:
            self._waiters.pop(waiter, None)

    def release(self) -> None:
        if self._in_use == 0:
            raise ValueError("A slot that nobody holds was released")
        while self._waiters:
            waiter = next(iter(self._waiters))
            del self._waiters[waiter]
            if not waiter.done():  # not one that was cancelled a moment ago
                waiter.set_result(None)  # the slot moves to it: still in use
                return
        self._in_use -= 1

    def fail_waiters(self, make_error: Callable[[], Exception]) -> None:
        """Fail every waiting caller with its own error; held slots are kept."""
        waiters = list(self._waiters)
        self._waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_exception(make_error())


class Database:
    """Owns the SQLAlchemy async engine.

    The engine is created on first use and does not connect until a query is
    run, so the application starts (and answers liveness probes) while
    PostgreSQL is down.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        # Readiness probes that are running or being stopped, and the driver
        # connection each one is using (see `check`).
        self._probes: set[asyncio.Task[Any]] = set()
        self._probe_connections: dict[asyncio.Task[Any], psycopg.AsyncConnection] = {}
        # Single flight: concurrent `check()` calls share one probe, and its
        # result is reused for `database_readiness_cache_seconds`.
        self._flight: asyncio.Task[DatabaseStatus] | None = None
        self._recent: tuple[float, DatabaseStatus] | None = None
        # Abortable statements use connections outside the pool: at most as many
        # at once as the pool would allow, so a burst cannot exhaust the server.
        self._abortable_slots = _Slots(settings.database_pool_size)
        # `dispose()`: how many are running, and how many have started. A call
        # that waited for a slot across the start of one must not run (see
        # `_acquire_slot`).
        self._disposing = 0
        self._disposals = 0
        # The engine of `run_abortable`: no pool, every transaction connects
        # through `_connect_abortable`, which registers the driver connection.
        self._abortable_engine: AsyncEngine | None = None
        self._abortable_sessions: async_sessionmaker[AsyncSession] | None = None

    @property
    def configured(self) -> bool:
        return self._settings.database_url is not None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            if self._settings.database_url is None:
                raise DatabaseNotConfiguredError("PAW_DATABASE_URL is not set")
            self._engine = create_async_engine(
                self._settings.database_url.get_secret_value(),
                pool_size=self._settings.database_pool_size,
                pool_pre_ping=True,
                connect_args={"connect_timeout": self._connect_timeout},
            )
        return self._engine

    @property
    def _connect_timeout(self) -> int:
        return max(1, round(self._settings.database_timeout_seconds))

    def _connect_kwargs(self, *, autocommit: bool) -> dict[str, Any]:
        """The arguments for ``psycopg.AsyncConnection.connect`` of a dedicated one.

        The same translation SQLAlchemy applies before it calls psycopg. The URL
        may already carry `connect_timeout` (or `autocommit`), so the caller's
        own values are merged in and win instead of being passed a second time.
        """
        url = make_url(self._settings.database_url.get_secret_value())
        _, kwargs = url.get_dialect()().create_connect_args(url)
        kwargs.update(autocommit=autocommit, connect_timeout=self._connect_timeout)
        return kwargs

    def session(self) -> AsyncSession:
        """Return a new session; use it as ``async with database.session()``."""
        if self._sessions is None:
            self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        return self._sessions()

    async def _ping(self) -> None:
        """One ``SELECT 1`` on a dedicated connection that ``_abort`` can drop."""
        await self._query("SELECT 1")

    async def _query(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> list[tuple]:
        """Run one short statement on a dedicated, abortable connection."""

        async def statement(connection: psycopg.AsyncConnection) -> list[tuple]:
            cursor = await connection.execute(sql, params)
            return await cursor.fetchall() if cursor.description else []

        return await self._run(statement)

    async def _run[T](
        self, work: Callable[[psycopg.AsyncConnection], Awaitable[T]]
    ) -> T:
        """Run ``work`` on a dedicated connection that ``_abort`` can drop.

        The connection deliberately does not come from the pool: it must not
        occupy a pool slot while the server is stalled, and it needs to be owned
        by this task from the first byte so that it can be torn down (see
        ``_abort``).
        """
        connection = await psycopg.AsyncConnection.connect(
            **self._connect_kwargs(autocommit=True)
        )
        probe = asyncio.current_task()
        self._probe_connections[probe] = connection
        try:
            return await work(connection)
        finally:
            self._probe_connections.pop(probe, None)
            await connection.close()

    async def _acquire_slot(self) -> None:
        """Wait for a free slot of the abortable connections; take it.

        The wait is part of the abortable work: cancelling the caller ends it
        without taking a slot, and ``dispose()`` fails it with
        ``DatabaseDisposedError`` (no slot is kept). A call is also refused
        while ``dispose()`` runs, and one that was given a slot just before
        ``dispose()`` began gives it back, so that no call starts work after
        ``dispose()`` has taken stock of what to abort.
        """
        if self._disposing:
            raise DatabaseDisposedError("The database is being disposed")
        disposals = self._disposals
        await self._abortable_slots.acquire()
        if self._disposals != disposals:
            self._abortable_slots.release()
            raise DatabaseDisposedError("The database was disposed while waiting")

    async def fetch_abortable(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> list[tuple]:
        """Run a short ``sql`` that never outlives its limit or ``dispose()``.

        For diagnostics and for writes that must fail closed on time (the audit
        row): the statement runs on its own autocommit connection, and when
        ``timeout_seconds`` (default ``database_timeout_seconds``) passes, the
        caller is cancelled, or the database is disposed, the connection's socket is
        shut down instead of asking a possibly stalled server to cancel the
        query (see ``_abort``). The limit is ONE deadline for the whole call:
        waiting for a free slot (see ``__init__``) and running the statement
        share it, so a call never takes longer than ``timeout_seconds``. Raises
        ``TimeoutError`` at the deadline, ``DatabaseDisposedError`` if
        ``dispose()`` ran while it waited (see ``_acquire_slot``) and the
        driver's error if the connection fails. ``params`` are bound by the
        driver (``%(name)s`` placeholders), never formatted into ``sql``.

        A write that is aborted may or may not have been committed: the caller
        learns only that it did not finish in time.
        """

        async def statement(connection: psycopg.AsyncConnection, deadline: float):
            cursor = await connection.execute(sql, params)
            return await cursor.fetchall() if cursor.description else []

        return await self._abortable(statement, timeout_seconds)

    async def transact_abortable[T](
        self,
        work: Callable[[psycopg.AsyncConnection], Awaitable[T]],
        *,
        timeout_seconds: float | None = None,
    ) -> T:
        """Run ``work(connection)`` as ONE transaction that never outlives its limit.

        For a write that needs several statements in one transaction (a lock,
        then a read, then the change) and must still fail closed on time: it has
        the guarantees of ``fetch_abortable`` (its own connection outside the
        pool, ONE deadline that includes the wait for a free slot, the socket
        shut down at the deadline, when the caller is cancelled or on
        ``dispose()``; ``TimeoutError`` at the deadline), and ``work`` runs
        between ``BEGIN`` and ``COMMIT`` of a transaction of its own: it commits
        when ``work`` returns and rolls back when it raises. ``work`` uses the
        connection it is given (``await connection.execute(sql, params)``) and
        must not keep it.

        A transaction that is aborted is all or nothing. The server sees only a
        closed connection: the statement it was running finishes (or fails), it
        never receives the next statement or the ``COMMIT``, and the transaction
        is rolled back. Only an abort during the ``COMMIT`` itself leaves the
        outcome unknown to the caller (which then learns only that it did not
        finish in time).

        The server is also told to give up, and the limit is on the WHOLE
        transaction: ``transaction_timeout`` (``SET LOCAL``; a setting since
        PostgreSQL 17, but the Tool Broker path requires PostgreSQL 18 or newer,
        decided by the human on 2026-09-25, see Decision 0006) is set to the
        time that is left plus ``_SERVER_GRACE_SECONDS``, and the server ends
        the session when it runs out, whatever the transaction is doing (a
        statement waiting on a lock, the pause between two statements, the
        ``COMMIT``). A statement that is
        waiting on a lock when the caller aborts is not woken by the closed
        socket (the server notices it only when it has something to send), so
        without a limit the abandoned backend would wait for the lock for as long
        as its holder takes, and keep the locks it already has. Limits that are
        set once for each *statement* (``lock_timeout`` / ``statement_timeout``
        from the full time left) do not do this: a later statement started when
        most of the deadline had gone would be granted the whole limit again, and
        outlive the caller by nearly that much. They are still set to the same
        value, as a backstop for the one thing the transaction limit does not
        cover (the server ignores the longer of it and ``statement_timeout``, so
        they never shorten it). The grace keeps the caller's own deadline first,
        so the caller always sees ``TimeoutError``. The timer starts at the
        ``SET LOCAL``, the first statement of the transaction, with the time
        that is left at that moment.

        A server older than 17 does not know ``transaction_timeout``: the
        transaction then fails at its first statement (fail closed) instead of
        running with a weaker limit. 17 knows it but is neither tested nor
        supported (the requirement is 18 or newer); the version is not checked.
        """
        loop = asyncio.get_running_loop()

        async def transaction(connection: psycopg.AsyncConnection, deadline: float):
            async with connection.transaction():
                server_limit = max(0.0, deadline - loop.time()) + _SERVER_GRACE_SECONDS
                milliseconds = str(max(1, round(server_limit * 1000)))
                await connection.execute(
                    "SELECT set_config('transaction_timeout', %(ms)s, true),"
                    " set_config('lock_timeout', %(ms)s, true),"
                    " set_config('statement_timeout', %(ms)s, true)",
                    {"ms": milliseconds},
                )
                return await work(connection)

        return await self._abortable(transaction, timeout_seconds)

    async def _abortable[T](
        self,
        work: Callable[[psycopg.AsyncConnection, float], Awaitable[T]],
        timeout_seconds: float | None,
    ) -> T:
        """``work(connection, deadline)`` on an abortable connection: the shared
        part of ``fetch_abortable`` and ``transact_abortable``. ``deadline`` is
        the absolute ``loop.time()`` at which the call is aborted."""
        if not self.configured:
            raise DatabaseNotConfiguredError("PAW_DATABASE_URL is not set")
        limit = (
            self._settings.database_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        # ONE deadline for the whole call: waiting for a free slot and running
        # the statement share the limit (the query gets only what is left).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        await asyncio.wait_for(self._acquire_slot(), limit)
        try:
            query = asyncio.create_task(
                self._run(lambda connection: work(connection, deadline))
            )
            self._probes.add(query)
            query.add_done_callback(self._probes.discard)
            # Retrieve the outcome so that asyncio does not log it as unhandled.
            query.add_done_callback(lambda task: task.cancelled() or task.exception())
            try:
                await asyncio.wait({query}, timeout=max(0.0, deadline - loop.time()))
            finally:
                if not query.done():  # timed out, or this caller was cancelled
                    self._abort(query)
            if not query.done():
                raise TimeoutError
            return query.result()
        finally:
            self._abortable_slots.release()

    async def execute_abortable(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """``fetch_abortable`` for a statement whose rows are not needed."""
        await self.fetch_abortable(sql, params, timeout_seconds=timeout_seconds)

    async def run_abortable[T](self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Run ``work(session)`` in ONE transaction that can be aborted at once.

        For a unit of several statements that must not outlive the caller or
        the database (the Research Scratch janitor's purge, PAW-050), where
        ``fetch_abortable`` (one autocommit statement) does not fit. Like it,
        the transaction runs on a dedicated connection outside the pool, in a
        task of its own, and the caller only waits for it. When the caller is
        cancelled, or the database is disposed, the connection's socket is shut
        down instead of asking a possibly stalled server to cancel the query
        (see ``_abort``): the statement in flight fails at once, the server
        rolls the transaction back, and the caller's cancellation is not held
        up. The connection is registered from the moment it exists, so the
        statements SQLAlchemy runs on a new connection are covered too.

        ``work`` gets an ``AsyncSession`` that is already in a transaction; a
        normal return commits, an exception rolls back and is re-raised. It must
        not start a transaction of its own, and its result must not be a
        connection-bound object. An aborted transaction may or may not have been
        committed: the caller learns only that it did not finish. There is no
        deadline of its own (a caller that needs one wraps the call in
        ``asyncio.timeout``: the expiry cancels the caller and so aborts the
        connection). Waiting for a free slot (see ``__init__``) is cancellable
        the same way, and ``dispose()`` fails it with ``DatabaseDisposedError``
        (see ``_acquire_slot``).
        """
        if not self.configured:
            raise DatabaseNotConfiguredError("PAW_DATABASE_URL is not set")
        await self._acquire_slot()
        try:
            transaction = asyncio.create_task(self._run_transaction(work))
            self._probes.add(transaction)
            transaction.add_done_callback(self._probes.discard)
            # Retrieve the outcome so that asyncio does not log it as unhandled.
            transaction.add_done_callback(
                lambda task: task.cancelled() or task.exception()
            )
            try:
                # Unlike awaiting the task, asyncio.wait() does not cancel it
                # when this caller is cancelled: the socket is shut down below.
                await asyncio.wait({transaction})
            finally:
                if not transaction.done():  # this caller was cancelled
                    self._abort(transaction)
            return transaction.result()
        finally:
            self._abortable_slots.release()

    async def _run_transaction[T](
        self, work: Callable[[AsyncSession], Awaitable[T]]
    ) -> T:
        """The task of ``run_abortable``: one session on its own connection."""
        task = asyncio.current_task()
        if self._abortable_sessions is None:
            self._abortable_engine = create_async_engine(
                # Only its dialect is used: `_connect_abortable` connects.
                self._settings.database_url.get_secret_value(),
                poolclass=NullPool,
                async_creator=self._connect_abortable,
            )
            self._abortable_sessions = async_sessionmaker(
                self._abortable_engine, expire_on_commit=False
            )
        try:
            async with self._abortable_sessions() as session, session.begin():
                return await work(session)
        finally:
            self._probe_connections.pop(task, None)

    async def _connect_abortable(self) -> psycopg.AsyncConnection:
        """Open the driver connection of a transaction and register it at once.

        SQLAlchemy runs its own statements on a new connection (the dialect's
        version and settings queries) before it hands it out, and a stalled
        server can leave any of them unanswered: registering here, before
        SQLAlchemy sees the connection, lets ``_abort`` shut it down then too.
        """
        connection = await psycopg.AsyncConnection.connect(
            **self._connect_kwargs(autocommit=False)
        )
        self._probe_connections[asyncio.current_task()] = connection
        return connection

    async def check(self) -> DatabaseStatus:
        """Run ``SELECT 1``. Never raises and never reports connection details.

        ``/health/ready`` may be called by anyone who can reach the server, so
        the number of connections it opens is bounded: concurrent calls share
        one probe (single flight) and the outcome, failures included, is
        reused for ``database_readiness_cache_seconds``. At most one probe
        connection is opened per interval, and a stalled server is probed
        again only after the previous probe timed out.

        Every call returns within ``database_timeout_seconds``, however slowly
        the driver gives up (see ``_abort``).
        """
        if not self.configured:
            return DatabaseStatus.NOT_CONFIGURED
        recent = self._recent
        if (
            recent is not None
            and time.monotonic() - recent[0]
            < self._settings.database_readiness_cache_seconds
        ):
            return recent[1]
        if self._flight is None:
            self._flight = asyncio.create_task(self._run_flight())
        flight = self._flight
        # Unlike awaiting the task, asyncio.wait() leaves it running when this
        # caller is cancelled: the other callers still need its result.
        await asyncio.wait({flight})
        return DatabaseStatus.UNAVAILABLE if flight.cancelled() else flight.result()

    async def _run_flight(self) -> DatabaseStatus:
        try:
            status = await self._probe()
        finally:
            self._flight = None
        self._recent = (time.monotonic(), status)
        return status

    async def _probe(self) -> DatabaseStatus:
        """One probe on a dedicated connection; aborted at the deadline."""
        probe = asyncio.create_task(self._ping())
        self._probes.add(probe)
        probe.add_done_callback(self._probes.discard)
        # Retrieve the outcome so that asyncio does not log it as unhandled.
        probe.add_done_callback(lambda task: task.cancelled() or task.exception())
        try:
            await asyncio.wait({probe}, timeout=self._settings.database_timeout_seconds)
        finally:
            if not probe.done():
                self._abort(probe)
        if not probe.done():
            logger.warning("Database readiness check failed: TimeoutError")
            return DatabaseStatus.UNAVAILABLE
        try:
            probe.result()
        except Exception as error:
            # Log the exception type only: driver messages can name the host,
            # database or user, and this log line is not the place for them.
            logger.warning("Database readiness check failed: %s", type(error).__name__)
            return DatabaseStatus.UNAVAILABLE
        return DatabaseStatus.OK

    def _abort(self, probe: asyncio.Task[Any]) -> None:
        """Make a running probe stop now, without waiting on the server.

        Cancelling a task that is inside a query makes psycopg ask the server
        to cancel it and then wait for the answer, up to about ten seconds. A
        stalled server never answers, and with a libpq older than 17 the
        request is sent from a thread that ``asyncio.run`` waits for at exit.
        Shutting down the connection's socket instead makes the pending query
        fail at once with "server closed the connection". The task is only
        cancelled when it has no connection yet (it is still connecting, where
        cancellation is immediate and there is no query to cancel).
        """
        connection = self._probe_connections.get(probe)
        if connection is not None:
            # Shut down a duplicate of the descriptor: the shutdown applies to
            # the shared socket, while the descriptor libpq owns stays open
            # (and cannot be reused by another socket) until libpq closes it.
            # Aborting twice is normal (a timed-out check, then dispose()); the
            # second shutdown fails with ENOTCONN, which must not fall through
            # to cancel(): the task is already on its way out.
            with contextlib.suppress(OSError, psycopg.Error):
                with socket.socket(fileno=os.dup(connection.pgconn.socket)) as sock:
                    sock.shutdown(socket.SHUT_RDWR)
            return
        probe.cancel()

    async def dispose(self) -> None:
        """Stop readiness probes and abortable work, close the pool, in the budget.

        Abortable work (``fetch_abortable`` and ``run_abortable``) that is still
        running is aborted like a readiness probe. A call that is still waiting
        for a slot fails with ``DatabaseDisposedError`` at once (it has not
        started anything), and so does one that starts while this runs: no
        abortable work begins after the disposal took stock of what to stop.
        Once ``dispose()`` has returned the object is usable again (the engines
        are created on first use).
        """
        self._disposals += 1
        self._disposing += 1
        try:
            self._abortable_slots.fail_waiters(
                lambda: DatabaseDisposedError("The database was disposed while waiting")
            )
            await self._stop_work_and_close_engines()
        finally:
            self._disposing -= 1

    async def _stop_work_and_close_engines(self) -> None:
        probes = set(self._probes)
        for probe in probes:
            self._abort(probe)
        tasks = probes | ({self._flight} if self._flight is not None else set())
        if tasks:
            budget = self._settings.shutdown_timeout_seconds / 2
            _, pending = await asyncio.wait(tasks, timeout=budget)
            for task in pending:  # last resort: cancel what did not fail on its own
                task.cancel()
            if pending:
                _, stuck = await asyncio.wait(pending, timeout=budget)
                if stuck:
                    logger.warning(
                        "%d readiness probe(s) did not stop within the shutdown "
                        "timeout",
                        len(stuck),
                    )
        self._recent = None
        if self._abortable_engine is not None:
            await self._abortable_engine.dispose()
            self._abortable_engine = None
            self._abortable_sessions = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None
