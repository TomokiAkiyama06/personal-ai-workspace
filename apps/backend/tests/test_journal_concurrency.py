"""Concurrency on a real PostgreSQL: sequences, locks and racing consolidators.

Acceptance criterion "event sequenceで順序保証": event numbers are unique, gapless and
in commit order however many writers run at once, and racing consolidators never
apply one output twice or lose an update. Each writer has an engine of its own, as
a separate backend process would.
"""

import asyncio
import unittest
from uuid import uuid4

from sqlalchemy import text

from paw_backend.memory.journal import (
    ConversationNotFoundError,
    JournalBusyError,
    Priority,
    RunOutcome,
)
from paw_backend.memory.models import MessageRole

from .journal_support import (
    AsyncPostgresJournalTestCase,
    ScriptedWorker,
    memory,
    raise_unexpected,
    requires_postgres,
    worker_output,
)


@requires_postgres
class ConcurrentSequenceTest(AsyncPostgresJournalTestCase):
    async def test_thirty_concurrent_user_messages_get_thirty_distinct_numbers(self):
        conversation = self.seed_conversation()
        journals = [self.new_journal() for _ in range(6)]

        results = await asyncio.gather(
            *(
                journals[n % 6].record_user_message(
                    self.user, conversation, f"message {n}"
                )
                for n in range(30)
            ),
            return_exceptions=True,
        )
        raise_unexpected(results)

        self.assertEqual(sorted(r.event_sequence for r in results), list(range(30)))
        # The messages and the entries carry the same numbers, none twice.
        stored = self.rows(
            "SELECT event_sequence FROM messages WHERE conversation_id = :c"
            " ORDER BY event_sequence",
            c=conversation,
        )
        self.assertEqual([r["event_sequence"] for r in stored], list(range(30)))
        entries = self.rows(
            "SELECT event_sequence FROM memory_journal_entries"
            " WHERE conversation_id = :c ORDER BY event_sequence",
            c=conversation,
        )
        self.assertEqual([r["event_sequence"] for r in entries], list(range(30)))
        self.assertEqual(
            self.scalar("SELECT count(*) FROM memory_consolidation_queue"), 30
        )

    async def test_the_number_follows_the_commit_order_not_the_start_order(self):
        # The sequence and the time the row was recorded agree: a writer that gets
        # a number holds the conversation lock until it commits.
        conversation = self.seed_conversation()
        journals = [self.new_journal() for _ in range(4)]
        results = await asyncio.gather(
            *(
                journals[n % 4].record_user_message(self.user, conversation, f"m{n}")
                for n in range(20)
            ),
            return_exceptions=True,
        )
        raise_unexpected(results)
        rows = self.rows(
            "SELECT event_sequence, recorded_at FROM memory_journal_entries"
            " WHERE conversation_id = :c ORDER BY event_sequence",
            c=conversation,
        )
        times = [r["recorded_at"] for r in rows]
        self.assertEqual(times, sorted(times))

    async def test_user_messages_and_replies_share_one_gapless_sequence(self):
        conversation = self.seed_conversation()
        journals = [self.new_journal() for _ in range(4)]
        turn = uuid4()

        def action(n: int):
            journal = journals[n % 4]
            if n % 2:
                return journal.append_message(
                    self.user,
                    conversation,
                    MessageRole.ASSISTANT,
                    f"reply {n}",
                    turn_id=turn,
                )
            return journal.record_user_message(self.user, conversation, f"message {n}")

        results = await asyncio.gather(
            *(action(n) for n in range(24)), return_exceptions=True
        )
        raise_unexpected(results)

        self.assertEqual(sorted(r.event_sequence for r in results), list(range(24)))
        self.assertEqual(
            self.scalar(
                "SELECT count(DISTINCT event_sequence) FROM messages"
                " WHERE conversation_id = :c",
                c=conversation,
            ),
            24,
        )

    async def test_conversations_do_not_wait_for_each_other_and_number_separately(self):
        first, second = self.seed_conversation(), self.seed_conversation()
        journals = [self.new_journal() for _ in range(4)]
        results = await asyncio.gather(
            *(
                journals[n % 4].record_user_message(
                    self.user, first if n % 2 else second, f"m{n}"
                )
                for n in range(20)
            ),
            return_exceptions=True,
        )
        raise_unexpected(results)
        for conversation in (first, second):
            with self.subTest(conversation=conversation):
                numbers = self.rows(
                    "SELECT event_sequence FROM messages WHERE conversation_id = :c"
                    " ORDER BY event_sequence",
                    c=conversation,
                )
                self.assertEqual(
                    [r["event_sequence"] for r in numbers], list(range(10))
                )


