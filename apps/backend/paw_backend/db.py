"""PostgreSQL access: metadata base, lazy async engine and readiness check."""

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Mapping
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

from paw_backend.config import Settings

logger = logging.getLogger(__name__)

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
        self._probes: set[asyncio.Task[None]] = set()
        self._probe_connections: dict[asyncio.Task[None], psycopg.AsyncConnection] = {}
        # Single flight: concurrent `check()` calls share one probe, and its
        # result is reused for `database_readiness_cache_seconds`.
        self._flight: asyncio.Task[DatabaseStatus] | None = None
        self._recent: tuple[float, DatabaseStatus] | None = None
        # Abortable statements use connections outside the pool: at most as many
        # at once as the pool would allow, so a burst cannot exhaust the server.
        self._abortable_slots = asyncio.Semaphore(settings.database_pool_size)

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
        """Run one short statement on a dedicated, abortable connection.

        The probe deliberately does not use the pool: it must not occupy a pool
        slot while the server is stalled, and it needs to own its connection
        from the first byte so that it can be torn down (see ``_abort``).
        """
        # The same translation SQLAlchemy applies before it calls psycopg. The
        # URL may already carry `connect_timeout` (or `autocommit`), so the
        # probe's own values are merged in and win instead of being passed a
        # second time.
        url = make_url(self._settings.database_url.get_secret_value())
        _, kwargs = url.get_dialect()().create_connect_args(url)
        kwargs.update(autocommit=True, connect_timeout=self._connect_timeout)
        connection = await psycopg.AsyncConnection.connect(**kwargs)
        probe = asyncio.current_task()
        self._probe_connections[probe] = connection
        try:
            cursor = await connection.execute(sql, params)
            return await cursor.fetchall() if cursor.description else []
        finally:
            self._probe_connections.pop(probe, None)
            await connection.close()

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
        ``TimeoutError`` at the deadline and the driver's error if the
        connection fails. ``params`` are bound by the driver (``%(name)s``
        placeholders), never formatted into ``sql``.

        A write that is aborted may or may not have been committed: the caller
        learns only that it did not finish in time.
        """
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
        await asyncio.wait_for(self._abortable_slots.acquire(), limit)
        try:
            query = asyncio.create_task(self._query(sql, params))
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

    def _abort(self, probe: asyncio.Task[None]) -> None:
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
        """Stop readiness probes and close the pool, within the shutdown budget."""
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
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None
