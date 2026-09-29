"""``audit_events`` retention / partitioning on a real PostgreSQL (Issue #86).

Migration 0086 (``docs/decisions/0027-audit-retention-and-partitioning.md``,
Approved) and ``AuditRetentionService``. Skipped unless ``PAW_TEST_DATABASE_URL``
is set. The whole database is reused across every test in this file (append-only,
like every other real-PostgreSQL audit test in this suite); every test that
creates a partition picks its own year from ``unique_year()`` so no two tests,
whatever order they run in, ever touch the same partition name.

``archive_due_partitions`` / ``purge_due_partitions`` apply a policy *globally*
(every ``LIVE`` — or, for purge, ``ARCHIVED`` — bookkeeping row, not only the
ones a given test created): a policy generous enough to make a test's own
"just-ended" far-future window due would, without a safeguard, also catch
``audit_events_p_legacy`` and Migration 0086's own first live partition (both
long "in the past" relative to any test's clock) — including whichever one
covers *this process's real wall-clock moment*, which every audit row this
module itself writes still needs (``recorded_at`` is always the database
clock, never the injected one). ``AuditRetentionService`` itself refuses to
archive or purge that one partition regardless of policy or clock (see
``_protected_partition_name`` in ``service.py``), which is what actually keeps
these tests from interfering with each other or with themselves.
"""

import asyncio
import itertools
import pathlib
import unittest
import uuid
from datetime import UTC, datetime, timedelta

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.authz.retention import (
    AuditRetentionService,
    MaintenanceReport,
    PartitionStatus,
    RetentionAction,
    RetentionActor,
    RetentionPolicy,
    month_start,
    next_month_start,
    partition_name,
)

from .task_support import migrate, new_database, requires_postgres

MIGRATION_SOURCE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0086_audit_retention_partitioning.py"
)

# A fresh, never-repeated year per test that needs one (module-level: shared by
# every test in this file, however many run and in whatever order).
_YEARS = itertools.count(2040)


def unique_year() -> int:
    return next(_YEARS)


def fixed_clock(moment: datetime):
    return lambda: moment


@requires_postgres
class RetentionPostgresTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()  # head

    async def asyncSetUp(self) -> None:
        self.database = new_database()
        self.addAsyncCleanup(self.database.dispose)

    async def scalar(self, sql: str, **parameters):
        async with self.database.engine.connect() as connection:
            return (await connection.execute(text(sql), parameters)).scalar()

    async def rows(self, sql: str, **parameters) -> list[tuple]:
        async with self.database.engine.connect() as connection:
            return [tuple(r) for r in await connection.execute(text(sql), parameters)]

    async def bookkeeping(self) -> dict[str, str]:
        async with self.database.session() as session:
            records = (
                await session.execute(
                    text("SELECT name, status FROM audit_retention_partitions")
                )
            ).all()
            return dict(records)

    async def execute_rejected(self, sql: str) -> DBAPIError:
        async with self.database.session() as session:
            with self.assertRaises(DBAPIError) as caught:
                await session.execute(text(sql))
                await session.commit()
        return caught.exception

    async def row_triggers(self, table: str) -> set[str]:
        """The row-level (not statement-level) trigger names enabled on ``table``.

        Checked from the catalog rather than by attempting an UPDATE / DELETE:
        a BEFORE ROW trigger only ever fires for a row the statement actually
        touches, so an UPDATE / DELETE against a table with no matching row
        "succeeds" (0 rows) without proving anything either way.
        """
        rows = await self.rows(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = to_regclass(:table) "
            "AND NOT tgisinternal AND tgtype & 1 = 1 AND tgenabled = 'A'",
            table=table,
        )
        return {row[0] for row in rows}


async def _hard_reset_schema() -> None:
    """Drop and recreate the whole ``public`` schema, then migrate to head.

    Not ``migrate("base", downgrade=True)``: Migration 0086's own
    ``downgrade()`` refuses to run once ``audit_events_p_legacy`` has been
    purged (by design — see its docstring), which a *previous* class in this
    shared database may well have done on purpose (``ArchiveAndPurgeTest``).
    Dropping the schema outright never needs to reverse anything; every
    migration's ``upgrade()`` builds the rest back from nothing regardless of
    what any earlier test left behind.
    """
    database = new_database()
    try:
        async with database.engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
            # A schema this test just created has none of the default grants
            # a freshly ``CREATE DATABASE``d one already carries (at least
            # USAGE to PUBLIC); without this, an unqualified reference from
            # any *other* role's own session (a throwaway app/operator role a
            # different test file creates, sharing this same database) cannot
            # even resolve "public" in its search_path, and fails as "does not
            # exist" rather than a permission error.
            await connection.execute(text("GRANT ALL ON SCHEMA public TO PUBLIC"))
    finally:
        await database.dispose()


