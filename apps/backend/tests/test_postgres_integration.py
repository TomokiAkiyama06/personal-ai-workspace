"""Tests against a real PostgreSQL server.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set, for example::

    PAW_TEST_DATABASE_URL=postgresql://paw:paw@localhost:5432/paw_test \\
        python -m unittest discover -s tests -t .

Use a disposable database: the migration test upgrades it to ``head`` and
downgrades it to ``base`` again.
"""

import asyncio
import io
import os
import unittest

from alembic import command
from sqlalchemy import text

from paw_backend.app import create_app
from paw_backend.db import Database, DatabaseStatus

from .support import make_client, make_settings, paw_environment
from .test_migrations import offline_config

TEST_DATABASE_URL = os.environ.get("PAW_TEST_DATABASE_URL")


def with_options(url: str, options: str) -> str:
    """``url`` with extra query options appended."""
    return f"{url}{'&' if '?' in url else '?'}{options}"


class CiProvidesPostgresTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CI") == "true", "enforced on CI only")
    def test_ci_sets_the_database_url_so_integration_tests_cannot_be_skipped(self):
        # A green CI run must mean the PostgreSQL tests below actually ran.
        self.assertTrue(
            TEST_DATABASE_URL,
            "CI must set PAW_TEST_DATABASE_URL (see .github/workflows/ci.yml)",
        )


@unittest.skipUnless(TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set")
class PostgresIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = make_settings(database_url=TEST_DATABASE_URL)

    async def test_database_check_succeeds(self):
        database = Database(self.settings)
        try:
            self.assertEqual(await database.check(), DatabaseStatus.OK)
            async with database.session() as session:
                self.assertEqual((await session.execute(text("SELECT 1"))).scalar(), 1)
        finally:
            await database.dispose()

    async def test_readiness_endpoint_is_ok(self):
        def request():
            with make_client(create_app(self.settings)) as client:
                return client.get("/api/v1/health/ready")

        response = await asyncio.to_thread(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["checks"], {"database": "ok"})

    async def test_readiness_works_with_options_in_the_url(self):
        # `connect_timeout` is also set by the probe itself; the URL's value
        # must not make the connection call fail with a duplicate argument.
        url = with_options(TEST_DATABASE_URL, "connect_timeout=10&application_name=x")
        database = Database(make_settings(database_url=url))
        try:
            self.assertEqual(await database.check(), DatabaseStatus.OK)
        finally:
            await database.dispose()

    async def test_migrations_upgrade_and_downgrade(self):
        def migrate():
            # `env.py` reads PAW_DATABASE_URL; an online run needs no ini file.
            with paw_environment(PAW_DATABASE_URL=TEST_DATABASE_URL):
                config = offline_config(io.StringIO())
                command.upgrade(config, "head")
                command.downgrade(config, "base")

        await asyncio.to_thread(migrate)


if __name__ == "__main__":
    unittest.main()
