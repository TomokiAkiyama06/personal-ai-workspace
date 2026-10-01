"""Revision 0129: ``task_events`` accepts ``release_repository_write`` (issue #129).

The first class needs no server (the rendered SQL and the Python enum must match).
The PostgreSQL class (skipped unless ``PAW_TEST_DATABASE_URL`` is set) runs the
revision down and up again, and refuses the downgrade over a recorded release
(the code before 0129 cannot read that event: issue #90, by the human's decision
of 2026-09-30). Nothing here assumes 0129 is the head: the previous revision is
read from the script directory.
"""

import asyncio
import io
import re
import unittest

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.exc import DBAPIError

from paw_backend.tasks import TaskCommand

from .support import paw_environment
from .task_support import migrate, requires_postgres
from .test_migrations import offline_config
from .test_task_write_release import ReleaseTestCase

REVISION = "0129"


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def command_lists(sql: str) -> list[set[str]]:
    return [
        {value.strip().strip("'") for value in listed.split(",")}
        for listed in re.findall(
            r"ck_task_events_command_valid CHECK \(command IN \(([^)]*)\)\)", sql
        )
    ]


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str) -> str:
        output = io.StringIO()
        with paw_environment(PAW_DATABASE_URL="postgresql://paw:pw@db.internal/paw"):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_list_is_the_python_enum(self):
        sql = self.sql("upgrade", f"{previous_revision()}:{REVISION}")
        self.assertEqual(command_lists(sql), [{c.value for c in TaskCommand}])
        self.assertNotIn("GRANT", sql)

    def test_the_downgrade_goes_back_to_the_list_before(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertEqual(
            command_lists(sql),
            [{c.value for c in TaskCommand} - {"release_repository_write"}],
        )
        # Refused (not a NOT VALID list) when a release is recorded.
        self.assertNotIn("NOT VALID", sql)
        self.assertIn("RAISE EXCEPTION", sql)
        self.assertLess(sql.index("RAISE EXCEPTION"), sql.index("DROP CONSTRAINT"))

    def test_the_revision_follows_the_head_it_was_written_on(self):
        self.assertEqual(previous_revision(), "0133")


@requires_postgres
class RoundTripTest(ReleaseTestCase):
    """Down and up again; never down over a release (the append-only history
    would keep an event the code before 0129 cannot read)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Each test starts without tasks: a release recorded by another test
        # (append-only, so only TRUNCATE removes it) would refuse every downgrade
        # and hide the path without one.
        await self.owner_sql("TRUNCATE tasks CASCADE")

    async def asyncTearDown(self):
        await asyncio.to_thread(migrate)
        await super().asyncTearDown()

    async def validated(self) -> bool:
        return await self.scalar(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = 'ck_task_events_command_valid'"
        )

    async def test_down_is_refused_while_a_release_is_recorded(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        await self.release(task_id, reservation)

        with self.assertRaises(DBAPIError) as raised:
            await asyncio.to_thread(migrate, previous_revision(), downgrade=True)

        restrict_violation = "23001"
        self.assertEqual(
            getattr(raised.exception.orig, "sqlstate", None), restrict_violation
        )
        self.assertIn("release_repository_write", str(raised.exception))
        # Nothing was changed: the release and the list that accepts it stay.
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM task_events "
                "WHERE task_id = :t AND command = 'release_repository_write'",
                t=task_id,
            ),
            1,
        )
        self.assertTrue(await self.validated())
        self.assertIn(
            "release_repository_write",
            await self.scalar(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_task_events_command_valid'"
            ),
        )

    async def test_down_without_a_release_validates_the_old_list(self):
        await self.two_targets()
        released = await self.scalar(
            "SELECT count(*) FROM task_events "
            "WHERE command = 'release_repository_write'"
        )
        # The path without a release is the one this test is about.
        self.assertEqual(released, 0)
        await asyncio.to_thread(migrate, previous_revision(), downgrade=True)
        self.assertTrue(await self.validated())
        await asyncio.to_thread(migrate)
        self.assertTrue(await self.validated())
