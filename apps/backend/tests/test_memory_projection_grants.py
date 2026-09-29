"""The Memory Markdown Projection in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema; the job connects with
``PAW_DATABASE_URL``, a NON-superuser application role. PAW-045 adds no migration
and grants nothing: the projection only reads ``memory_versions`` (SELECT, revision
0040) and records its outcome in ``audit_events`` (INSERT and SELECT, revisions
0025 / 0086). These tests run the PostgreSQL projection tests unchanged as that
role, and check that the role still cannot rewrite a memory or an audit row.

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

from . import test_memory_projection_postgres
from .memory_support import requires_postgres
from .task_support import TEST_DATABASE_URL, migrate, new_database
from .test_memory_projection_postgres import PostgresProjectionTestCase

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_projection_app_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-projection"


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
    """The projection connects as the application role."""

    def database_url(self) -> str:
        return role_url(APP_ROLE)


def _as(base: type) -> type:
    name = f"{base.__name__}AsAppRole"
    return type(name, (AsAppRole, base), {"__module__": __name__})


for _cls in (
    test_memory_projection_postgres.ProjectionTest,
    test_memory_projection_postgres.FailureTest,
):
    globals()[f"{_cls.__name__}AsAppRole"] = _as(_cls)
# The loop variable must not be collected as a test class itself.
del _cls


@requires_postgres
class AppRoleLimitsTest(PostgresProjectionTestCase):
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
            caught.exception.orig,
            psycopg.errors.InsufficientPrivilege | psycopg.errors.RaiseException,
        )

    def test_the_role_cannot_rewrite_a_memory_or_an_audit_row(self):
        seeded = self.seed("kept", owner=uuid.uuid4(), embed=False)
        self.assertRefused(
            f"UPDATE memory_versions SET content = 'x' WHERE id = '{seeded.version_id}'"
        )
        self.assertRefused(
            "DELETE FROM audit_events WHERE resource_kind = 'memory_projection_run'"
        )
