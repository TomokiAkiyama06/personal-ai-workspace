"""The Recovery backup in the split-role deployment (real PostgreSQL).

The backup connects with ``PAW_DATABASE_URL``, the NON-superuser application
role: PAW-047 adds no migration and grants nothing, and SELECT on the tables it
reads plus INSERT / SELECT on ``audit_events`` are all it needs. These tests run
the end-to-end PostgreSQL tests with the backup as that role (the restore stays
the table owner, ``PAW_MIGRATION_DATABASE_URL``), and check that the role cannot
restore: it may not insert users. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest

from sqlalchemy.exc import ProgrammingError

from paw_backend.recovery import RecoveryRestorer

from . import test_recovery_postgres
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


class RecoveryAsAppRoleTest(test_recovery_postgres.RecoveryPostgresTest):
    def backup_url(self) -> str:
        return role_url(APP_ROLE)

    async def test_the_application_role_cannot_restore(self) -> None:
        await self.back_up()
        clone = self.world.clone()
        self.clean_tables()
        restorer = RecoveryRestorer(
            self.new_database(role_url(APP_ROLE)),
            clone,
            protected_homes=self.world.homes,
            clock=self.clock,
        )
        # It cannot even read the schema version (the command: exit 2).
        with self.assertRaises(ProgrammingError):
            await restorer.run(apply=True)
        self.assertEqual([], self.rows("users"))
        self.assertEqual([], self.rows("memories"))


# Not collected twice under its own name in this module.
del test_recovery_postgres
