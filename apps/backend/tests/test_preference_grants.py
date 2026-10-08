"""The Inferred Preference flow in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema; the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). Revision 0192 grants
that role SELECT and INSERT on ``memory_preference_resolutions`` and nothing else;
the flow otherwise uses what the Memory, Journal and Repository revisions already
grant (reading the person's journal entries, messages and keys; writing versions,
relations, sources and keys; locking a project or a repository FOR SHARE). These
tests run the service test classes unchanged as that role, and check that the role
cannot rewrite or remove an answer.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid

import psycopg.errors
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from . import test_preference_flow, test_preference_service
from .preference_support import PostgresPreferenceTestCase, requires_postgres
from .task_support import TEST_DATABASE_URL, migrate, new_database

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_preference_app_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-preferences"


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
    """The services connect as the application role."""

    def database_url(self) -> str:
        return role_url(APP_ROLE)


def _as(base: type) -> type:
    name = f"{base.__name__}AsAppRole"
    return type(name, (AsAppRole, base), {"__module__": __name__})


for _cls in (
    test_preference_service.CandidatesTest,
    test_preference_service.ConfirmMemoryTest,
    test_preference_service.HighRiskTest,
    test_preference_service.RejectTest,
    test_preference_service.ConfirmHeldTest,
    test_preference_service.FreeTextTest,
    # Through the Immediate Journal and the consolidator.
    test_preference_flow.EndToEndTest,
):
    globals()[f"{_cls.__name__}AsAppRole"] = _as(_cls)
# The loop variable must not be collected as a test class itself.
del _cls


@requires_postgres
class AppRoleLimitsTest(PostgresPreferenceTestCase):
    def app_execute(self, sql: str) -> None:
        url = make_url(role_url(APP_ROLE)).set(drivername="postgresql+psycopg")
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text(sql))
        finally:
            engine.dispose()

    def test_the_role_cannot_rewrite_or_remove_an_answer(self):
        me = self.user()
        self.observe(me.user_id, "k", result="held_high_risk")
        self.execute(
            "INSERT INTO memory_preference_resolutions (entry_id, item_index,"
            " owner_user_id, resolution) SELECT id, 0, owner_user_id, 'rejected'"
            " FROM memory_journal_entries"
        )
        for sql in (
            "UPDATE memory_preference_resolutions SET resolution = 'confirmed'",
            "DELETE FROM memory_preference_resolutions",
            "TRUNCATE memory_preference_resolutions",
        ):
            with self.subTest(sql), self.assertRaises(DBAPIError) as caught:
                self.app_execute(sql)
            self.assertIsInstance(
                caught.exception.orig, psycopg.errors.InsufficientPrivilege
            )
        self.assertEqual(len(self.resolutions()), 1)
