"""PostgreSQL access: metadata base, lazy async engine and readiness check."""

import asyncio
import logging
from enum import StrEnum

from sqlalchemy import MetaData, text
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

    @property
    def configured(self) -> bool:
        return self._settings.database_url is not None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            if self._settings.database_url is None:
                raise DatabaseNotConfiguredError("PAW_DATABASE_URL is not set")
            timeout = self._settings.database_timeout_seconds
            self._engine = create_async_engine(
                self._settings.database_url.get_secret_value(),
                pool_size=self._settings.database_pool_size,
                pool_pre_ping=True,
                connect_args={"connect_timeout": max(1, round(timeout))},
            )
        return self._engine

    def session(self) -> AsyncSession:
        """Return a new session; use it as ``async with database.session()``."""
        if self._sessions is None:
            self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        return self._sessions()

    async def check(self) -> DatabaseStatus:
        """Run ``SELECT 1``. Never raises and never reports connection details."""
        if not self.configured:
            return DatabaseStatus.NOT_CONFIGURED
        try:
            async with asyncio.timeout(self._settings.database_timeout_seconds):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except Exception as error:
            # Log the exception type only: driver messages can name the host,
            # database or user, and this log line is not the place for them.
            logger.warning("Database readiness check failed: %s", type(error).__name__)
            return DatabaseStatus.UNAVAILABLE
        return DatabaseStatus.OK

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None
