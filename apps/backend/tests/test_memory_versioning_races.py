"""Races of a memory write with another transaction that commits meanwhile (Codex).

Each test holds a transaction open on a connection of its own (the project's row
lock of ``ProjectService``, or the version row lock of the Immediate Journal),
starts the service call, waits until that call waits for the lock (or has already
finished, which is the bug), then commits the held transaction. The call must
decide on what was committed, not on the snapshot it read before it waited.
"""

import asyncio
import time
from uuid import UUID

from sqlalchemy import text

from paw_backend.memory.models import MemoryScope
from paw_backend.memory.versioning import (
    MemoryChanges,
    MemoryDraft,
    MemoryNotFoundError,
    MemoryPermissionError,
    MemoryVersionConflictError,
)

from .versioning_support import PostgresVersioningTestCase, requires_postgres

WAIT_SECONDS = 30.0


def draft(**overrides) -> MemoryDraft:
    values = {
        "scope": MemoryScope.USER,
        "memory_type": "preference",
        "title": "deploy day",
        "content": "deploy backend friday",
    }
    values.update(overrides)
    return MemoryDraft(**values)


@requires_postgres
class ConcurrentCommitTest(PostgresVersioningTestCase):
    def waiting_for_a_lock(self) -> bool:
        """Is any other backend of this database waiting for a row lock?"""
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database()"
                        " AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
                    )
                ).scalar_one()
            )

    async def run_while_held(self, hold: list[tuple[str, dict]], call) -> object:
        """Run ``call`` while a transaction that ran ``hold`` is open; commit the
        transaction once ``call`` waits for it (or is done), return the outcome."""
        with self.engine.connect() as holder:
            transaction = holder.begin()
            for sql, parameters in hold:
                holder.execute(text(sql), parameters)
            task = asyncio.ensure_future(call())
            deadline = time.monotonic() + WAIT_SECONDS
            while not task.done() and not self.waiting_for_a_lock():
                if time.monotonic() > deadline:
                    task.cancel()
                    self.fail("the call neither waited for the lock nor finished")
                await asyncio.sleep(0.02)
            transaction.commit()
        results = await asyncio.gather(task, return_exceptions=True)
        return results[0]

    @staticmethod
    def lock_project(project: UUID) -> tuple[str, dict]:
        # What ``ProjectService`` does first in every membership / lifecycle change.
        return ("SELECT id FROM projects WHERE id = :p FOR UPDATE", {"p": project})

    async def test_a_create_waits_for_the_archive_of_the_project(self):
        project = self.seed_project()
        team = self.seed_team(project)
        outcome = await self.run_while_held(
            [
                self.lock_project(project),
                (
                    "UPDATE projects SET status = 'archived' WHERE id = :p",
                    {"p": project},
                ),
            ],
            lambda: self.versioning.create_memory(
                self.actor(team.contributor),
                draft(scope=MemoryScope.PROJECT, project_id=project),
            ),
        )
        self.assertIsInstance(outcome, MemoryPermissionError)
        self.assertEqual(outcome.reason, "project_state_forbids")
        self.assertEqual(self.rows("SELECT id FROM memory_versions"), [])

    async def test_an_edit_waits_for_the_removal_of_the_member(self):
        project = self.seed_project()
        team = self.seed_team(project)
        seeded = self.seed(
            "team rule", "deploy backend friday", scope="project", project=project
        )
        outcome = await self.run_while_held(
            [
                self.lock_project(project),
                (
                    "DELETE FROM project_members"
                    " WHERE project_id = :p AND user_id = :u",
                    {"p": project, "u": team.contributor},
                ),
            ],
            lambda: self.versioning.edit_memory(
                self.actor(team.contributor),
                seeded.memory_id,
                1,
                MemoryChanges(content="deploy backend thursday"),
            ),
        )
        self.assertIsInstance(outcome, MemoryNotFoundError)
        self.assertEqual(len(self.versions(seeded.memory_id)), 1)

    async def test_an_edit_waits_for_the_demotion_of_the_member(self):
        project = self.seed_project()
        team = self.seed_team(project)
        seeded = self.seed(
            "team rule", "deploy backend friday", scope="project", project=project
        )
        outcome = await self.run_while_held(
            [
                self.lock_project(project),
                (
                    "UPDATE project_members SET role = 'viewer'"
                    " WHERE project_id = :p AND user_id = :u",
                    {"p": project, "u": team.contributor},
                ),
            ],
            lambda: self.versioning.edit_memory(
                self.actor(team.contributor),
                seeded.memory_id,
                1,
                MemoryChanges(content="deploy backend thursday"),
            ),
        )
        self.assertIsInstance(outcome, MemoryPermissionError)
        self.assertEqual(len(self.versions(seeded.memory_id)), 1)

    async def test_an_edit_sees_the_version_the_journal_committed_meanwhile(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        memory = {"m": created.memory_id}
        # What the Journal's consolidator does without the service's advisory lock:
        # retire version 1 (row lock) and insert version 2.
        outcome = await self.run_while_held(
            [
                ("SELECT set_config('paw.actor_type', 'system', true)", {}),
                (
                    "UPDATE memory_versions SET status = 'superseded'"
                    " WHERE memory_id = :m AND version_number = 1",
                    memory,
                ),
                (
                    "CREATE TEMP TABLE paw_next ON COMMIT DROP AS"
                    " SELECT * FROM memory_versions WHERE memory_id = :m",
                    memory,
                ),
                (
                    "UPDATE paw_next SET id = gen_random_uuid(), version_number = 2,"
                    " status = 'active'",
                    {},
                ),
                ("INSERT INTO memory_versions SELECT * FROM paw_next", {}),
            ],
            lambda: self.versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content="deploy thursday")
            ),
        )
        self.assertIsInstance(outcome, MemoryVersionConflictError)
        self.assertEqual((outcome.expected_version, outcome.current_version), (1, 2))
        self.assertEqual(len(self.versions(created.memory_id)), 2)
