"""Revision 0083: the index ``tasks (project_id, state)`` and the queries that use it.

Issue #83 (Decision 0008, section 8, item 6): ``tasks.project_id`` had no index, so
the project module's reads of "the tasks of this project" scanned every task ever
created. The first classes need no server. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the migration up and down, compare it with
the model through Alembic's autogenerate, and plan every statement of the project
module that reads ``tasks`` (also as a prepared statement, ``force_generic_plan``).
"""

import io
import unittest
import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from paw_backend.db import Base
from paw_backend.projects import store
from paw_backend.tasks.models import TaskRow

from .memory_support import migrate as migrate_by_action
from .memory_support import sync_database_url
from .support import paw_environment
from .task_support import PostgresTaskTestCase, requires_postgres
from .test_migrations import offline_config

REVISION = "0083"
INDEX = "ix_tasks_project_id_state"


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def offline_sql(action: str, revisions: str) -> str:
    output = io.StringIO()
    with paw_environment(PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw"):
        getattr(command, action)(offline_config(output), revisions, sql=True)
    return output.getvalue()


class IndexDefinitionTest(unittest.TestCase):
    def test_the_model_declares_one_named_index_on_project_and_state(self):
        indexes = {index.name: index for index in TaskRow.__table__.indexes}

        self.assertEqual(list(indexes), [INDEX])
        self.assertEqual(
            [column.name for column in indexes[INDEX].columns], ["project_id", "state"]
        )
        self.assertFalse(indexes[INDEX].unique)
        self.assertIsNone(indexes[INDEX].dialect_options["postgresql"]["where"])

    def test_the_upgrade_only_creates_the_index_and_the_downgrade_drops_it(self):
        up = offline_sql("upgrade", f"{previous_revision()}:{REVISION}")
        down = offline_sql("downgrade", f"{REVISION}:{previous_revision()}")

        self.assertIn(f"CREATE INDEX {INDEX} ON tasks (project_id, state);", up)
        self.assertEqual(up.count("CREATE"), 1)
        self.assertNotIn("ALTER TABLE", up)
        self.assertNotIn("GRANT", up)  # no table, so no privilege
        self.assertIn(f"DROP INDEX {INDEX};", down)
        self.assertEqual(down.count("DROP"), 1)

    def test_the_revision_is_a_single_step_after_its_parent(self):
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        revision = scripts.get_revision(REVISION)

        self.assertEqual(revision.revision, REVISION)
        self.assertIsInstance(revision.down_revision, str)


def only_the_index_objects(obj, name, type_, reflected, compare_to):
    table = obj if type_ == "table" else getattr(obj, "table", None)
    return table is not None and table.name == "tasks"


@requires_postgres
class IndexMigrationTest(unittest.TestCase):
    """Up, down, up again on a real PostgreSQL, and no drift from the model."""

    def setUp(self) -> None:
        migrate_by_action("downgrade", "base")
        self.addCleanup(migrate_by_action, "upgrade", "head")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def index_definitions(self) -> dict[str, str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes"
                    " WHERE schemaname = 'public' AND tablename = 'tasks'"
                )
            )
            return {name: definition for name, definition in rows}

    def test_upgrade_builds_the_index_and_downgrade_removes_it(self):
        previous = previous_revision()

        migrate_by_action("upgrade", REVISION)

        definitions = self.index_definitions()
        self.assertIn(INDEX, definitions)
        self.assertTrue(
            definitions[INDEX].endswith("USING btree (project_id, state)"),
            definitions[INDEX],
        )

        migrate_by_action("downgrade", previous)

        self.assertNotIn(INDEX, self.index_definitions())
        self.assertIn("pk_tasks", self.index_definitions())

    def test_the_whole_chain_runs_up_down_and_up_again(self):
        migrate_by_action("upgrade", "head")
        self.assertIn(INDEX, self.index_definitions())

        migrate_by_action("downgrade", "base")
        with self.engine.connect() as connection:
            tables = set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                ).scalars()
            )
        self.assertNotIn("tasks", tables)

        migrate_by_action("upgrade", "head")
        self.assertIn(INDEX, self.index_definitions())

    def test_autogenerate_finds_no_difference_between_the_model_and_the_migration(
        self,
    ):
        migrate_by_action("upgrade", "head")

        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": only_the_index_objects,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_the_drift_check_notices_a_missing_index(self):
        migrate_by_action("upgrade", "head")

        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(text(f"DROP INDEX {INDEX}"))
            context = MigrationContext.configure(
                connection, opts={"include_object": only_the_index_objects}
            )
            difference = compare_metadata(context, Base.metadata)
            transaction.rollback()

        self.assertEqual(
            [(step[0], step[1].name) for step in difference], [("add_index", INDEX)]
        )


