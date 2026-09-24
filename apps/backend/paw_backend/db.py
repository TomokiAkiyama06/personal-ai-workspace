"""PostgreSQL access: metadata base, lazy async engine and readiness check."""

import asyncio
import contextlib
import logging
import os
import socket
from enum import StrEnum

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
        """One ``SELECT 1`` on a dedicated connection that ``_abort`` can drop.

        The probe deliberately does not use the pool: it must not occupy a pool
        slot while the server is stalled, and it needs to own its connection
        from the first byte so that it can be torn down (see ``_abort``).
        """
        # The same translation SQLAlchemy applies before it calls psycopg.
        url = make_url(self._settings.database_url.get_secret_value())
        _, kwargs = url.get_dialect()().create_connect_args(url)
        connection = await psycopg.AsyncConnection.connect(
            autocommit=True, connect_timeout=self._connect_timeout, **kwargs
        )
        probe = asyncio.current_task()
        self._probe_connections[probe] = connection
        try:
            await connection.execute("SELECT 1")
        finally:
            self._probe_connections.pop(probe, None)
            await connection.close()

    async def check(self) -> DatabaseStatus:
        """Run ``SELECT 1``. Never raises and never reports connection details.

        Returns within ``database_timeout_seconds`` even if the server stalls.
        A probe that is still running at the deadline is aborted in the
        background (see ``_abort``); ``dispose()`` makes sure none outlives
        the application.
        """
        if not self.configured:
            return DatabaseStatus.NOT_CONFIGURED
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
        if probes:
            budget = self._settings.shutdown_timeout_seconds / 2
            _, pending = await asyncio.wait(probes, timeout=budget)
            for probe in pending:  # last resort: cancel what did not fail on its own
                probe.cancel()
            if pending:
                _, stuck = await asyncio.wait(pending, timeout=budget)
                if stuck:
                    logger.warning(
                        "%d readiness probe(s) did not stop within the shutdown "
                        "timeout",
                        len(stuck),
                    )
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None
