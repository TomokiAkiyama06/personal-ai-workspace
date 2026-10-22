"""Revision 0279: ``tasks.read_only`` (issue #279, Decision 0094, 1 A).

The first class needs no server (the rendered SQL). The PostgreSQL class
(skipped unless ``PAW_TEST_DATABASE_URL`` is set) runs the revision down and up
again, and refuses the downgrade while a read-only task is active (the code
before 0279 would run it as an ordinary one). Nothing here assumes 0279 is the
head: the previous revision is read from the script directory.
"""

import asyncio
import io
import unittest
import uuid
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.tasks import RepoRole, TaskCommand, WorkingSetEntry

from .support import paw_environment
from .task_support import (
    BASELINE,
    PostgresTaskTestCase,
    migrate,
    new_database,
    requires_postgres,
)
from .test_deploy_release import load_tool
from .test_migrations import offline_config

REVISION = "0279"


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://paw:pw@db.internal/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_column_is_added_with_a_constant_default(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertIn(
            "ALTER TABLE tasks ADD COLUMN read_only BOOLEAN DEFAULT false NOT NULL",
            sql,
        )
        self.assertNotIn("UPDATE tasks", sql)
        self.assertNotIn("GRANT", sql)

    def test_the_downgrade_is_refused_before_the_column_is_dropped(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("RAISE EXCEPTION", sql)
        self.assertLess(
            sql.index("RAISE EXCEPTION"), sql.index("DROP COLUMN read_only")
        )

    def test_the_downgrade_keeps_every_task_that_can_run_again(self):
        # Only a completed task never runs again (failed / cancelled reopen).
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("read_only AND state <> 'completed'", sql)

    def test_a_rollback_does_not_keep_this_schema(self):
        # Codex review of #284 (P1): the release before 0279 runs on the schema
        # but would run an unfinished read-only task as an ordinary one, so the
        # revision is not ``expand`` (Decision 0079, 4): a rollback over it
        # needs a restore point.
        scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
        module = scripts.get_revision(REVISION).module
        self.assertNotEqual(getattr(module, "paw_compatibility", None), "expand")
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        migrations = load_tool().read_migrations(versions)
        self.assertNotEqual(migrations[REVISION].compatibility, "expand")

    def test_the_revision_follows_the_head_it_was_written_on(self):
        # Re-chained onto 0239 (#276), which was written on 0194 and merged first.
        self.assertEqual(previous_revision(), "0239")


@requires_postgres
class RoundTripTest(PostgresTaskTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.owner_sql("TRUNCATE tasks CASCADE")

    async def asyncTearDown(self):
        await asyncio.to_thread(migrate)
        await super().asyncTearDown()

    async def owner_sql(self, sql: str) -> None:
        owner = new_database()
        try:
            async with owner.engine.begin() as connection:
                await connection.execute(text(sql))
        finally:
            await owner.dispose()

    async def read_only_task(self) -> uuid.UUID:
        return await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, RepoRole.REFERENCED, BASELINE)
            ],
            read_only=True,
        )

    async def column_exists(self) -> bool:
        async with self.database.engine.connect() as connection:
            return bool(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns"
                        " WHERE table_name = 'tasks' AND column_name = 'read_only'"
                    )
                )
            )

    async def refused(self) -> None:
        with self.assertRaises(DBAPIError) as raised:
            await asyncio.to_thread(migrate, previous_revision(), downgrade=True)
        self.assertEqual(getattr(raised.exception.orig, "sqlstate", None), "23001")
        self.assertTrue(await self.column_exists())

    async def test_down_is_refused_while_a_read_only_task_can_still_run(self):
        task_id = await self.read_only_task()
        await self.refused()
        # Cancelled or failed, it can be restarted or retried (Codex review of
        # #284): the code before 0279 would run it as an ordinary task.
        await self.service.execute(task_id, TaskCommand.CANCEL, actor=self.system)
        await self.refused()
        failed = await self.read_only_task()
        await self.service.execute(failed, TaskCommand.START, actor=self.system)
        await self.service.execute(failed, TaskCommand.FAIL, actor=self.system)
        await self.refused()

    async def test_down_and_up_once_every_read_only_task_completed(self):
        task_id = await self.read_only_task()
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        await self.service.execute(task_id, TaskCommand.COMPLETE, actor=self.system)
        await asyncio.to_thread(migrate, previous_revision(), downgrade=True)
        self.assertFalse(await self.column_exists())
        await asyncio.to_thread(migrate)
        self.assertTrue(await self.column_exists())
        # Every task of before is an ordinary one.
        snapshot = await self.service.restore(task_id, log_limit=0)
        self.assertFalse(snapshot.read_only)


if __name__ == "__main__":
    unittest.main()