@requires_postgres
class IndexPlanTest(PostgresTaskTestCase):
    """Every statement that reads a project's tasks uses an index, in both plan modes.

    The tables hold a long history of finished tasks and entries of OTHER projects,
    so a sequential scan would read every one of them. ``plan()`` (bitmap scans off)
    explains each statement the project module really sends, as a prepared
    statement planned with the parameters (``force_custom_plan``) and without
    them (``force_generic_plan``, what the driver may cache): the states and the
    queue statuses are written into the SQL text, which is what lets the generic
    plan use ``ix_tasks_project_id_state`` and the partial queue indexes.
    """

    HISTORY = 20_000
    STATES = ("completed", "failed", "cancelled", "queued", "running")

    async def owner_sql(self, sql: str, **parameters) -> None:
        async with self.database.engine.begin() as connection:
            await connection.execute(text(sql), parameters)

    async def busy_tables(self) -> uuid.UUID:
        """A long history of other projects and one project with active tasks."""
        await self.owner_sql("TRUNCATE tasks CASCADE")
        await self.owner_sql(
            "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
            " attempt, retry_count, version, created_at, updated_at)"
            " SELECT gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 't',"
            " '{}', (ARRAY['completed', 'failed', 'cancelled'])[1 + g % 3], 1, 0, 1,"
            " now(), now() FROM generate_series(1, :n) g",
            n=self.HISTORY,
        )
        project_id = uuid.uuid4()
        await self.owner_sql(
            "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
            " attempt, retry_count, version, created_at, updated_at)"
            " SELECT gen_random_uuid(), :p, gen_random_uuid(), 't', '{}',"
            " (ARRAY['completed', 'failed', 'cancelled', 'queued', 'running'])"
            "[1 + g % 5], 1, 0, 1, now(), now() FROM generate_series(1, 30) g",
            p=project_id,
        )
        # The finished history of the queue (one completed entry per old task)
        # and an active entry for the queued and the cancelled tasks of the project.
        await self.owner_sql(
            "INSERT INTO queue_entries (task_id, priority, priority_rank, status,"
            " enqueued_at, claimed_by, claimed_at, claim_count, finished_at)"
            " SELECT id, 'normal', 1, 'completed', now(), 'w', now(), 1, now()"
            " FROM tasks WHERE project_id <> :p",
            p=project_id,
        )
        await self.owner_sql(
            "INSERT INTO queue_entries (task_id, priority, priority_rank, status,"
            " enqueued_at, claim_count) SELECT id, 'normal', 1, 'queued', now(), 0"
            " FROM tasks WHERE project_id = :p AND state IN ('queued', 'cancelled')",
            p=project_id,
        )
        await self.owner_sql("ANALYZE tasks")
        await self.owner_sql("ANALYZE queue_entries")
        return project_id

    async def sent(self, project_id: uuid.UUID):
        """The statements ``store`` sends for the reads of the stop processor."""
        with self.captured_statements() as captured:
            async with self.database.session() as session, session.begin():
                active = await store.select_active_task_ids(session, project_id, 100)
                one = await store.has_active_task(session, project_id)
                entries = await store.select_active_entry_task_ids(
                    session, project_id, 100
                )
                strays = await store.select_active_entry_task_ids(
                    session, project_id, 100, terminal_tasks_only=True
                )
                any_entry = await store.has_active_queue_entry(session, project_id)
        statements = [s for s in captured if "tasks" in s[0]]
        return statements, (active, one, entries, strays, any_entry)

    async def indexes_of(self, statement: tuple, mode: str) -> dict[str, list]:
        sql, parameters = statement
        (explained,) = await self.plan(sql, parameters, mode)
        nodes = list(self.plan_nodes(explained["Plan"]))
        self.assertNotIn(
            "Seq Scan", {node["Node Type"] for node in nodes}, f"{mode}: {sql}"
        )
        found: dict[str, list] = {}
        for node in nodes:
            if "Index Name" in node:
                found.setdefault(node["Relation Name"], []).append(node["Index Name"])
        return found

    async def test_the_reads_return_what_the_plans_are_checked_for(self):
        project_id = await self.busy_tables()

        statements, (active, one, entries, strays, any_entry) = await self.sent(
            project_id
        )

        # 30 tasks: 6 of each state; queued and running are the active ones.
        self.assertEqual(len(active), 12)
        self.assertTrue(one)
        # An entry for the 6 queued and the 6 cancelled tasks; the cancelled are strays.
        self.assertEqual((len(entries), len(strays)), (12, 6))
        self.assertTrue(any_entry)
        self.assertEqual(len(statements), 5)

    async def test_the_list_of_active_tasks_uses_the_project_state_index(self):
        project_id = await self.busy_tables()
        statements, _ = await self.sent(project_id)

        for statement in (statements[0], statements[1]):  # the list, "is any active?"
            self.assertIn("tasks.state IN (", statement[0])
            self.assertNotIn("queue_entries", statement[0])
            for mode in ("force_custom_plan", "force_generic_plan"):
                with self.subTest(mode=mode):
                    self.assertEqual(
                        await self.indexes_of(statement, mode), {"tasks": [INDEX]}
                    )

    async def test_the_reads_through_the_queue_use_indexes_on_both_tables(self):
        project_id = await self.busy_tables()
        statements, _ = await self.sent(project_id)

        joins = [s for s in statements if "queue_entries" in s[0]]
        self.assertEqual(len(joins), 3)
        for statement in joins:
            self.assertIn("queue_entries.status IN ('claimed', 'queued')", statement[0])
            for mode in ("force_custom_plan", "force_generic_plan"):
                with self.subTest(mode=mode, sql=statement[0][-80:]):
                    found = await self.indexes_of(statement, mode)
                    self.assertEqual(sorted(found), ["queue_entries", "tasks"])
                    self.assertTrue(
                        set(found["queue_entries"])
                        <= {
                            "ix_queue_entries_claim_order",
                            "uq_queue_entries_one_active_per_task",
                        },
                        found,
                    )
                    self.assertTrue(set(found["tasks"]) <= {INDEX, "pk_tasks"}, found)


if __name__ == "__main__":
    unittest.main()
