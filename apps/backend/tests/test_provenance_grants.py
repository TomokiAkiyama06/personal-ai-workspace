"""The provenance tables in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* run the store and query test classes as that role, so every statement the
  code executes is proven to work with exactly the privileges revision 0052
  grants (SELECT and INSERT: nothing is ever updated or deleted), and
* check that nothing else is allowed: rewriting a source, a claim, a stance or a
  relation, deleting evidence, ``ON CONFLICT DO UPDATE``, truncating, changing
  the schema, or linking rows of two projects.

Role names are unique per run and dropped afterwards; the test user must be
allowed to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import unittest
import uuid
from typing import Any

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database
from paw_backend.research.provenance import ProvenanceStore

from . import (
    test_provenance_concurrency,
    test_provenance_queries,
    test_provenance_store_record,
    test_provenance_store_relations,
    test_provenance_store_trace,
)
from .provenance_support import FakeClock, PostgresProvenanceTestCase
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_prov_app_{_RUN}"
OTHER_ROLE = f"paw_prov_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-provenance"

# The application only reads and inserts: no table-level UPDATE / DELETE and no
# column-level UPDATE anywhere. The exact copy of the choice in migration 0052.
TABLES = (
    "research_sources",
    "research_claims",
    "research_claim_sources",
    "research_claim_uses",
    "research_claim_relations",
    "research_source_relations",
)
EXPECTED_PRIVILEGES = {"SELECT", "INSERT"}
ALL_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def role_database(role: str) -> Database:
    url = make_url(TEST_DATABASE_URL).set(username=role, password=ROLE_PASSWORD)
    return Database(
        make_settings(database_url=url.render_as_string(hide_password=False))
    )


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
            for role in (APP_ROLE, OTHER_ROLE):
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
    for role in (APP_ROLE, OTHER_ROLE):
        asyncio.run(
            owner_sql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{ROLE_PASSWORD}'"
            )
        )
    # Recreate every table with the grants of the split-role deployment.
    migrate("base", downgrade=True)
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_roles())


class AsAppRole:
    """Mixed into a ``PostgresProvenanceTestCase``: its stores connect as the app role.

    Seeding and cleaning still use the owner engine of the base class.
    """

    def new_store(
        self, clock: FakeClock | None = None, **options: Any
    ) -> ProvenanceStore:
        database = role_database(APP_ROLE)
        self.addAsyncCleanup(database.dispose)
        return ProvenanceStore(database, clock=clock or self.clock, **options)


class QueriesAsAppRole(AsAppRole):
    """The same for the query tests, which use a ``Database`` directly."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.database = role_database(APP_ROLE)
        self.addAsyncCleanup(self.database.dispose)


# The store tests, unchanged, but every store statement runs as the
# unprivileged role.
class RecordClaimAsAppRole(AsAppRole, test_provenance_store_record.RecordClaimTest):
    pass


class DeduplicationAsAppRole(
    AsAppRole, test_provenance_store_record.RecordClaimDeduplicationTest
):
    pass


class SourceIdentityAsAppRole(
    AsAppRole, test_provenance_store_record.RecordClaimSourceIdentityTest
):
    pass


class ConflictAsAppRole(
    AsAppRole, test_provenance_store_record.RecordClaimConflictTest
):
    pass


class TaskAsAppRole(AsAppRole, test_provenance_store_record.RecordClaimTaskTest):
    pass


class ProjectAsAppRole(AsAppRole, test_provenance_store_record.RecordClaimProjectTest):
    pass


class LimitAsAppRole(AsAppRole, test_provenance_store_record.RecordClaimLimitTest):
    pass


class NoContentAsAppRole(
    AsAppRole, test_provenance_store_record.RecordClaimNoContentTest
):
    pass


class MarkRelatedAsAppRole(AsAppRole, test_provenance_store_relations.MarkRelatedTest):
    pass


class ListRelationsAsAppRole(
    AsAppRole, test_provenance_store_relations.ListRelationsTest
):
    pass


class AddReferenceAsAppRole(AsAppRole, test_provenance_store_trace.AddReferenceTest):
    pass


class GetClaimAsAppRole(AsAppRole, test_provenance_store_trace.GetClaimTest):
    pass


