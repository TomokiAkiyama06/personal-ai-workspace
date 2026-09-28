"""Memory versioning in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema; the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). Revision 0042 creates
no table and grants nothing: versioning and the freshness jobs only use what
revisions 0026, 0040 and 0071 already give (INSERT of memories, versions and
relations; UPDATE of ``status`` and ``stale_since``; the history row the trigger
writes; SELECT of projects and memberships). These tests run the service and job
test classes unchanged as that role, and check that the role still cannot rewrite
a version or drop the new index.

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid

import psycopg.errors
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from . import (
    test_memory_freshness,
    test_memory_versioning_races,
    test_memory_versioning_service,
)
from .task_support import TEST_DATABASE_URL, migrate, new_database
from .versioning_support import PostgresVersioningTestCase, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_versioning_app_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-versioning"


def role_url(role: str) -> str:
    url = make_url(TEST_DATABASE_URL).set(username=role, password=ROLE_PASSWORD)
    return url.render_as_string(hide_password=False)


async def owner_sql(sql: str) -> None:
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            await connection.execute(text(sql))
    finally:
        await database.dispose()


async def drop_role() -> None:
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            exists = await connection.execute(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :r"),
                {"r": APP_ROLE},
            )
            if exists.scalar():
                await connection.execute(text(f"DROP OWNED BY {APP_ROLE}"))
                await connection.execute(text(f"DROP ROLE {APP_ROLE}"))
    finally:
        await database.dispose()


def setUpModule():
    if not TEST_DATABASE_URL:
        raise unittest.SkipTest("PAW_TEST_DATABASE_URL is not set")
    asyncio.run(drop_role())
    asyncio.run(
        owner_sql(
            f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
            f" PASSWORD '{ROLE_PASSWORD}'"
        )
    )
    # Recreate every table with the grants of the split-role deployment.
    migrate("base", downgrade=True)
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_role())


class AsAppRole:
    """The service, the jobs and the retriever connect as the application role."""

    def database_url(self) -> str:
        return role_url(APP_ROLE)


def _as(base: type) -> type:
    name = f"{base.__name__}AsAppRole"
    return type(name, (AsAppRole, base), {"__module__": __name__})


for _cls in (
    test_memory_versioning_service.CreateTest,
    test_memory_versioning_service.EditTest,
    test_memory_versioning_service.AccessTest,
    test_memory_versioning_service.RestoreDeprecateRevalidateTest,
    test_memory_versioning_service.RelationTest,
    test_memory_versioning_races.ConcurrentCommitTest,
    test_memory_freshness.RevalidateTest,
    test_memory_freshness.RepoCommitTest,
    test_memory_freshness.ExpiryAndSessionTest,
):
    globals()[f"{_cls.__name__}AsAppRole"] = _as(_cls)
# The loop variable must not be collected as a test class itself.
del _cls


@requires_postgres
class AppRoleLimitsTest(PostgresVersioningTestCase):
    def app_execute(self, sql: str) -> None:
        url = make_url(role_url(APP_ROLE)).set(drivername="postgresql+psycopg")
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text(sql))
        finally:
            engine.dispose()

    def assertRefused(self, sql: str) -> None:
        with self.assertRaises(DBAPIError) as caught:
            self.app_execute(sql)
        self.assertIsInstance(
            caught.exception.orig, psycopg.errors.InsufficientPrivilege
        )

    def test_the_role_cannot_rewrite_a_version_or_drop_the_index(self):
        seeded = self.seed("kept", owner=self.user().user_id)
        self.assertRefused(
            f"UPDATE memory_versions SET content = 'x' WHERE id = '{seeded.version_id}'"
        )
        self.assertRefused(
            f"UPDATE memory_versions SET verified_at = now()"
            f" WHERE id = '{seeded.version_id}'"
        )
        self.assertRefused("DELETE FROM memory_metadata_changes")
        self.assertRefused("DROP INDEX ix_memory_versions_freshness_due")