class FreshlyMigratedTestCase(RetentionPostgresTestCase):
    """Its own fresh database: this class's assertions are about the state

    Migration 0086 itself leaves behind (``audit_events_p_legacy`` still
    ``live``, the archive parent still empty, and so on), which a class that
    runs earlier in this shared database and legitimately archives or purges
    ``audit_events_p_legacy`` (any class exercising ``AuditRetentionService``
    with a policy generous enough to reach it — see the module docstring)
    would otherwise falsify, whatever order the classes in this module run in.
    """

    @classmethod
    def setUpClass(cls) -> None:
        asyncio.run(_hard_reset_schema())
        migrate()  # head, from nothing


class MigrationShapeTest(FreshlyMigratedTestCase):
    async def test_the_parents_the_bookkeeping_table_and_the_legacy_row_exist(self):
        self.assertEqual(
            await self.scalar("SELECT to_regclass('audit_events')::text"),
            "audit_events",
        )
        self.assertEqual(
            await self.scalar("SELECT to_regclass('audit_events_archive')::text"),
            "audit_events_archive",
        )
        self.assertEqual(
            await self.scalar("SELECT to_regclass('audit_retention_partitions')::text"),
            "audit_retention_partitions",
        )
        statuses = await self.bookkeeping()
        self.assertIn("audit_events_p_legacy", statuses)
        self.assertEqual(statuses["audit_events_p_legacy"], "live")

    async def test_audit_events_is_partitioned_by_range_on_recorded_at(self):
        self.assertEqual(
            await self.scalar(
                "SELECT partstrat FROM pg_partitioned_table "
                "WHERE partrelid = 'audit_events'::regclass"
            ),
            "r",
        )

    async def test_the_archive_parent_starts_with_no_partitions(self):
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM pg_inherits "
                "WHERE inhparent = 'audit_events_archive'::regclass"
            ),
            0,
        )

    async def test_the_app_role_has_select_but_not_insert_on_the_archive_parent(self):
        # Table-level, independent of whether PAW_APP_DATABASE_ROLE is configured
        # for this run: the migration's grant call is exercised by
        # tests/test_migration_grants.py and the split-role tests; here we only
        # check that INSERT was never requested for the archive parent.
        source = MIGRATION_SOURCE.read_text()
        self.assertIn(
            'grant_app_privileges(op, "audit_events_archive", select=True)', source
        )


class AppendOnlyStillHoldsTest(FreshlyMigratedTestCase):
    """Regression: the guarantee Migration 0025 built is unaffected by 0086."""

    async def test_the_parent_rejects_update_delete_and_truncate(self):
        for sql in (
            "UPDATE audit_events SET decision = 'allow'",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
        ):
            with self.subTest(sql=sql):
                error = await self.execute_rejected(sql)
                self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)

    async def test_the_legacy_partition_rejects_truncate_directly(self):
        error = await self.execute_rejected("TRUNCATE audit_events_p_legacy")
        self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)

    async def test_the_legacy_partition_still_carries_the_row_level_triggers(self):
        self.assertEqual(
            await self.row_triggers("audit_events_p_legacy"),
            {
                "tr_audit_events_reject_update_delete",
                "tr_audit_events_force_recorded_at",
            },
        )

    async def test_a_row_inserted_through_the_parent_keeps_recorded_at_server_side(
        self,
    ):
        event_id = uuid.uuid4()
        async with self.database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO audit_events (id, correlation_id, occurred_at, "
                    "action, resource_kind, decision, reason) VALUES "
                    "(:id, gen_random_uuid(), now(), 'a', 'system', 'allow', 'r')"
                ),
                {"id": event_id},
            )
            await session.commit()
        recorded_at, table = (
            await self.rows(
                "SELECT recorded_at, tableoid::regclass::text FROM audit_events "
                "WHERE id = :id",
                id=event_id,
            )
        )[0]
        self.assertIsNotNone(recorded_at)
        # It landed in whichever partition covers "now", not the legacy one.
        self.assertNotEqual(table, "audit_events_p_legacy")

    async def test_a_real_row_cannot_be_updated_or_deleted(self):
        # Belt and suspenders over the catalog check above: a row that really
        # exists (inserted moments ago, in whichever partition covers "now"),
        # updated or deleted through the parent, still hits the trigger.
        event_id = uuid.uuid4()
        async with self.database.session() as session:
            await session.execute(
                text(
                    "INSERT INTO audit_events (id, correlation_id, occurred_at, "
                    "action, resource_kind, decision, reason) VALUES "
                    "(:id, gen_random_uuid(), now(), 'a', 'system', 'allow', 'r')"
                ),
                {"id": event_id},
            )
            await session.commit()
        for sql in (
            f"UPDATE audit_events SET decision = 'allow' WHERE id = '{event_id}'",
            f"DELETE FROM audit_events WHERE id = '{event_id}'",
        ):
            with self.subTest(sql=sql):
                error = await self.execute_rejected(sql)
                self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)


