"""Stored notifications as the application role (real PostgreSQL, split-role).

Revision ``0188`` grants ``PAW_APP_DATABASE_ROLE`` SELECT, INSERT, DELETE and
``UPDATE (resolved_at)`` on ``notifications`` and SELECT, INSERT and
``UPDATE (read_at, dismissed_at)`` on ``notification_receipts``. These tests run
the PostgreSQL tests of the store, the System Health producer and the routes as
that role, and check that it cannot rewrite a notification's content or
audience, move a receipt, or delete one. Skipped unless ``PAW_TEST_DATABASE_URL``
is set.
"""

import asyncio
import unittest

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from paw_backend.db import Database

from . import test_notifications_postgres
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


class AsAppRole:
    def database_url(self) -> str:
        return role_url(APP_ROLE)


class StoreAsAppRoleTest(AsAppRole, test_notifications_postgres.NotificationStoreTest):
    async def test_the_role_cannot_rewrite_or_delete_what_it_must_not(self):
        notification = await self.add(key="k:a", recipient_user_id=self.alice.user_id)
        await self.store.mark_read(self.alice.user_id, (), None)
        database = Database(make_settings(database_url=role_url(APP_ROLE)))
        self.addAsyncCleanup(database.dispose)
        for statement in (
            "UPDATE notifications SET key = 'other'",
            "UPDATE notifications SET params = '{}'::jsonb",
            "UPDATE notifications SET recipient_user_id = NULL,"
            " audience_capability = 'admin.system_health.view'",
            "UPDATE notification_receipts SET user_id = gen_random_uuid()",
            "DELETE FROM notification_receipts",
            "TRUNCATE notifications CASCADE",
        ):
            with self.subTest(statement), self.assertRaises(ProgrammingError):
                async with database.engine.begin() as connection:
                    await connection.execute(text(statement))
        self.assertEqual(
            self.sql("SELECT key FROM notifications WHERE id = :id", id=notification),
            [("k:a",)],
        )


class HealthAsAppRoleTest(
    AsAppRole, test_notifications_postgres.HealthNotificationTest
):
    pass


class HttpAsAppRoleTest(AsAppRole, test_notifications_postgres.NotificationHttpTest):
    pass


# Not collected twice under their own names in this module.
del test_notifications_postgres
