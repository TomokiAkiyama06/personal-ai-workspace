"""The persistent external-send audit in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* prove that revision 0087 (``audit_events.details``) grants nothing: the
  privileges of the application role before and after it are the same, table by
  table and column by column, and on ``audit_events`` they are exactly INSERT and
  SELECT (revision 0025),
* run the test classes of ``test_privacy_audit_postgres.py`` as that role, so every
  statement the audit sink executes works with exactly those privileges, and
* check what the role cannot do: change or delete a recorded send, truncate,
  change the schema or the triggers, write a row whose ``details`` could hold text,
  and (a role with no INSERT at all) record a send, which the gate answers by
  refusing to send.

Role names are unique per run and dropped afterwards; the test user must be allowed
to create roles. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import json
import unittest
import uuid
from typing import Any

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from paw_backend.db import Database
from paw_backend.research.privacy import (
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    build_research_broker,
)
from paw_backend.research.providers import ResearchRequest

from . import test_privacy_audit_postgres as postgres
from .privacy_audit_support import PostgresAuditTestCase
from .privacy_support import guarded
from .research_support import fixed_clock, hit, registry_of, web
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_audit_app_{_RUN}"
OTHER_ROLE = f"paw_audit_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-audit"

# What revision 0025 gives the application role on the audit table, and 0087 leaves.
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
PREVIOUS_REVISION = "0026"
REVISION = "0087"

# The privileges of the application role before revision 0087 was applied, read in
# ``setUpModule`` from the same database (filled there).
BEFORE_0087: dict[str, Any] = {}

_TABLE_GRANTS = (
    "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
    "WHERE grantee = :role AND table_schema = current_schema() ORDER BY 1, 2"
)
# Column-level ACL entries (a table-level grant has no entry here).
_COLUMN_ACLS = (
    "SELECT c.relname, a.attname, a.attacl::text FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "WHERE c.relnamespace = current_schema()::regnamespace AND a.attacl IS NOT NULL "
    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 2"
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


async def owner_rows(sql: str, **parameters: Any) -> list[tuple]:
    database = new_database()
    try:
        async with database.engine.connect() as connection:
            return [tuple(r) for r in await connection.execute(text(sql), parameters)]
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


async def app_privileges() -> dict[str, list[tuple]]:
    return {
        "tables": await owner_rows(_TABLE_GRANTS, role=APP_ROLE),
        "columns": await owner_rows(_COLUMN_ACLS),
    }


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
    # Recreate every table with the grants of the split-role deployment: first up to
    # the revision before 0087, to see what the role holds then ...
    migrate("base", downgrade=True)
    migrate(PREVIOUS_REVISION, PAW_APP_DATABASE_ROLE=APP_ROLE)
    BEFORE_0087.update(asyncio.run(app_privileges()))
    # ... then to head.
    migrate(PAW_APP_DATABASE_ROLE=APP_ROLE)


def tearDownModule():
    if TEST_DATABASE_URL:
        asyncio.run(drop_roles())


class AsAppRole:
    """Mixed into a ``PostgresAuditTestCase``: the code under test writes as the app.

    Reading (``self.reader``) and everything else stay with the owner.
    """

    def database_url(self) -> str:
        url = make_url(TEST_DATABASE_URL).set(username=APP_ROLE, password=ROLE_PASSWORD)
        return url.render_as_string(hide_password=False)


# The test classes of test_privacy_audit_postgres.py, unchanged, but every statement
# of the audit sink runs as the unprivileged role.
class SendFlowAsAppRole(AsAppRole, postgres.SendFlowTest):
    pass


class ProductionSinkAsAppRole(AsAppRole, postgres.ProductionSinkTest):
    pass


class AppendOnlyAsAppRole(AsAppRole, postgres.AppendOnlyTest):
    pass


class FailClosedAsAppRole(AsAppRole, postgres.FailClosedTest):
    pass


class ConcurrencyAsAppRole(AsAppRole, postgres.ConcurrencyTest):
    pass


class SinkDirectAsAppRole(AsAppRole, postgres.SinkDirectTest):
    pass


@requires_postgres
class Revision0087GrantsNothingTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_privileges_of_the_app_role_are_the_same_as_before_0087(self):
        self.assertTrue(BEFORE_0087["tables"])  # the snapshot was really taken
        after = await app_privileges()
        self.assertEqual(after["tables"], BEFORE_0087["tables"])
        self.assertEqual(after["columns"], BEFORE_0087["columns"])

    async def test_the_app_role_holds_exactly_select_and_insert_on_the_audit_table(
        self,
    ):
        granted = {
            privilege
            for privilege in ALL_PRIVILEGES
            if (
                await owner_rows(
                    "SELECT has_table_privilege(:r, 'audit_events', :p)",
                    r=APP_ROLE,
                    p=privilege,
                )
            )[0][0]
        }
        self.assertEqual(granted, EXPECTED_PRIVILEGES)

    async def test_the_new_column_can_be_read_and_inserted_but_never_updated(self):
        for privilege, expected in (
            ("SELECT", True),
            ("INSERT", True),
            ("UPDATE", False),
            ("REFERENCES", False),
        ):
            with self.subTest(privilege=privilege):
                ((granted,),) = await owner_rows(
                    "SELECT has_column_privilege(:r, 'audit_events', 'details', :p)",
                    r=APP_ROLE,
                    p=privilege,
                )
                self.assertIs(granted, expected)

    async def test_no_column_of_the_audit_table_can_be_updated_by_the_app_role(self):
        columns = [
            row[0]
            for row in await owner_rows(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_events' AND table_schema = current_schema()"
            )
        ]
        self.assertIn("details", columns)
        for column in columns:
            with self.subTest(column=column):
                ((granted,),) = await owner_rows(
                    "SELECT has_column_privilege(:r, 'audit_events', :c, 'UPDATE')",
                    r=APP_ROLE,
                    c=column,
                )
                self.assertFalse(granted)

    async def test_public_and_a_role_without_grants_get_nothing(self):
        for privilege in ALL_PRIVILEGES:
            with self.subTest(privilege=privilege):
                ((granted,),) = await owner_rows(
                    "SELECT has_table_privilege(:r, 'audit_events', :p)",
                    r=OTHER_ROLE,
                    p=privilege,
                )
                self.assertFalse(granted)
        self.assertEqual(
            await owner_rows(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'PUBLIC' AND table_name = 'audit_events'"
            ),
            [],
        )


@requires_postgres
class AppRoleCannotTest(PostgresAuditTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = role_database(APP_ROLE)
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.app.dispose)
        self.addAsyncCleanup(self.other.dispose)

    async def refused(self, database: Database, sql: str, **parameters: Any) -> Any:
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql), parameters)
                await session.commit()
        return caught.exception.orig

    async def seed(self) -> dict:
        provider = web(hits=[hit()])
        broker = build_research_broker(
            registry_of(provider), self.database, clock=fixed_clock()
        )
        await guarded(
            broker.gather(
                ResearchRequest("python asyncio"),
                preflight_input=PrivacyInput([], self.project_id),
            )
        )
        (row,) = await self.rows()
        return row

    async def test_the_roles_really_are_non_superusers(self):
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

    async def test_a_recorded_send_cannot_be_rewritten_deleted_or_truncated(self):
        before = await self.seed()
        forbidden = [
            "UPDATE audit_events SET details = '{}'::jsonb",
            'UPDATE audit_events SET details = details || \'{"note": "x"}\'',
            "UPDATE audit_events SET reason = 'x'",
            "UPDATE audit_events SET decision = 'deny'",
            "UPDATE audit_events SET project_id = gen_random_uuid()",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
            # The schema, the constraints and the triggers belong to the migrator.
            "ALTER TABLE audit_events ADD COLUMN extra text",
            "ALTER TABLE audit_events DROP COLUMN details",
            "ALTER TABLE audit_events DROP CONSTRAINT "
            "ck_audit_events_external_send_details",
            "ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_details_object",
            "ALTER TABLE audit_events DISABLE TRIGGER ALL",
            "ALTER TABLE audit_events DISABLE TRIGGER "
            "tr_audit_events_reject_update_delete",
            "DROP TRIGGER tr_audit_events_reject_truncate ON audit_events",
            "CREATE RULE r AS ON INSERT TO audit_events DO INSTEAD NOTHING",
            "DROP TABLE audit_events",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                error = await self.refused(self.app, sql)
                self.assertIsInstance(error, psycopg.errors.InsufficientPrivilege)
        self.assertEqual(await self.rows(), [before])

    async def test_a_row_that_could_hold_text_is_refused_for_the_app_role_too(self):
        details = {
            "query_fingerprint": "sha256:" + "a" * 64,
            "query_chars": 3,
            "provider_kinds": ["web"],
            "withheld": dict.fromkeys(
                ("private_source", "private_memory", "raw_conversation", "secret"), 0
            ),
            "credentials_removed": 0,
            "pieces_matched": 0,
            "abstractions": 0,
            "truncated": False,
        }
        insert = (
            "INSERT INTO audit_events (id, correlation_id, occurred_at, action, "
            "resource_kind, decision, reason, project_id, details) VALUES "
            "(gen_random_uuid(), gen_random_uuid(), now(), "
            "'research.external_send', 'research_query', 'allow', "
            "'send_authorized', :p, CAST(:d AS jsonb))"
        )
        # The valid one is accepted ...
        async with self.app.session() as session:
            await session.execute(
                text(insert), {"p": self.project_id, "d": json.dumps(details)}
            )
            await session.commit()
        self.assertEqual(len(await self.rows()), 1)
        # ... and a copy with a place for a query is not.
        for label, bad in {
            "an extra key": {**details, "query": "python asyncio"},
            "the query as the fingerprint": {**details, "query_fingerprint": "python"},
            "the query as a kind": {**details, "provider_kinds": ["python asyncio"]},
        }.items():
            with self.subTest(case=label):
                error = await self.refused(
                    self.app, insert, p=self.project_id, d=json.dumps(bad)
                )
                self.assertIsInstance(error, psycopg.errors.CheckViolation)
        self.assertEqual(len(await self.rows()), 1)

    async def assert_the_gate_refuses_and_nothing_is_recorded(self, database):
        provider = web(hits=[hit()])
        broker = build_research_broker(
            registry_of(provider), database, clock=fixed_clock()
        )
        with self.assertLogs("paw_backend.research.privacy", level="WARNING") as logs:
            with self.assertRaises(PrivacyRefusal) as caught:
                await guarded(
                    broker.gather(
                        ResearchRequest("python asyncio"),
                        preflight_input=PrivacyInput([], self.project_id),
                    )
                )
        self.assertIs(caught.exception.reason, RefusalReason.AUDIT_FAILED)
        self.assertEqual(provider.search_calls, [])  # not sent
        self.assertEqual(await self.rows(), [])  # not recorded
        (line,) = logs.output
        self.assertNotIn("python", line)
        self.assertNotIn(ROLE_PASSWORD, line)
        self.assertNotIn(OTHER_ROLE, line)

    async def test_a_role_without_insert_records_nothing_and_the_gate_refuses(self):
        await self.assert_the_gate_refuses_and_nothing_is_recorded(self.other)

    async def test_a_role_that_may_only_read_cannot_record_either(self):
        await owner_sql(f"GRANT SELECT ON audit_events TO {OTHER_ROLE}")
        try:
            await self.assert_the_gate_refuses_and_nothing_is_recorded(self.other)
        finally:
            await owner_sql(f"REVOKE SELECT ON audit_events FROM {OTHER_ROLE}")

    async def test_the_app_role_reads_back_what_it_wrote(self):
        await self.seed()
        async with self.app.session() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM audit_events WHERE project_id = :p"),
                    {"p": self.project_id},
                )
            ).scalar()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
