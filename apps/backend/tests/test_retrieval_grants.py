"""Hybrid Retrieval in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema; the backend connects as a NON-superuser
role. Revision 0043 creates no table, so it grants nothing: the retrieval only reads
tables whose privileges revisions 0026 and 0040 already give. These tests

* run the retrieval test classes as the application role (``PAW_APP_DATABASE_ROLE``);
* run them again as a role that holds **SELECT on exactly five tables** and nothing
  else, which proves that set is enough;
* remove the SELECT on each of the five in turn and show the retrieval then fails,
  which proves each one is needed (the set is exact);
* check that the retrieval writes nothing, and that the application role can neither
  drop nor replace the full-text index.

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid
from typing import Any

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from . import (
    test_retrieval_leakage,
    test_retrieval_service_access,
    test_retrieval_service_metadata,
    test_retrieval_service_search,
    test_retrieval_service_stages,
)
from .retrieval_pg_support import PostgresRetrievalTestCase, requires_postgres
from .task_support import TEST_DATABASE_URL, migrate, new_database

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_retrieval_app_{_RUN}"
MINIMAL_ROLE = f"paw_retrieval_min_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-retrieval"

# The tables a retrieval reads, and nothing else (revision 0043 adds none).
READ_TABLES = (
    "memory_versions",
    "memory_embeddings",
    "memory_relations",
    "projects",
    "project_members",
)
MISSING_ROLES = {
    table: f"paw_retrieval_no_{n}_{_RUN}" for n, table in enumerate(READ_TABLES)
}
ALL_ROLES = (APP_ROLE, MINIMAL_ROLE, *MISSING_ROLES.values())
ALL_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


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


async def drop_roles() -> None:
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            for role in ALL_ROLES:
                exists = await connection.execute(
                    text("SELECT count(*) FROM pg_roles WHERE rolname = :r"),
                    {"r": role},
                )
                if exists.scalar():
                    await connection.execute(text(f"DROP OWNED BY {role}"))
                    await connection.execute(text(f"DROP ROLE {role}"))
    finally:
        await database.dispose()


def setUpModule():
    if not TEST_DATABASE_URL:
        raise unittest.SkipTest("PAW_TEST_DATABASE_URL is not set")
    asyncio.run(drop_roles())
    for role in ALL_ROLES:
        asyncio.run(
            owner_sql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
                f" PASSWORD '{ROLE_PASSWORD}'"
            )
        )
    # Recreate every table with the grants of the split-role deployment.
    migrate("base", downgrade=True)
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)
    for table in READ_TABLES:
        asyncio.run(owner_sql(f"GRANT SELECT ON {table} TO {MINIMAL_ROLE}"))
    for missing, role in MISSING_ROLES.items():
        for table in READ_TABLES:
            if table != missing:
                asyncio.run(owner_sql(f"GRANT SELECT ON {table} TO {role}"))


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_roles())


class AsAppRole:
    """Mixed into a retrieval test class: its retriever connects as the app role."""

    def database_url(self) -> str:
        return role_url(APP_ROLE)


class AsMinimalRole:
    """The retriever connects as a role with SELECT on the five tables only."""

    def database_url(self) -> str:
        return role_url(MINIMAL_ROLE)


def _as(mixin: type, base: type, name: str) -> type:
    return type(name, (mixin, base), {"__module__": __name__})


# The retrieval test classes, unchanged, but the retrieval's statements run as an
# unprivileged role. (Seeding and cleaning use the owner engine of the base class.)
# The role with SELECT on five tables runs all of them; the application role a
# representative selection (the split-role deployment).
_CLASSES = [
    test_retrieval_service_search.KeywordSearchTest,
    test_retrieval_service_search.VectorSearchTest,
    test_retrieval_service_search.HybridSearchTest,
    test_retrieval_service_metadata.StatusTest,
    test_retrieval_service_metadata.FreshnessFilterTest,
    test_retrieval_service_metadata.StaleTest,
    test_retrieval_service_metadata.StructuredScoreTest,
    test_retrieval_service_metadata.DuplicateTest,
    test_retrieval_service_metadata.ConflictTest,
    test_retrieval_service_access.UserScopeTest,
    test_retrieval_service_access.SharedScopeTest,
    test_retrieval_service_access.ProjectScopeTest,
    test_retrieval_service_access.NarrowingTest,
    test_retrieval_service_access.RepoScopeTest,
    test_retrieval_service_access.ProjectGroupScopeTest,
    test_retrieval_service_stages.SystemPolicyTest,
    test_retrieval_leakage.ExplicitLeakageTest,
    test_retrieval_leakage.RandomisedLeakageTest,
]
_APP_ROLE_CLASSES = [
    test_retrieval_service_search.KeywordSearchTest,
    test_retrieval_service_search.HybridSearchTest,
    test_retrieval_service_metadata.ConflictTest,
    test_retrieval_service_access.ProjectScopeTest,
    test_retrieval_service_access.RepoScopeTest,
    test_retrieval_leakage.ExplicitLeakageTest,
]
for _cls in _CLASSES:
    globals()[f"{_cls.__name__}AsMinimalRole"] = _as(
        AsMinimalRole, _cls, f"{_cls.__name__}AsMinimalRole"
    )
for _cls in _APP_ROLE_CLASSES:
    globals()[f"{_cls.__name__}AsAppRole"] = _as(
        AsAppRole, _cls, f"{_cls.__name__}AsAppRole"
    )
# The loop variable must not be collected as a test class itself.
del _cls


@requires_postgres
class RolePrivilegesTest(PostgresRetrievalTestCase):
    def owner_scalar(self, sql: str, **parameters: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), parameters).scalar()

    def build_world(self):
        """A user with own, project and shared memory, embeddings and a conflict."""
        project = self.seed_project()
        me = self.member_of(project)
        mine = self.seed("mine", "deploy backend friday", owner=me.user_id)
        theirs = self.seed(
            "theirs", "deploy backend friday project", scope="project", project=project
        )
        self.seed("shared", "deploy backend friday shared", scope="shared")
        self.seed_relation(mine.version_id, theirs.version_id)
        return me

    async def test_the_roles_really_are_non_superusers_with_the_expected_grants(self):
        for role in (APP_ROLE, MINIMAL_ROLE):
            with self.subTest(role=role):
                row = self.owner_scalar(
                    "SELECT rolsuper OR rolcreaterole OR rolbypassrls"
                    " FROM pg_roles WHERE rolname = :r",
                    r=role,
                )
                self.assertFalse(row)
        for table in READ_TABLES:
            for privilege in ALL_PRIVILEGES:
                with self.subTest(table=table, privilege=privilege):
                    granted = self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=MINIMAL_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(granted, privilege == "SELECT")
        # No other table is readable by it.
        for table in ("memories", "embedding_models", "users", "audit_events"):
            with self.subTest(table=table):
                self.assertFalse(
                    self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, 'SELECT')",
                        r=MINIMAL_ROLE,
                        t=table,
                    )
                )

    async def test_the_world_is_found_as_the_role_with_select_alone(self):
        me = self.build_world()
        retriever = self.new_retriever_as(MINIMAL_ROLE)
        result = await self.retrieve(me, "deploy backend friday", retriever=retriever)
        self.assertEqual(
            sorted(h.title for h in result.hits), ["mine", "shared", "theirs"]
        )
        self.assertEqual(len(result.conflicts), 1)

    async def test_each_of_the_five_tables_is_needed(self):
        me = self.build_world()
        for table, role in MISSING_ROLES.items():
            with self.subTest(missing=table):
                retriever = self.new_retriever_as(role)
                with self.assertRaises(DBAPIError) as caught:
                    await self.retrieve(
                        me, "deploy backend friday", retriever=retriever
                    )
                self.assertIsInstance(
                    caught.exception.orig, psycopg.errors.InsufficientPrivilege
                )

    async def test_a_retrieval_writes_nothing(self):
        me = self.build_world()
        tables = (*READ_TABLES, "memory_metadata_changes", "memories", "memory_sources")
        counts = "SELECT " + ", ".join(f"(SELECT count(*) FROM {t})" for t in tables)
        fingerprint = (
            "SELECT md5(string_agg(t::text, '' ORDER BY t::text))"
            " FROM memory_versions t"
        )

        def snapshot():
            with self.engine.connect() as connection:
                return (
                    tuple(connection.execute(text(counts)).one()),
                    connection.execute(text(fingerprint)).scalar(),
                )

        before = snapshot()
        for role in (APP_ROLE, MINIMAL_ROLE):
            retriever = self.new_retriever_as(role)
            await self.retrieve(me, "deploy backend friday", retriever=retriever)
        self.assertEqual(snapshot(), before)

    async def test_the_app_role_can_neither_drop_nor_replace_the_index(self):
        database = self.role_database(APP_ROLE)
        for sql in (
            "DROP INDEX ix_memory_versions_search",
            "CREATE INDEX ix_other ON memory_versions (title)",
            "ALTER INDEX ix_memory_versions_search RENAME TO ix_stolen",
        ):
            with self.subTest(sql=sql):
                async with database.session() as session:
                    with self.assertRaises(DBAPIError) as caught:
                        await session.execute(text(sql))
                        await session.commit()
                self.assertIsInstance(
                    caught.exception.orig, psycopg.errors.InsufficientPrivilege
                )
        self.assertTrue(
            self.owner_scalar(
                "SELECT count(*) FROM pg_indexes WHERE indexname = :n",
                n="ix_memory_versions_search",
            )
        )

    def role_database(self, role: str):
        from paw_backend.db import Database

        from .support import make_settings

        database = Database(make_settings(database_url=role_url(role)))
        self.addAsyncCleanup(database.dispose)
        return database

    def new_retriever_as(self, role: str):
        class Bound(AsMinimalRole):
            def database_url(inner) -> str:
                return role_url(role)

        original = self.database_url
        self.database_url = Bound().database_url
        try:
            return self.new_retriever()
        finally:
            self.database_url = original


if __name__ == "__main__":
    unittest.main()
