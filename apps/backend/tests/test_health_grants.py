"""System Health as the application role (real PostgreSQL, split-role deployment).

The monitor runs in the web process, which connects as ``PAW_APP_DATABASE_ROLE``.
Revision ``0066`` grants that role what the store needs on the two health tables
(SELECT / INSERT / UPDATE / DELETE on the samples, SELECT / INSERT / DELETE on the
events), and the sources only read tables it can already read (``tasks``,
``shared_connections``, ``connection_usage``, ``memory_consolidation_queue``,
``audit_events``). These tests run the PostgreSQL tests of System Health as that
role, and check that it cannot rewrite an event. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from paw_backend.db import Database

from . import test_health_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate
from .test_memory_projection_grants import (
    APP_ROLE,
    drop_role,
    owner_sql,
    role_url,
)
from .test_memory_projection_grants import ROLE_PASSWORD as _PASSWORD


def setUpModule():
    if not TEST_DATABASE_URL:
        raise unittest.SkipTest("PAW_TEST_DATABASE_URL is not set")
    asyncio.run(drop_role())
    asyncio.run(
        owner_sql(
            f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
            f" PASSWORD '{_PASSWORD}'"
        )
    )
    migrate("base", downgrade=True)
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_role())


class HealthAsAppRoleTest(test_health_postgres.HealthPostgresTest):
    def database_url(self) -> str:
        return role_url(APP_ROLE)

    async def test_an_event_cannot_be_rewritten(self):
        self.sql(
            "INSERT INTO health_events (component, severity, status)"
            " VALUES ('database', 'info', 'ok')"
        )
        database = Database(make_settings(database_url=role_url(APP_ROLE)))
        self.addAsyncCleanup(database.dispose)
        with self.assertRaises(ProgrammingError):
            async with database.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE health_events SET severity = 'critical'")
                )


# Not collected twice under its own name in this module.
del test_health_postgres
