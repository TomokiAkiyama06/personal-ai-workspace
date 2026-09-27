"""Revision 0085: the Working Set tables, and the move of the per-attempt state.

The first class needs no server (the rendered SQL and the Python enums the CHECK
lists must match). The PostgreSQL class (skipped unless ``PAW_TEST_DATABASE_URL`` is
set) runs the revision down and up again over data. Nothing here assumes 0085 is
the head: the previous revision is read from the script directory. That the models
and the migrated schema agree is ``test_task_persistence`` (autogenerate and the
names of every constraint of the ``task*`` tables).
"""

import asyncio
import io
import re
import unittest
import uuid

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text

from paw_backend.tasks import (
    ActorKind,
    EvaluationResult,
    PullRequestInfo,
    PullRequestState,
    RepoRole,
    ReviewState,
    ReviewStatus,
    TaskCommand,
    WorkingSetEntry,
    WorkingSetOperation,
    WorktreeState,
)

from .support import paw_environment
from .task_support import FIRST_RUN, PostgresTaskTestCase, migrate, requires_postgres
from .test_migrations import offline_config

REVISION = "0085"


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def listed(members) -> str:
    return ", ".join(f"'{member.value}'" for member in members)


class OfflineMigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://paw:pw@db.internal/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def upgrade_sql(self, **environment: str) -> str:
        return self.sql("upgrade", f"{previous_revision()}:{REVISION}", **environment)

    def test_the_lists_of_the_checks_are_the_python_enums(self):
        sql = self.upgrade_sql()
        self.assertIn(f"role IN ({listed(RepoRole)})", sql)
        self.assertIn(f"strongest_role IN ({listed(RepoRole)})", sql)
        self.assertIn(f"added_by_kind IN ({listed(ActorKind)})", sql)
        self.assertIn(f"review_status IN ({listed(ReviewStatus)})", sql)
        self.assertIn(f"evaluation_result IN ({listed(EvaluationResult)})", sql)
        self.assertIn(f"pr_state IN ({listed(PullRequestState)})", sql)
        (commands,) = re.findall(
            r"ck_task_events_command_valid CHECK \(command IN \(([^)]*)\)\)", sql
        )
        self.assertEqual(
            {value.strip().strip("'") for value in commands.split(",")},
            {command.value for command in TaskCommand},
        )

    def test_the_state_moves_and_the_task_loses_its_single_commit(self):
        sql = self.upgrade_sql()
        self.assertIn("CREATE TABLE task_repositories (", sql)
        self.assertIn("CREATE TABLE task_attempt_repositories (", sql)
        for column in (
            "branch",
            "worktree_path",
            "head_commit",
            "review_status",
            "evaluation_result",
            "pr_number",
            "pr_url",
            "pr_state",
        ):
            self.assertIn(f"ALTER TABLE task_attempts DROP COLUMN {column}", sql)
        self.assertIn("ALTER TABLE tasks DROP COLUMN starting_commit", sql)
        # No foreign key to ``repositories``: a task's history outlives a purge.
        self.assertNotIn("REFERENCES repositories", sql)

    def test_the_grants_are_the_least_privileges_of_the_service(self):
        sql = self.upgrade_sql(PAW_APP_DATABASE_ROLE="paw_app")
        for table in ("task_repositories", "task_attempt_repositories"):
            self.assertIn(f'GRANT INSERT, SELECT ON {table} TO "paw_app"', sql)
            self.assertNotIn("DELETE", sql)
        self.assertIn(
            "GRANT UPDATE (role, starting_commit, added_by_kind, added_by, added_at, "
            'updated_at, removed_at) ON task_repositories TO "paw_app"',
            sql,
        )
        self.assertIn(
            "GRANT UPDATE (branch, worktree_path, head_commit, review_status, "
            "evaluation_result, pr_number, pr_url, pr_state, strongest_role, "
            'modified, updated_at) ON task_attempt_repositories TO "paw_app"',
            sql,
        )
        self.assertIn('REVOKE UPDATE (updated_at) ON task_attempts FROM "paw_app"', sql)

    def test_without_an_application_role_nothing_is_granted(self):
        sql = self.upgrade_sql()
        self.assertNotIn("paw_app", sql)
        self.assertNotIn("REVOKE UPDATE (updated_at)", sql)

    def test_the_revision_follows_the_head_it_was_written_on(self):
        self.assertEqual(previous_revision(), "0041")


