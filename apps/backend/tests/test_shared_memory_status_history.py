"""Shared Memory: delete, restore and edit leave a status history (issue #90).

The service changes ``memory_versions.status`` in place (an edit supersedes the
current version, a delete deprecates it, a restore activates it again). Since
revision 0071 the database records each change in ``memory_metadata_changes``
and refuses one that names no actor, so the service names the manager it has
authorised as the actor. Real PostgreSQL; skipped unless ``PAW_TEST_DATABASE_URL``
is set. ``tests/test_shared_memory_grants.py`` runs the class again as the
unprivileged application role.
"""

import asyncio
from uuid import UUID

from paw_backend.memory.shared import (
    SharedMemory,
    SharedMemoryChanges,
    SharedMemoryPermissionError,
    SharedMemoryStateError,
)

from .shared_memory_support import (
    AsyncPostgresSharedTestCase,
    draft,
    raise_unexpected,
    requires_postgres,
)


@requires_postgres
class SharedStatusHistoryTest(AsyncPostgresSharedTestCase):
    def history(self, memory_id: UUID) -> list[tuple]:
        """(version number, old status, new status, actor type, actor id), in order."""
        rows = self.rows(
            "SELECT v.version_number, c.old_status, c.new_status,"
            " c.actor_type, c.actor_user_id"
            " FROM memory_metadata_changes c"
            " JOIN memory_versions v ON v.id = c.memory_version_id"
            " WHERE v.memory_id = :m ORDER BY c.created_at",
            m=memory_id,
        )
        return [
            (
                row["version_number"],
                row["old_status"],
                row["new_status"],
                row["actor_type"],
                row["actor_user_id"],
            )
            for row in rows
        ]

    async def test_a_delete_records_who_deprecated_the_version(self):
        memory_id = self.seed_memory(title="Rule")

        await self.service.delete_memory(self.owner, memory_id)

        self.assertEqual(
            self.history(memory_id),
            [(1, "active", "deprecated", "user", self.owner.user_id)],
        )

    async def test_a_restore_records_who_activated_the_version_again(self):
        memory_id = self.seed_memory(title="Rule", status="deprecated")

        await self.service.restore_memory(self.admin, memory_id)

        self.assertEqual(
            self.history(memory_id),
            [(1, "deprecated", "active", "user", self.admin.user_id)],
        )

    async def test_an_edit_records_who_superseded_the_old_version_only(self):
        memory_id = self.seed_memory(title="Old")

        await self.service.edit_memory(
            self.admin, memory_id, 1, SharedMemoryChanges(title="New")
        )

        # The new version is inserted (its own actor columns say who wrote it):
        # only the old one changed in place.
        self.assertEqual(
            self.history(memory_id),
            [(1, "active", "superseded", "user", self.admin.user_id)],
        )

    async def test_the_whole_life_of_a_memory_is_told_by_its_history(self):
        created = await self.service.create_memory(self.owner, draft(title="v1"))
        await self.service.edit_memory(
            self.admin, created.memory_id, 1, SharedMemoryChanges(title="v2")
        )
        await self.service.delete_memory(self.admin, created.memory_id)
        await self.service.restore_memory(self.owner, created.memory_id)

        self.assertEqual(
            self.history(created.memory_id),
            [
                (1, "active", "superseded", "user", self.admin.user_id),
                (2, "active", "deprecated", "user", self.admin.user_id),
                (2, "deprecated", "active", "user", self.owner.user_id),
            ],
        )

    async def test_a_refused_or_failed_change_records_nothing(self):
        memory_id = self.seed_memory(title="Rule")
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.delete_memory(self.user, memory_id)
        self.assertEqual(self.history(memory_id), [])

        await self.service.delete_memory(self.owner, memory_id)
        with self.assertRaises(SharedMemoryStateError):
            await self.service.delete_memory(self.admin, memory_id)

        self.assertEqual(
            self.history(memory_id),
            [(1, "active", "deprecated", "user", self.owner.user_id)],
        )

    async def test_two_deletes_at_once_are_recorded_exactly_once(self):
        memory_id = self.seed_memory()
        other = self.new_service()

        results = await asyncio.gather(
            self.service.delete_memory(self.owner, memory_id),
            other.delete_memory(self.admin, memory_id),
            return_exceptions=True,
        )

        raise_unexpected(results, SharedMemory, SharedMemoryStateError)
        (row,) = self.history(memory_id)
        self.assertEqual(row[:4], (1, "active", "deprecated", "user"))
        self.assertIn(row[4], {self.owner.user_id, self.admin.user_id})
