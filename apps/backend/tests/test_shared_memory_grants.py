"""Shared Memory in the split-role deployment (real PostgreSQL).

The migrations run as the owner of the schema and the backend connects as a
NON-superuser application role (``PAW_APP_DATABASE_ROLE``). These tests migrate
the test database that way, then

* run the service test classes as that role, so every statement the service
  executes is proven to work with exactly the privileges revisions 0040 and 0046
  grant (row locks, conditional status updates, the decision columns, the
  advisory locks), and
* check that nothing else is allowed on the candidates table (rewriting what was
  proposed, deleting the record of a decision, truncating, changing the schema).

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

from . import (
    test_shared_memory_audit_actions as audit_actions,
)
from . import (
    test_shared_memory_candidates as candidates,
)
from . import (
    test_shared_memory_completion as completion,
)
from . import (
    test_shared_memory_effective_view as effective_view,
)
from . import (
    test_shared_memory_promotion_refused as refused,
)
from . import (
    test_shared_memory_service_guards as guards,
)
from . import (
    test_shared_memory_service_manage as manage,
)
from . import (
    test_shared_memory_service_read as read,
)
from . import (
    test_shared_memory_status_history as status_history,
)
from .shared_memory_support import AsyncPostgresSharedTestCase
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

_RUN = uuid.uuid4().hex[:10]
APP_ROLE = f"paw_shared_app_{_RUN}"
OTHER_ROLE = f"paw_shared_other_{_RUN}"
ROLE_PASSWORD = "dummy-test-password-shared"

CANDIDATES = "shared_memory_candidates"
# table -> (table-level privileges, columns the application may UPDATE). The
# exact copy of the choices (and their reasons) in migration 0046 for the new
# table, and what revision 0040 gives the tables the service also writes.
EXPECTED = {
    CANDIDATES: (
        {"SELECT", "INSERT"},
        {"state", "decided_by", "decided_at", "decision_reason", "memory_id"},
    ),
    "memories": ({"SELECT", "INSERT", "DELETE"}, set()),
    "memory_versions": (
        {"SELECT", "INSERT"},
        {"status", "stale_since", "pinned", "importance"},
    ),
    "memory_relations": ({"SELECT", "INSERT"}, set()),
    "memory_sources": ({"SELECT", "INSERT"}, {"source_deleted_at"}),
    # The audit rows (revision 0025): the attempt of the Authorizer and, in the
    # transaction of each change, its completion. Append and read, nothing else.
    "audit_events": ({"SELECT", "INSERT"}, set()),
}
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
    """Mixed into a service test class: its services connect as the app role.

    Seeding, cleaning and every assertion still use the owner engine.
    """

    def new_database(self) -> Database:
        return role_database(APP_ROLE)


# The service test classes, unchanged, but every service statement runs as the
# unprivileged role.
class ListMemoriesAsAppRole(AsAppRole, read.ListMemoriesTest):
    pass


class IncludeDeletedAsAppRole(AsAppRole, read.IncludeDeletedTest):
    pass


class GetMemoryAsAppRole(AsAppRole, read.GetMemoryTest):
    pass


class CreateMemoryAsAppRole(AsAppRole, manage.CreateMemoryTest):
    pass


class EditMemoryAsAppRole(AsAppRole, manage.EditMemoryTest):
    pass


class DeleteAndRestoreAsAppRole(AsAppRole, manage.DeleteAndRestoreTest):
    pass


class ConcurrencyAsAppRole(AsAppRole, manage.ConcurrencyTest):
    pass


class StatusHistoryAsAppRole(AsAppRole, status_history.SharedStatusHistoryTest):
    pass


class ProposeAsAppRole(AsAppRole, candidates.ProposeTest):
    pass


class PendingLimitAsAppRole(AsAppRole, candidates.PendingLimitTest):
    pass


class ReviewAsAppRole(AsAppRole, candidates.ReviewTest):
    pass


class ApproveAsAppRole(AsAppRole, candidates.ApproveTest):
    pass


class RejectAsAppRole(AsAppRole, candidates.RejectTest):
    pass


class DecisionRaceAsAppRole(AsAppRole, candidates.DecisionRaceTest):
    pass


class PolicyWinsAsAppRole(AsAppRole, effective_view.PolicyWinsTest):
    pass


class PolicyWordingStaysInternalAsAppRole(
    AsAppRole, effective_view.PolicyWordingStaysInternalTest
):
    pass


class AgentsNeverManageAsAppRole(AsAppRole, refused.AgentsNeverManageTest):
    pass


class ApproveGuardsAsAppRole(AsAppRole, guards.ApproveGuardsTest):
    pass


class EditGuardsAsAppRole(AsAppRole, guards.EditGuardsTest):
    pass


# The audit rows of the operations are written to ``audit_events`` by the same
# application role (INSERT, SELECT), one action per operation.
class EachOperationHasItsOwnActionAsAppRole(
    AsAppRole, audit_actions.EachOperationHasItsOwnActionTest
):
    pass


class ARefusalNamesTheOperationAsAppRole(
    AsAppRole, audit_actions.ARefusalNamesTheOperationTest
):
    pass


class TheAdministrationViewsStayManageAsAppRole(
    AsAppRole, audit_actions.TheAdministrationViewsStayManageTest
):
    pass


class TheAuditRowComesBeforeTheChangeAsAppRole(
    AsAppRole, audit_actions.TheAuditRowComesBeforeTheChangeTest
):
    pass


class AnAuditFailureBlocksTheOperationAsAppRole(
    AsAppRole, audit_actions.AnAuditFailureBlocksTheOperationTest
):
    pass


# The completion row of a change is one more INSERT into ``audit_events`` by the
# same role, in the transaction of the change: it needs no privilege beyond the
# ones revision 0025 grants.
class EverySuccessfulChangeIsCompletedAsAppRole(
    AsAppRole, completion.EverySuccessfulChangeIsCompletedTest
):
    pass


class AFailedChangeIsNeverCompletedAsAppRole(
    AsAppRole, completion.AFailedChangeIsNeverCompletedTest
):
    pass


class ACompletionThatCannotBeWrittenAsAppRole(
    AsAppRole, completion.ACompletionThatCannotBeWrittenTakesTheChangeBackTest
):
    pass


class TheAttemptKeepsItsRulesAsAppRole(
    AsAppRole, completion.TheAttemptKeepsItsRulesTest
):
    pass


@requires_postgres
class AppRolePrivilegesTest(AsyncPostgresSharedTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = role_database(APP_ROLE)
        self.other = role_database(OTHER_ROLE)
        self.addAsyncCleanup(self.app.dispose)
        self.addAsyncCleanup(self.other.dispose)

    def owner_scalar(self, sql: str, **parameters: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), parameters).scalar()

    async def refused(self, database: Database, sql: str):
        async with database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql))
                await session.commit()
        self.assertIsInstance(
            caught.exception.orig, psycopg.errors.InsufficientPrivilege
        )

    async def test_the_service_really_runs_as_a_non_superuser_role(self):
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

    async def test_the_app_role_holds_exactly_the_expected_privileges(self):
        for table, (privileges, update_columns) in EXPECTED.items():
            with self.subTest(table=table):
                for privilege in ALL_PRIVILEGES:
                    granted = self.owner_scalar(
                        "SELECT has_table_privilege(:r, :t, :p)",
                        r=APP_ROLE,
                        t=table,
                        p=privilege,
                    )
                    self.assertEqual(granted, privilege in privileges, privilege)
                columns = self.rows(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = :t",
                    t=table,
                )
                updatable = {
                    row["column_name"]
                    for row in columns
                    if self.owner_scalar(
                        "SELECT has_column_privilege(:r, :t, :c, 'UPDATE')",
                        r=APP_ROLE,
                        t=table,
                        c=row["column_name"],
                    )
                }
                self.assertEqual(updatable, update_columns)

    async def test_the_app_role_cannot_rewrite_or_erase_the_audit_trail(self):
        # The completion rows live in the append-only table: the role that
        # writes them cannot change or remove them (nor can it drop the guards).
        actor = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_events (id, correlation_id, occurred_at,"
                    " actor_id, action, resource_kind, decision, reason)"
                    " VALUES (gen_random_uuid(), gen_random_uuid(), now(), :actor,"
                    " 'shared_memory.delete', 'shared_memory', 'allow', 'completed')"
                ),
                {"actor": actor},
            )
        for sql in (
            "UPDATE audit_events SET actor_id = NULL",
            "UPDATE audit_events SET reason = 'granted_by_system_role'",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
            "DROP TRIGGER tr_audit_events_reject_update_delete ON audit_events",
            "ALTER TABLE audit_events DISABLE TRIGGER ALL",
        ):
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)
        self.assertEqual(
            self.owner_scalar(
                "SELECT count(*) FROM audit_events WHERE actor_id = :a", a=actor
            ),
            1,
        )

    async def test_a_role_without_grants_reaches_no_candidate(self):
        self.seed_candidate()
        await self.refused(self.other, f"SELECT count(*) FROM {CANDIDATES}")
        await self.refused(self.other, f"DELETE FROM {CANDIDATES}")

    async def test_the_app_role_cannot_rewrite_delete_or_truncate_a_candidate(self):
        candidate_id = self.seed_candidate(state="approved")
        before = self.candidate_row(candidate_id)
        forbidden = [
            # What was proposed and by whom is never rewritten.
            f"UPDATE {CANDIDATES} SET content = 'changed'",
            f"UPDATE {CANDIDATES} SET title = 'changed'",
            f"UPDATE {CANDIDATES} SET memory_type = 'changed'",
            f"UPDATE {CANDIDATES} SET importance = 1",
            f"UPDATE {CANDIDATES} SET policy_subjects = '{{}}'",
            f"UPDATE {CANDIDATES} SET reason = 'changed'",
            f"UPDATE {CANDIDATES} SET proposer_user_id = gen_random_uuid()",
            f"UPDATE {CANDIDATES} SET proposer_agent_id = gen_random_uuid()",
            f"UPDATE {CANDIDATES} SET origin_scope = 'repo'",
            f"UPDATE {CANDIDATES} SET origin_version_id = gen_random_uuid()",
            f"UPDATE {CANDIDATES} SET created_at = now()",
            f"UPDATE {CANDIDATES} SET id = gen_random_uuid()",
            # The record of a decision is never erased.
            f"DELETE FROM {CANDIDATES}",
            f"TRUNCATE {CANDIDATES}",
            # The schema belongs to the migration role.
            f"ALTER TABLE {CANDIDATES} ADD COLUMN extra text",
            f"ALTER TABLE {CANDIDATES} DROP CONSTRAINT "
            f"ck_{CANDIDATES}_pending_has_no_decision",
            f"DROP INDEX ix_{CANDIDATES}_state_created_at",
            f"DROP TABLE {CANDIDATES}",
        ]
        for sql in forbidden:
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)
        self.assertEqual(self.candidate_row(candidate_id), before)

    async def test_the_app_role_cannot_rewrite_a_version_of_a_shared_memory(self):
        memory_id = self.seed_memory(title="Rule", content="Body")
        before = self.versions(memory_id)
        for sql in (
            "UPDATE memory_versions SET content = 'changed'",
            "UPDATE memory_versions SET title = 'changed'",
            "UPDATE memory_versions SET scope = 'user'",
            "UPDATE memory_versions SET confirmation_state = 'rejected'",
            "UPDATE memory_versions SET attributes = '{}'::jsonb",
            "DELETE FROM memory_versions",
            "UPDATE memory_relations SET reason = 'x'",
            "DELETE FROM memory_relations",
            "DELETE FROM memory_sources",
        ):
            with self.subTest(sql=sql):
                await self.refused(self.app, sql)
        self.assertEqual(self.versions(memory_id), before)

    async def test_the_app_role_can_propose_read_lock_and_decide_a_candidate(self):
        async with self.app.session() as session:
            await session.execute(
                text(
                    f"INSERT INTO {CANDIDATES} (proposer_user_id, origin_scope,"
                    " memory_type, title, content)"
                    " VALUES (gen_random_uuid(), 'user', 'rule', 'T', 'C')"
                )
            )
            await session.commit()
        async with self.app.session() as session:
            locked = (
                await session.execute(text(f"SELECT id FROM {CANDIDATES} FOR UPDATE"))
            ).all()
            self.assertEqual(len(locked), 1)
            await session.execute(
                text(
                    f"UPDATE {CANDIDATES} SET state = 'rejected',"
                    " decided_by = gen_random_uuid(), decided_at = now(),"
                    " decision_reason = 'no'"
                )
            )
            await session.commit()
        self.assertEqual(
            self.owner_scalar(f"SELECT state FROM {CANDIDATES}"), "rejected"
        )

    async def test_the_app_role_can_take_an_advisory_lock(self):
        async with self.app.session() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended('x', 0))")
                )


if __name__ == "__main__":
    unittest.main()