class TraceScenarioAsAppRole(AsAppRole, test_provenance_store_trace.TraceScenarioTest):
    pass


class TraceEdgeCasesAsAppRole(
    AsAppRole, test_provenance_store_trace.TraceEdgeCasesTest
):
    pass


class ConcurrentRecordAsAppRole(
    AsAppRole, test_provenance_concurrency.ConcurrentRecordTest
):
    pass


class LockingAsAppRole(AsAppRole, test_provenance_concurrency.LockingTest):
    pass


class ConcurrentRelationAsAppRole(
    AsAppRole, test_provenance_concurrency.ConcurrentRelationTest
):
    pass


class EnsureSourceAsAppRole(QueriesAsAppRole, test_provenance_queries.EnsureSourceTest):
    pass


class EnsureClaimAsAppRole(QueriesAsAppRole, test_provenance_queries.EnsureClaimTest):
    pass


class LinkClaimSourceAsAppRole(
    QueriesAsAppRole, test_provenance_queries.LinkClaimSourceTest
):
    pass


class AddClaimUseAsAppRole(QueriesAsAppRole, test_provenance_queries.AddClaimUseTest):
    pass


class InsertRelationAsAppRole(
    QueriesAsAppRole, test_provenance_queries.InsertRelationTest
):
    pass


class FetchClaimAsAppRole(QueriesAsAppRole, test_provenance_queries.FetchClaimTest):
    pass


class FetchReferenceClaimsAsAppRole(
    QueriesAsAppRole, test_provenance_queries.FetchReferenceClaimsTest
):
    pass


class FetchClaimLinksAsAppRole(
    QueriesAsAppRole, test_provenance_queries.FetchClaimLinksTest
):
    pass


class FetchRelationsAsAppRole(
    QueriesAsAppRole, test_provenance_queries.FetchRelationsTest
):
    pass