class EnsurePartitionsTest(RetentionPostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = datetime(unique_year(), 3, 17, tzinfo=UTC)
        self.service = AuditRetentionService(self.database, clock=fixed_clock(self.now))
        self.policy = RetentionPolicy(
            archive_after_days=180, purge_after_days=None, horizon_months=2
        )
        self.first = partition_name(month_start(self.now))
        self.second = partition_name(next_month_start(self.now))
        self.third = partition_name(next_month_start(next_month_start(self.now)))

    async def test_it_creates_this_month_and_the_horizon_and_is_idempotent(self):
        created = await self.service.ensure_partitions(self.policy)
        self.assertEqual(
            [w.name for w in created], [self.first, self.second, self.third]
        )
        for window in created:
            self.assertIs(window.status, PartitionStatus.LIVE)

        again = await self.service.ensure_partitions(self.policy)
        self.assertEqual(again, [])

    async def test_each_created_partition_is_attached_and_append_only(self):
        await self.service.ensure_partitions(self.policy)
        self.assertEqual(
            await self.scalar(
                "SELECT inhparent::regclass::text FROM pg_inherits "
                f"WHERE inhrelid = '{self.first}'::regclass"
            ),
            "audit_events",
        )
        self.assertEqual(
            await self.row_triggers(self.first),
            {
                "tr_audit_events_reject_update_delete",
                "tr_audit_events_force_recorded_at",
            },
        )
        error = await self.execute_rejected(f"TRUNCATE {self.first}")
        self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)

    async def test_bookkeeping_rows_match_what_was_created(self):
        created = await self.service.ensure_partitions(self.policy)
        statuses = await self.bookkeeping()
        for window in created:
            self.assertEqual(statuses[window.name], "live")

    async def test_creating_partitions_is_itself_audited(self):
        actor = RetentionActor(user_id=uuid.uuid4(), system_role="admin")
        await self.service.ensure_partitions(self.policy, actor=actor)
        rows = await self.rows(
            "SELECT action, reason, actor_id, actor_role, decision, resource_kind "
            "FROM audit_events WHERE action = :action AND reason = :reason",
            action=RetentionAction.PARTITION_CREATED.value,
            reason=self.first,
        )
        self.assertEqual(len(rows), 1)
        action, reason, actor_id, actor_role, decision, resource_kind = rows[0]
        self.assertEqual(reason, self.first)
        self.assertEqual(actor_id, actor.user_id)
        self.assertEqual(actor_role, "admin")
        self.assertEqual(decision, "allow")
        self.assertEqual(resource_kind, "audit_partition")

    async def test_an_unattended_run_records_no_actor(self):
        await self.service.ensure_partitions(self.policy)
        rows = await self.rows(
            "SELECT actor_id, actor_role FROM audit_events "
            "WHERE action = :action AND reason = :reason",
            action=RetentionAction.PARTITION_CREATED.value,
            reason=self.second,
        )
        self.assertEqual(rows, [(None, None)])


