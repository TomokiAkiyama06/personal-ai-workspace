"""A consolidation that retires a memory names its actor (revision 0071).

From revision 0071 on, every change of ``memory_versions.status`` is recorded by a
trigger in ``memory_metadata_changes`` with its actor, and REFUSED when no actor is
named (``paw_backend.memory.metadata.metadata_change_actor``). The consolidator
retires a version in exactly one place (``applier._supersede``) and names the
``system`` there: the background Memory Worker does it, not the owner of the
conversation. These tests read the history rows the database wrote.

The columns of the history rows (``old_status``, ``new_status``) come from revision
0071 (PR #109), which revision 0041 follows in the migration chain, so they always
exist when these tests run.
"""

import unittest

from paw_backend.memory.journal import ItemResult, Priority, RunOutcome

from .journal_support import (
    AsyncPostgresJournalTestCase,
    ScriptedWorker,
    memory,
    requires_postgres,
    worker_output,
)


@requires_postgres
class StatusHistoryTest(AsyncPostgresJournalTestCase):
    async def consolidate(self, worker, count: int = 1):
        consolidator = self.new_consolidator(worker)
        return [await consolidator.run_once() for _ in range(count)]

    def history(self) -> list[dict]:
        """Every history row, oldest first, with the version it is about."""
        return self.rows(
            "SELECT h.*, v.title AS key, v.version_number, v.status AS status_now"
            " FROM memory_metadata_changes h"
            " JOIN memory_versions v ON v.id = h.memory_version_id"
            " ORDER BY h.created_at, h.id"
        )

    def assert_retired_by_the_system(self, row: dict, key: str, version_number: int):
        self.assertEqual(
            (
                row["key"],
                row["version_number"],
                row["old_status"],
                row["new_status"],
                row["actor_type"],
                row["actor_user_id"],
            ),
            (key, version_number, "active", "superseded", "system", None),
        )
        # Nothing else changed in the same UPDATE.
        self.assertEqual((row["old_pinned"], row["new_pinned"]), (False, False))
        self.assertEqual((row["old_importance"], row["new_importance"]), (50, 50))
        self.assertEqual((row["old_stale_since"], row["new_stale_since"]), (None, None))
        self.assertEqual(row["status_now"], "superseded")

    async def test_a_newer_version_that_retires_the_previous_one_writes_one_row(self):
        conversation = self.seed_conversation()
        await self.record("Use tabs.", conversation=conversation)
        await self.record("Actually spaces.", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use tabs.")),
            worker_output(memory("indent_style", content="Use spaces.")),
        )

        first, second = await self.consolidate(worker, count=2)

        self.assertEqual(
            (first.items, second.items), ((ItemResult.CREATED,), (ItemResult.UPDATED,))
        )
        # Exactly one row: the retired version 1. Writing version 2 (an INSERT) and
        # creating version 1 (an INSERT) record nothing.
        (row,) = self.history()
        self.assert_retired_by_the_system(row, "indent_style", 1)
        (version_two,) = self.rows(
            "SELECT status FROM memory_versions WHERE version_number = 2"
        )
        self.assertEqual(version_two["status"], "active")

    async def test_retiring_another_keys_memory_writes_one_row_for_that_version(self):
        conversation = self.seed_conversation()
        await self.record("editor", conversation=conversation)
        await self.record("switch", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("editor", content="Uses vim.")),
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
        )

        await self.consolidate(worker, count=2)

        (row,) = self.history()
        self.assert_retired_by_the_system(row, "editor", 1)

    async def test_an_item_that_retires_two_versions_writes_two_rows(self):
        conversation = self.seed_conversation()
        for text_ in ("editor", "ide", "both"):
            await self.record(text_, conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("editor", content="Uses vim.")),
            worker_output(memory("ide", content="Uses Emacs.")),
            # Updates ``ide`` (retiring its version 1) AND retires ``editor``.
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
        )

        results = await self.consolidate(worker, count=3)

        self.assertEqual(results[2].items, (ItemResult.UPDATED,))
        rows = self.history()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted((r["key"], r["version_number"]) for r in rows),
            [("editor", 1), ("ide", 1)],
        )
        for row in rows:
            self.assert_retired_by_the_system(row, row["key"], 1)
        self.assertEqual(len({r["memory_version_id"] for r in rows}), 2)

    async def test_nothing_retired_nothing_recorded(self):
        conversation = self.seed_conversation()
        await self.record("first", conversation=conversation)
        await self.record("again", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use tabs.")),
            worker_output(memory("indent_style", content="Use tabs.")),  # duplicate
        )

        first, second = await self.consolidate(worker, count=2)

        self.assertEqual(
            (first.items, second.items),
            ((ItemResult.CREATED,), (ItemResult.DUPLICATE,)),
        )
        self.assertEqual(self.history(), [])

    async def test_a_held_or_stale_candidate_records_nothing(self):
        self.seed_key_memory("indent_style", "Use spaces.", "confirmed")
        await self.record("Use tabs now.")
        (result,) = await self.consolidate(
            ScriptedWorker(worker_output(memory("indent_style", content="Use tabs.")))
        )
        self.assertEqual(result.items, (ItemResult.HELD_CONFIRMED,))
        self.assertEqual(self.history(), [])

    async def test_a_rolled_back_consolidation_leaves_no_history(self):
        """The row is written in the transaction of the retirement.

        The first item retires a version; the second breaks (the registry names a
        memory that is somebody else's). The whole application rolls back, and the
        history row of the retirement goes with it: nothing was retired, so there is
        nothing to explain.
        """
        self.seed_key_memory("first", "old", "inferred")
        foreign = self.seed_key_memory(
            "theirs",
            "not yours",
            "inferred",
            owner=self.other_user.user_id,
            register=False,
        )
        self.execute(
            "INSERT INTO memory_consolidation_keys (owner_user_id, key, memory_id,"
            " applied_conversation_id, applied_event_sequence, applied_recorded_at)"
            " VALUES (:o, 'second', :m, gen_random_uuid(), 0,"
            " '2000-01-01T00:00:00+00:00')",
            o=self.user.user_id,
            m=foreign,
        )
        await self.record("two facts", priority=Priority.NORMAL)

        (result,) = await self.consolidate(
            ScriptedWorker(
                worker_output(
                    memory("first", content="new"), memory("second", content="Mine.")
                )
            )
        )

        self.assertEqual(result.outcome, RunOutcome.RETRY_SCHEDULED)
        self.assertEqual(self.history(), [])
        (first,) = self.versions()
        self.assertEqual((first["content"], first["status"]), ("old", "active"))

    async def test_the_consolidation_works_whether_or_not_the_history_exists(self):
        # Naming the actor is what lets the UPDATE through the trigger of revision
        # 0071: the version is retired and the job completes.
        conversation = self.seed_conversation()
        await self.record("one", conversation=conversation)
        await self.record("two", conversation=conversation)
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use tabs.")),
            worker_output(memory("indent_style", content="Use spaces.")),
        )
        first, second = await self.consolidate(worker, count=2)
        self.assertEqual(
            (first.outcome, second.outcome),
            (RunOutcome.COMPLETED, RunOutcome.COMPLETED),
        )
        self.assertEqual(
            [(v["version_number"], v["status"]) for v in self.versions()],
            [(1, "superseded"), (2, "active")],
        )


if __name__ == "__main__":
    unittest.main()