@requires_postgres
class ConversationLockTest(AsyncPostgresJournalTestCase):
    async def test_a_writer_waiting_for_a_delete_gets_not_found_and_saves_nothing(self):
        conversation = self.seed_conversation()
        connection = self.engine.connect()
        deleter = connection.begin()
        self.addCleanup(connection.close)
        connection.execute(
            text("DELETE FROM conversations WHERE id = :c"), {"c": conversation}
        )
        write = self.spawn(
            self.journal.record_user_message(self.user, conversation, "too late")
        )
        await self.wait_until_a_backend_waits_for_a_lock()
        deleter.commit()

        with self.assertRaises(ConversationNotFoundError):
            await write

        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_journal_entries"), 0)

    async def test_a_lock_that_is_not_granted_in_time_is_busy_and_saves_nothing(self):
        conversation = self.seed_conversation()
        journal = self.new_journal(lock_timeout_ms=200)
        connection = self.engine.connect()
        holder = connection.begin()
        self.addCleanup(connection.close)
        connection.execute(
            text("SELECT id FROM conversations WHERE id = :c FOR UPDATE"),
            {"c": conversation},
        )

        with self.assertRaises(JournalBusyError):
            await journal.record_user_message(self.user, conversation, "blocked")
        holder.rollback()

        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 0)
        # Once the lock is free the same call works: nothing was left behind.
        receipt = await journal.record_user_message(self.user, conversation, "again")
        self.assertEqual(receipt.event_sequence, 0)