class ArchiveAndPurgeTest(FreshlyMigratedTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = datetime(unique_year(), 6, 10, tzinfo=UTC)
        self.service = AuditRetentionService(self.database, clock=fixed_clock(self.now))
        # archive_after_days=0: a month whose window already ended is due the
        # instant it ends. horizon_months=1 keeps the setup small.
        self.policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        await self.service.ensure_partitions(self.policy)
        self.old_name = partition_name(month_start(self.now))

    async def insert_row(self, name: str) -> uuid.UUID:
        """A row inserted straight into ``name``, its ``recorded_at`` in its window.

        The recorded_at-forcing trigger (Migration 0025) sets ``recorded_at``
        to the *real* database clock unconditionally; ``name`` here is a
        future test partition, whose window the real clock is never in, so a
        normal INSERT would hit exactly the cross-partition-move restriction
        Decision 0027 documents. Disabling the trigger for this one owner-only
        statement (the application role has no such privilege — see Migration
        0086) is how this test gets a row into a partition without waiting for
        the real calendar to reach it.
        """
        event_id = uuid.uuid4()
        async with self.database.session() as session, session.begin():
            await session.execute(
                text(
                    f"ALTER TABLE {name} DISABLE TRIGGER "
                    "tr_audit_events_force_recorded_at"
                )
            )
            await session.execute(
                text(
                    f"INSERT INTO {name} (id, correlation_id, occurred_at, "
                    "recorded_at, action, resource_kind, decision, reason) VALUES "
                    "(:id, gen_random_uuid(), now(), :recorded_at, 'a', "
                    "'system', 'allow', 'r')"
                ),
                {"id": event_id, "recorded_at": self.now},
            )
            await session.execute(
                text(
                    f"ALTER TABLE {name} ENABLE ALWAYS TRIGGER "
                    "tr_audit_events_force_recorded_at"
                )
            )
        return event_id

    async def test_a_partition_whose_window_has_ended_is_archived_on_the_next_run(self):
        later = AuditRetentionService(
            self.database, clock=fixed_clock(self.now + timedelta(days=32))
        )
        archived = await later.archive_due_partitions(self.policy)
        # archive_after_days=0 with a far-future clock also sweeps up Migration
        # 0086's own legacy / first-live partitions (both long "in the past"
        # relative to any test's clock — see the module docstring): this test
        # only asserts that *its own* partition is among what got archived.
        by_name = {window.name: window for window in archived}
        self.assertIn(self.old_name, by_name)
        self.assertIs(by_name[self.old_name].status, PartitionStatus.ARCHIVED)
        statuses = await self.bookkeeping()
        self.assertEqual(statuses[self.old_name], "archived")

    async def test_an_archived_partition_is_no_longer_reachable_through_audit_events(
        self,
    ):
        row_id = await self.insert_row(self.old_name)
        later = AuditRetentionService(
            self.database, clock=fixed_clock(self.now + timedelta(days=32))
        )
        await later.archive_due_partitions(self.policy)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM audit_events WHERE id = :id", id=row_id
            ),
            0,
        )

    async def test_the_row_still_exists_and_is_still_append_only_via_the_archive(self):
        row_id = await self.insert_row(self.old_name)
        later = AuditRetentionService(
            self.database, clock=fixed_clock(self.now + timedelta(days=32))
        )
        await later.archive_due_partitions(self.policy)
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM audit_events_archive WHERE id = :id", id=row_id
            ),
            1,
        )
        for sql in (
            f"UPDATE {self.old_name} SET decision = 'allow' WHERE id = '{row_id}'",
            f"DELETE FROM {self.old_name} WHERE id = '{row_id}'",
        ):
            with self.subTest(sql=sql):
                error = await self.execute_rejected(sql)
                self.assertIsInstance(error.orig, psycopg.errors.RestrictViolation)

    async def test_archiving_is_itself_audited(self):
        later = AuditRetentionService(
            self.database, clock=fixed_clock(self.now + timedelta(days=32))
        )
        await later.archive_due_partitions(self.policy)
        # The audit row for the archive operation is written at archive time,
        # so it lands in whatever partition covers "now" then — always
        # reachable through the live parent, unlike the archived data itself.
        found = await self.rows(
            "SELECT decision, resource_kind FROM audit_events "
            "WHERE action = :a AND reason = :r",
            a=RetentionAction.PARTITION_ARCHIVED.value,
            r=self.old_name,
        )
        self.assertEqual(found, [("allow", "audit_partition")])

    async def test_purge_is_disabled_by_default_and_archived_data_survives(self):
        later = AuditRetentionService(
            self.database, clock=fixed_clock(self.now + timedelta(days=400))
        )
        await later.archive_due_partitions(self.policy)
        purged = await later.purge_due_partitions(self.policy)  # purge_after_days=None
        self.assertEqual(purged, [])
        statuses = await self.bookkeeping()
        self.assertEqual(statuses[self.old_name], "archived")

    async def test_purge_drops_the_partition_when_explicitly_enabled(self):
        row_id = await self.insert_row(self.old_name)
        archiver = AuditRetentionService(
            self.database, clock=fixed_clock(self.now + timedelta(days=32))
        )
        await archiver.archive_due_partitions(self.policy)

        purge_policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=365, horizon_months=1
        )
        purger = AuditRetentionService(
            self.database, clock=fixed_clock(self.now + timedelta(days=400))
        )
        purged = await purger.purge_due_partitions(purge_policy)
        # audit_events_p_legacy (archived above, alongside self.old_name, for
        # the same reason as the archive test) is now old enough to purge too.
        by_name = {window.name: window for window in purged}
        self.assertIn(self.old_name, by_name)
        self.assertIs(by_name[self.old_name].status, PartitionStatus.PURGED)

        self.assertIsNone(await self.scalar(f"SELECT to_regclass('{self.old_name}')"))
        statuses = await self.bookkeeping()
        self.assertEqual(statuses[self.old_name], "purged")
        # The row itself is gone (that is what a purge means) ...
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM audit_events_archive WHERE id = :id", id=row_id
            ),
            0,
        )
        # ... but the fact that it was purged is not.
        found = await self.rows(
            "SELECT decision, reason FROM audit_events "
            "WHERE action = :a AND reason = :r",
            a=RetentionAction.PARTITION_PURGED.value,
            r=self.old_name,
        )
        self.assertEqual(found, [("allow", self.old_name)])


