"""Alembic environment: async engine, URL taken from ``PAW_DATABASE_URL``."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Import every module that defines ORM models here, so that autogenerate sees
# them.
from paw_backend.authz import models as authz_models  # noqa: F401
from paw_backend.config import Settings
from paw_backend.db import Base
from paw_backend.identity import models as identity_models  # noqa: F401
from paw_backend.memory import models as memory_models  # noqa: F401
from paw_backend.memory.shared import models as shared_memory_models  # noqa: F401
from paw_backend.research.scratch import models as scratch_models  # noqa: F401
from paw_backend.tasks import models as task_models  # noqa: F401
from paw_backend.tasks.queueing import models as queueing_models  # noqa: F401
from paw_backend.tools import models as tool_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    settings = Settings()
    # A separate migration role (the owner of the schema) is optional.
    url = settings.migration_database_url or settings.database_url
    if url is None:
        raise SystemExit("PAW_DATABASE_URL is not set; cannot run migrations.")
    return url.get_secret_value()


def run_migrations_offline() -> None:
    """Emit SQL without a database connection (``alembic upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