@requires_postgres
class RacingConsolidatorsTest(AsyncPostgresJournalTestCase):
    async def test_each_of_twelve_observations_is_applied_exactly_once(self):
        conversations = [self.seed_conversation() for _ in range(12)]
        for n, conversation in enumerate(conversations):
            await self.record(f"fact {n}", conversation=conversation)
        queue = self.new_queue()

        def worker():
            return ScriptedWorker(
                fallback=lambda text_: worker_output(
                    memory(text_.replace(" ", "_"), content=text_)
                )
            )

        consolidators = [
            self.new_consolidator(worker(), queue=self.new_queue(), worker_id=f"w-{n}")
            for n in range(6)
        ]

        async def drain(consolidator):
            done = []
            while (
                result := await consolidator.run_once()
            ).outcome is not RunOutcome.IDLE:
                done.append(result)
            return done

        results = await asyncio.gather(
            *(drain(c) for c in consolidators), return_exceptions=True
        )
        raise_unexpected(results)

        finished = [r for batch in results for r in batch]
        self.assertEqual([r.outcome for r in finished], [RunOutcome.COMPLETED] * 12)
        self.assertEqual(len({r.job_id for r in finished}), 12)
        self.assertEqual(
            sorted(self.active_versions()), sorted(f"fact_{n}" for n in range(12))
        )
        self.assertEqual(len(self.versions()), 12)
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM memory_consolidation_queue"
                " WHERE status <> 'completed'"
            ),
            0,
        )
        self.assertIsNotNone(queue)

    async def test_racing_updates_of_one_key_leave_one_active_version_and_a_chain(self):
        # Ten observations of ONE conversation all say something about one key;
        # HIGH / NORMAL mixes the order in which workers pick them up.
        conversation = self.seed_conversation()
        for n in range(10):
            await self.record(
                f"statement {n}",
                conversation=conversation,
                priority=Priority.HIGH if n % 3 == 0 else Priority.NORMAL,
            )

        def worker():
            return ScriptedWorker(
                fallback=lambda text_: worker_output(memory("style", content=text_))
            )

        consolidators = [
            self.new_consolidator(worker(), queue=self.new_queue(), worker_id=f"w-{n}")
            for n in range(5)
        ]

        async def drain(consolidator):
            done = []
            while (
                result := await consolidator.run_once()
            ).outcome is not RunOutcome.IDLE:
                done.append(result)
            return done

        results = await asyncio.gather(
            *(drain(c) for c in consolidators), return_exceptions=True
        )
        raise_unexpected(results)

        finished = [r for batch in results for r in batch]
        self.assertEqual({r.outcome for r in finished}, {RunOutcome.COMPLETED})
        self.assertEqual(len(finished), 10)
        versions = self.versions()
        # Exactly one active version, and the numbers form an unbroken chain.
        self.assertEqual([v["status"] for v in versions].count("active"), 1)
        self.assertEqual(
            [v["version_number"] for v in versions], list(range(1, len(versions) + 1))
        )
        # The winner is the NEWEST statement that was applied, by event sequence: no
        # older turn overwrote a newer one, whatever the finishing order was.
        (active,) = [v for v in versions if v["status"] == "active"]
        newest_applied = max(
            v["attributes"]["journal"]["event_sequence"] for v in versions
        )
        self.assertEqual(
            active["attributes"]["journal"]["event_sequence"], newest_applied
        )
        self.assertEqual(active["content"], f"statement {newest_applied}")
        # And no version was applied out of order: sequences rise along the chain.
        sequences = [v["attributes"]["journal"]["event_sequence"] for v in versions]
        self.assertEqual(sequences, sorted(sequences))
        # Whatever was not applied is recorded as stale, never silently dropped.
        stale = self.scalar(
            "SELECT count(*) FROM memory_journal_entries e,"
            " jsonb_array_elements(e.outcome -> 'items') AS i"
            " WHERE i ->> 'result' = 'stale'"
        )
        self.assertEqual(stale + len(versions), 10)

    async def test_a_supersession_and_an_older_observation_end_in_one_state(self):
        """Whichever finishes first, the retired key is not brought back.

        ``editor`` exists; a NEWER turn says ``ide`` replaces it and an OLDER turn
        restates ``editor``. The two run at the same time on two consolidators, in
        several rounds (the queue order alternates) so that both finishing orders
        occur. The result must always be: ``ide`` active, no active ``editor``.
        """
        for round_number in range(6):
            with self.subTest(round=round_number):
                self.clean_tables()
                conversation = self.seed_conversation()
                # Odd rounds put the OLDER observation first in the queue, so that
                # it starts (and can finish) before the newer supersession.
                older_first = round_number % 2 == 1
                await self.record(
                    "editor", conversation=conversation, priority=Priority.HIGH
                )
                await self.record(
                    "editor again",
                    conversation=conversation,
                    priority=Priority.HIGH if older_first else Priority.NORMAL,
                )
                await self.record(
                    "switch to ide",
                    conversation=conversation,
                    priority=Priority.NORMAL if older_first else Priority.HIGH,
                )
                await self.new_consolidator(
                    ScriptedWorker(
                        worker_output(memory("editor", content="Uses vim."))
                    ),
                    worker_id="setup",
                ).run_once()

                def answer(text_):
                    if text_ == "switch to ide":
                        return worker_output(
                            memory("ide", content="Uses VS Code.", supersedes="editor")
                        )
                    return worker_output(memory("editor", content="Uses vim again."))

                consolidators = [
                    self.new_consolidator(
                        ScriptedWorker(fallback=answer),
                        queue=self.new_queue(),
                        worker_id=f"w-{n}",
                    )
                    for n in range(2)
                ]
                results = await asyncio.gather(
                    *(c.run_once() for c in consolidators), return_exceptions=True
                )
                raise_unexpected(results)

                self.assertEqual({r.outcome for r in results}, {RunOutcome.COMPLETED})
                self.assertEqual(sorted(self.active_versions()), ["ide"])
                editor = [v for v in self.versions() if v["key"] == "editor"]
                self.assertNotIn("active", [v["status"] for v in editor])
                # The guard ended at the superseding turn (sequence 2) either way.
                self.assertEqual(
                    self.scalar(
                        "SELECT applied_event_sequence FROM memory_consolidation_keys"
                        " WHERE key = 'editor'"
                    ),
                    2,
                )

    async def test_two_consolidators_needing_the_same_keys_do_not_deadlock(self):
        first = self.seed_conversation()
        second = self.seed_conversation()
        await self.record("a", conversation=first)
        await self.record("b", conversation=second)
        # Both outputs touch keys x and y, mentioned in opposite orders.
        one = ScriptedWorker(worker_output(memory("x"), memory("y", supersedes="x")))
        two = ScriptedWorker(
            worker_output(memory("y"), memory("x", conflicts_with=["y"]))
        )
        results = await asyncio.wait_for(
            asyncio.gather(
                self.new_consolidator(one, worker_id="w-1").run_once(),
                self.new_consolidator(two, worker_id="w-2").run_once(),
                return_exceptions=True,
            ),
            30,
        )
        raise_unexpected(results)
        self.assertEqual({r.outcome for r in results}, {RunOutcome.COMPLETED})


if __name__ == "__main__":
    unittest.main()