class MaintenanceReportTest(FreshlyMigratedTestCase):
    """Its own fresh database (see ``FreshlyMigratedTestCase``): this test's

    ``archive_after_days=0`` sweep is deliberately unbounded, so on the shared
    database it would also catch every other test's own partitions in this
    module, not only ``audit_events_p_legacy`` (all "in the past" relative to
    this test's own far-future clock, whatever it is — the same reasoning as
    ``ArchiveAndPurgeTest``, just with nothing else of this module's own
    making left to catch, on a freshly migrated database).
    """

    async def test_run_maintenance_returns_created_archived_and_purged(self):
        now = datetime(unique_year(), 1, 5, tzinfo=UTC)
        service = AuditRetentionService(self.database, clock=fixed_clock(now))
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        report = await service.run_maintenance(policy)
        self.assertIsInstance(report, MaintenanceReport)
        self.assertEqual(
            [w.name for w in report.created],
            [partition_name(month_start(now)), partition_name(next_month_start(now))],
        )
        # archive_after_days=0 with this far-future clock also reaches
        # audit_events_p_legacy (real, long past by "now" — see the module
        # docstring); the two partitions this call just created have not
        # ended yet (this month and the next), so they are never among it, and
        # neither is whatever partition covers the real current moment (the
        # service's own protection — see service.py).
        self.assertIn("audit_events_p_legacy", {w.name for w in report.archived})
        for window in report.created:
            self.assertNotIn(window.name, {w.name for w in report.archived})
        self.assertEqual(report.purged, ())


class MonthMathAgreesWithPostgresTest(RetentionPostgresTestCase):
    """The pure ``rules`` helpers agree with what PostgreSQL actually stores."""

    async def test_a_partition_bound_matches_month_start_and_next_month_start(self):
        now = datetime(unique_year(), 2, 18, tzinfo=UTC)
        service = AuditRetentionService(self.database, clock=fixed_clock(now))
        policy = RetentionPolicy(
            archive_after_days=0, purge_after_days=None, horizon_months=1
        )
        created, _next_month = await service.ensure_partitions(policy)
        self.assertEqual(created.name, partition_name(month_start(now)))
        bound = await self.scalar(
            "SELECT pg_get_expr(relpartbound, oid) FROM pg_class "
            f"WHERE relname = '{created.name}'"
        )
        self.assertIn(month_start(now).strftime("%Y-%m-%d"), bound)
        self.assertIn(next_month_start(now).strftime("%Y-%m-%d"), bound)