@requires_postgres
class RoundTripTest(PostgresTaskTestCase):
    """Down and up again over tasks that exist: nothing refuses, the history
    stays, and a Single-Repo task's state goes back to its attempt."""

    async def asyncTearDown(self):
        await asyncio.to_thread(migrate)
        await super().asyncTearDown()

    async def columns(self, table: str) -> set[str]:
        async with self.database.engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"
                ),
                {"t": table},
            )
            return {row[0] for row in rows}

    async def test_down_keeps_the_history_and_copies_a_single_repositorys_state(self):
        multi = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, RepoRole.TARGET, "a" * 40)
            ]
        )
        await self.service.update_attempt(
            multi,
            run=FIRST_RUN,
            repository_id=self.repository_id,
            worktree=WorktreeState("agent/x", "/srv/w/x", "b" * 40),
            review=ReviewState(ReviewStatus.APPROVED, EvaluationResult.PASSED),
            pull_request=PullRequestInfo(
                9, "https://example.test/pr/9", PullRequestState.OPEN
            ),
        )
        await self.service.change_working_set(
            multi,
            WorkingSetOperation.ADD_REFERENCED,
            uuid.uuid4(),
            actor=self.user,
            expected_role=None,
        )
        # ``multi`` has two repositories in its attempt now: nothing is copied for
        # it. ``single`` has one (and one starting commit).
        single = await self.create_task()

        await asyncio.to_thread(migrate, previous_revision(), downgrade=True)
        self.assertNotIn("task_repositories", await self.tables())
        self.assertIn("starting_commit", await self.columns("tasks"))
        self.assertIn("pr_url", await self.columns("task_attempts"))
        self.assertEqual(
            await self.scalar(
                "SELECT count(*) FROM task_events "
                "WHERE task_id = :t AND command = 'change_working_set'",
                t=multi,
            ),
            1,
        )
        # The old list does not hold for the kept history: not validated.
        self.assertFalse(
            await self.scalar(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ck_task_events_command_valid'"
            )
        )
        self.assertEqual(
            await self.scalar(
                "SELECT review_status FROM task_attempts WHERE task_id = :t", t=single
            ),
            "not_started",
        )
        self.assertEqual(
            await self.scalar(
                "SELECT starting_commit FROM tasks WHERE id = :t", t=single
            ),
            "0" * 40,
        )
        self.assertIsNone(
            await self.scalar(
                "SELECT pr_url FROM task_attempts WHERE task_id = :t", t=multi
            )
        )

        await asyncio.to_thread(migrate)
        self.assertIn("task_repositories", await self.tables())
        self.assertTrue(
            await self.scalar(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ck_task_events_command_valid'"
            )
        )

    async def test_down_copies_the_state_of_the_only_repository(self):
        task_id = await self.create_task()
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.repository_id,
            worktree=WorktreeState("agent/y", "/srv/w/y", "c" * 40),
            pull_request=PullRequestInfo(
                3, "https://example.test/pr/3", PullRequestState.MERGED
            ),
        )
        await asyncio.to_thread(migrate, previous_revision(), downgrade=True)
        async with self.database.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT branch, head_commit, pr_number, pr_state, "
                        "review_status FROM task_attempts WHERE task_id = :t"
                    ),
                    {"t": task_id},
                )
            ).one()
        self.assertEqual(tuple(row), ("agent/y", "c" * 40, 3, "merged", "not_started"))

    async def tables(self) -> set[str]:
        async with self.database.engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename LIKE 'task%'"
                )
            )
            return {row[0] for row in rows}
