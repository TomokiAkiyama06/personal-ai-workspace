"""A database error never carries a memory's text out of PAW-042 (Codex P1).

SQLAlchemy's error text lists the bound parameters (a title, a content), and
PostgreSQL's ``DETAIL`` of a check violation quotes the failing row. The versioning
service and the freshness jobs turn every database error into a typed error with
no ``__cause__`` / ``__context__``. The engine here is built WITHOUT
``hide_parameters`` (the shared ``Database`` sets it; the service must not depend on
that), and the failures are provoked by a check constraint and triggers that the
test adds as the schema's owner.
"""

import traceback
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from paw_backend.memory.models import MemoryScope
from paw_backend.memory.versioning import (
    FreshnessMaintenance,
    ManualRelation,
    MemoryChanges,
    MemoryDatabaseError,
    MemoryDraft,
    MemoryStateError,
    MemoryVersionConflictError,
    MemoryVersioningService,
    StateProblem,
)

from .memory_support import sync_database_url
from .versioning_support import T0, PostgresVersioningTestCase, requires_postgres

SECRET = "zq-private-memory-text-7731"


def exception_chain(error: BaseException) -> list[BaseException]:
    """``error`` and every exception reachable by ``__cause__`` / ``__context__``."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or any(current is known for known in seen):
            continue
        seen.append(current)
        pending += [current.__cause__, current.__context__]
    return seen


@requires_postgres
class DatabaseErrorPrivacyTest(PostgresVersioningTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        database = self._database()
        database._engine = create_async_engine(
            sync_database_url(), hide_parameters=False
        )
        self.addAsyncCleanup(database._engine.dispose)
        self.leaky_versioning = MemoryVersioningService(
            database, self.authorizer, clock=self.clock
        )
        self.leaky_freshness = FreshnessMaintenance(database, clock=self.clock)

    def execute(self, sql: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql))

    def reject_the_secret_by_a_check(self) -> None:
        self.execute(
            "ALTER TABLE memory_versions ADD CONSTRAINT paw_test_no_secret"
            f" CHECK (position('{SECRET}' IN content) = 0) NOT VALID"
        )
        self.addCleanup(
            self.execute,
            "ALTER TABLE memory_versions DROP CONSTRAINT IF EXISTS paw_test_no_secret",
        )

    def trigger(self, when: str, body: str) -> None:
        self.execute(
            "CREATE FUNCTION paw_test_versions() RETURNS trigger"
            f" LANGUAGE plpgsql AS $$ BEGIN {body} END $$"
        )
        self.execute(
            f"CREATE TRIGGER paw_test_versions {when} ON memory_versions"
            " FOR EACH ROW EXECUTE FUNCTION paw_test_versions()"
        )
        self.addCleanup(
            self.execute, "DROP FUNCTION IF EXISTS paw_test_versions() CASCADE"
        )

    def assert_leaks_nothing(self, error: BaseException) -> None:
        self.assertEqual(exception_chain(error), [error])
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for shown in (
            str(error),
            repr(error),
            repr(error.args),
            "".join(traceback.format_exception(error)),
        ):
            self.assertNotIn(SECRET, shown)

    def draft(self, content: str) -> MemoryDraft:
        return MemoryDraft(
            scope=MemoryScope.USER,
            memory_type="preference",
            title="deploy day",
            content=content,
        )

    async def test_a_check_violation_of_a_new_memory_quotes_nothing(self):
        self.reject_the_secret_by_a_check()
        me = self.user()
        with self.assertRaises(MemoryDatabaseError) as caught:
            await self.leaky_versioning.create_memory(me, self.draft(SECRET))
        self.assertEqual(caught.exception.sqlstate, "23514")
        self.assert_leaks_nothing(caught.exception)

    async def test_a_check_violation_of_an_edit_quotes_nothing(self):
        me = self.user()
        created = await self.versioning.create_memory(me, self.draft("friday"))
        self.reject_the_secret_by_a_check()
        with self.assertRaises(MemoryDatabaseError) as caught:
            await self.leaky_versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content=SECRET)
            )
        self.assert_leaks_nothing(caught.exception)
        self.assertEqual(len(self.versions(created.memory_id)), 1)

    async def test_a_trigger_that_quotes_the_row_leaks_nothing(self):
        me = self.user()
        created = await self.versioning.create_memory(me, self.draft(SECRET))
        # The trigger's own message quotes the content: the driver's text leaks.
        self.trigger(
            "BEFORE UPDATE",
            "RAISE EXCEPTION 'rejected: %', OLD.content;",
        )
        calls = (
            lambda: self.leaky_versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content="other")
            ),
            lambda: self.leaky_versioning.deprecate_memory(me, created.memory_id, 1),
        )
        for call in calls:
            with self.assertRaises(MemoryDatabaseError) as caught:
                await call()
            self.assert_leaks_nothing(caught.exception)
        self.assertEqual(self.versions(created.memory_id)[0].status, "active")

    async def test_a_version_race_is_a_conflict_without_the_driver_error(self):
        me = self.user()
        created = await self.versioning.create_memory(me, self.draft("friday"))
        # What a concurrent writer's version looks like to the INSERT.
        self.trigger(
            "BEFORE INSERT",
            "RAISE EXCEPTION USING ERRCODE = 'unique_violation',"
            " CONSTRAINT = 'uq_memory_versions_memory_id',"
            " MESSAGE = 'duplicate: ' || NEW.content;",
        )
        with self.assertRaises(MemoryVersionConflictError) as caught:
            await self.leaky_versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content=SECRET)
            )
        self.assert_leaks_nothing(caught.exception)

    def clash_on_insert(self, table: str, changes: str) -> None:
        """Before each INSERT into ``table``, insert a copy of the new row (with
        ``changes`` applied) so that the INSERT itself runs into the real unique
        constraint, the way a concurrent writer's committed row would."""
        self.execute(
            f"CREATE FUNCTION paw_test_clash() RETURNS trigger LANGUAGE plpgsql AS $$"
            f" DECLARE copy {table}; BEGIN"
            " IF pg_trigger_depth() = 1 THEN"
            f" copy := NEW; copy.id := gen_random_uuid(); {changes}"
            f" INSERT INTO {table} SELECT copy.*; END IF;"
            " RETURN NEW; END $$"
        )
        self.execute(
            f"CREATE TRIGGER paw_test_clash BEFORE INSERT ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION paw_test_clash()"
        )
        self.addCleanup(
            self.execute, "DROP FUNCTION IF EXISTS paw_test_clash() CASCADE"
        )

    async def test_a_real_version_number_clash_is_a_conflict(self):
        # Codex P2: the handler must know the constraint name PostgreSQL reports
        # (the naming convention's ``uq_memory_versions_memory_id``).
        me = self.user()
        created = await self.versioning.create_memory(me, self.draft("friday"))
        self.clash_on_insert("memory_versions", "copy.status := 'superseded';")
        with self.assertRaises(MemoryVersionConflictError) as caught:
            await self.leaky_versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content=SECRET)
            )
        self.assert_leaks_nothing(caught.exception)
        self.assertEqual(len(self.versions(created.memory_id)), 1)

    async def test_a_real_relation_clash_is_already_related(self):
        me = self.user()
        older = await self.versioning.create_memory(me, self.draft("friday"))
        newer = await self.versioning.create_memory(me, self.draft("monday"))
        self.clash_on_insert("memory_relations", "")
        with self.assertRaises(MemoryStateError) as caught:
            await self.leaky_versioning.relate_memories(
                me,
                ManualRelation.EXTENDS,
                newer_memory_id=newer.memory_id,
                newer_expected_version=1,
                older_memory_id=older.memory_id,
                older_expected_version=1,
            )
        self.assertEqual(caught.exception.problem, StateProblem.ALREADY_RELATED)
        self.assert_leaks_nothing(caught.exception)
        self.assertEqual(self.relations(), [])

    async def test_a_freshness_job_leaks_nothing(self):
        me = self.user()
        self.seed(
            "this month only",
            SECRET,
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0 - timedelta(days=1),
        )
        self.trigger(
            "BEFORE UPDATE",
            "RAISE EXCEPTION 'rejected: %', OLD.content;",
        )
        with self.assertRaises(MemoryDatabaseError) as caught:
            await self.leaky_freshness.expire_due()
        self.assert_leaks_nothing(caught.exception)