@requires_postgres
class AppRolePrivilegesTest(PostgresProvenanceTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = role_database(APP_ROLE)
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.app.dispose)
        self.addAsyncCleanup(self.other.dispose)

    def owner_scalar(self, sql: str, **parameters: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), parameters).scalar()

    def owner_rows(self, sql: str) -> list[tuple]:
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql))]

    async def refused(self, database: Database, sql: str):
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql))
                await session.commit()
        self.assertIsInstance(
            caught.exception.orig, psycopg.errors.InsufficientPrivilege
        )

    def snapshot(self) -> dict[str, list[tuple]]:
        return {
            table: self.owner_rows(f"SELECT * FROM {table} ORDER BY 1, 2")
            for table in TABLES
        }

    async def test_the_store_really_runs_as_a_non_superuser_role(self):
        for database in (self.app, self.other):
            async with database.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT current_user, rolsuper, rolcreaterole, "
                            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
            self.assertIn(row[0], (APP_ROLE, OTHER_ROLE))
            self.assertEqual(tuple(row[1:]), (False, False, False))

    async def test_the_expectations_cover_every_provenance_table(self):
        tables = {
            row[0]
            for row in self.owner_rows(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename LIKE 'research\\_%' "
                "AND tablename NOT LIKE 'research\\_scratch\\_%'"
            )
        }
        self.assertEqual(tables, set(TABLES))

    async def test_the_app_role_holds_exactly_select_and_insert(self):
        for table in TABLES:
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    granted = self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=APP_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(
                        granted, privilege in EXPECTED_PRIVILEGES, privilege
                    )
                columns = [
                    row[0]
                    for row in self.owner_rows(
                        "SELECT column_name FROM information_schema.columns "
                        f"WHERE table_name = '{table}'"
                    )
                ]
                self.assertTrue(columns)
                for column in columns:
                    self.assertFalse(
                        self.owner_scalar(
                            "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                            r=APP_ROLE,
                            t=table,
                            c=column,
                        ),
                        f"{table}.{column}",
                    )

    async def test_a_role_without_grants_reaches_no_provenance_table(self):
        for table in TABLES:
            with self.subTest(table=table):
                await self.refused(self.other, f"SELECT count(*) FROM {table}")
                await self.refused(self.other, f"DELETE FROM {table}")

    async def test_the_app_role_cannot_rewrite_delete_or_truncate_evidence(self):
        claim = self.seed_claim("Claim")
        other_claim = self.seed_claim("Other claim")
        source = self.seed_source()
        other_source = self.seed_source(locator="https://example.com/b")
        self.seed_link(claim, source)
        self.seed_use(claim, "answer", uuid.uuid4())
        self.seed_relation("claim", claim, other_claim, "duplicate")
        self.seed_relation("source", source, other_source, "duplicate")
        before = self.snapshot()

        forbidden = [
            # A source is never edited: not its dates, type, hash or locator.
            "UPDATE research_sources SET fetched_at = now()",
            "UPDATE research_sources SET published_at = now()",
            "UPDATE research_sources SET source_type = 'primary'",
            "UPDATE research_sources SET content_hash = 'sha256:' || repeat('b', 64)",
            "UPDATE research_sources SET locator = 'https://example.com/changed'",
            "UPDATE research_sources SET title = 'changed'",
            "UPDATE research_sources SET project_id = gen_random_uuid()",
            # ... nor a claim.
            "UPDATE research_claims SET claim_text = 'changed'",
            "UPDATE research_claims SET text_fingerprint = repeat('c', 64)",
            "UPDATE research_claims SET created_by = gen_random_uuid()",
            "UPDATE research_claims SET task_id = NULL",
            # A stance, a use and a relation are never edited.
            "UPDATE research_claim_sources SET stance = 'contradicts'",
            "UPDATE research_claim_sources SET source_id = gen_random_uuid()",
            "UPDATE research_claim_uses SET ref_id = gen_random_uuid()",
            "UPDATE research_claim_relations SET kind = 'contradiction'",
            "UPDATE research_source_relations SET kind = 'contradiction'",
            # Nothing is deleted or truncated by the application.
            "DELETE FROM research_sources",
            "DELETE FROM research_claims",
            "DELETE FROM research_claim_sources",
            "DELETE FROM research_claim_uses",
            "DELETE FROM research_claim_relations",
            "DELETE FROM research_source_relations",
            "TRUNCATE research_claim_sources",
            "TRUNCATE research_claims CASCADE",
            "TRUNCATE research_sources CASCADE",
            # The schema belongs to the migration role.
            "ALTER TABLE research_sources ADD COLUMN extra text",
            "ALTER TABLE research_claims DROP CONSTRAINT "
            "ck_research_claims_claim_text_length",
            "DROP INDEX ix_research_claim_uses_reference",
            "DROP TABLE research_claim_relations",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)

        self.assertEqual(self.snapshot(), before)

    async def test_on_conflict_do_update_is_not_available_to_the_app_role(self):
        claim = self.seed_claim("Claim")
        source = self.seed_source()
        self.seed_link(claim, source, "supports")

        await self.refused(
            self.app,
            "INSERT INTO research_claim_sources"
            " (claim_id, source_id, project_id, stance, created_at)"
            f" VALUES ('{claim}', '{source}', '{self.project_id}',"
            " 'contradicts', now())"
            " ON CONFLICT (claim_id, source_id) DO UPDATE SET stance = 'contradicts'",
        )

        (row,) = self.rows("SELECT stance FROM research_claim_sources")
        self.assertEqual(row["stance"], "supports")

    async def test_a_link_cannot_cross_projects_even_for_the_app_role(self):
        claim = self.seed_claim("Claim")
        foreign_source = self.seed_source(project_id=self.other_project_id)

        async with self.app.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(
                    text(
                        "INSERT INTO research_claim_sources"
                        " (claim_id, source_id, project_id, stance, created_at)"
                        " VALUES (:c, :s, :p, 'supports', now())"
                    ),
                    {"c": claim, "s": foreign_source, "p": self.project_id},
                )
                await session.commit()
        self.assertIsInstance(caught.exception.orig, psycopg.errors.ForeignKeyViolation)

        own_source = self.seed_source(locator="https://example.com/own")
        async with self.app.session() as session:
            await session.execute(
                text(
                    "INSERT INTO research_claim_sources"
                    " (claim_id, source_id, project_id, stance, created_at)"
                    " VALUES (:c, :s, :p, 'supports', now())"
                ),
                {"c": claim, "s": own_source, "p": self.project_id},
            )
            await session.commit()
            count = (
                await session.execute(
                    text("SELECT count(*) FROM research_claim_sources")
                )
            ).scalar_one()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
