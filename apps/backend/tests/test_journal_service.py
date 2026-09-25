"""``MemoryJournal``: the raw message and the Pending Observation are saved at once.

Real PostgreSQL (skipped unless ``PAW_TEST_DATABASE_URL`` is set). Acceptance
criterion "User message時にRaw + Pending Observation即保存" and the event sequence.
"""

import unittest
from unittest import mock
from uuid import uuid4

from paw_backend.authz import (
    ALL_PROJECTS,
    AgentGrant,
    Capability,
    Principal,
    SystemRole,
)
from paw_backend.memory.journal import (
    ConversationNotFoundError,
    InvalidJournalInputError,
    JournalPermissionError,
    Priority,
)
from paw_backend.memory.models import MessageRole
from paw_backend.memory.shared import AgentActor

from .journal_support import (
    OTHER_USER_ID,
    USER_ID,
    AsyncPostgresJournalTestCase,
    requires_postgres,
)


@requires_postgres
class RecordUserMessageTest(AsyncPostgresJournalTestCase):
    async def test_the_raw_message_the_observation_and_the_job_are_saved_together(self):
        project, repo = uuid4(), uuid4()
        conversation = self.seed_conversation(project_id=project, repo_id=repo)
        turn = uuid4()

        receipt = await self.journal.record_user_message(
            self.user, conversation, "Please use tabs.", turn_id=turn
        )

        (message,) = self.rows(
            "SELECT * FROM messages WHERE conversation_id = :c", c=conversation
        )
        self.assertEqual(
            (message["role"], message["content"], message["event_sequence"]),
            ("user", "Please use tabs.", 0),
        )
        self.assertEqual(message["turn_id"], turn)
        entry = self.entry_row(receipt.entry_id)
        self.assertEqual(
            (
                entry["conversation_id"],
                entry["message_id"],
                entry["turn_id"],
                entry["event_sequence"],
                entry["owner_user_id"],
                entry["project_id"],
                entry["repo_id"],
                entry["state"],
                entry["consolidated_at"],
                entry["outcome"],
            ),
            (
                conversation,
                message["id"],
                turn,
                0,
                USER_ID,
                project,
                repo,
                "pending",
                None,
                None,
            ),
        )
        self.assertIsNotNone(entry["recorded_at"])
        (job,) = self.jobs_of(receipt.entry_id)
        self.assertEqual(
            (job["status"], job["priority"], job["priority_rank"]),
            ("queued", "normal", 1),
        )
        self.assertEqual(
            (job["attempts"], job["deferrals"], job["claim_count"]), (0, 0, 0)
        )
        self.assertEqual(
            (receipt.message_id, receipt.conversation_id, receipt.turn_id),
            (message["id"], conversation, turn),
        )
        self.assertEqual(
            (receipt.event_sequence, receipt.priority), (0, Priority.NORMAL)
        )

    async def test_no_worker_and_no_model_are_involved(self):
        # The journal has no reference to a worker: saving works with none running.
        receipt = await self.record("Remember: I use uv.")
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")

    async def test_a_failure_after_the_message_rolls_everything_back(self):
        conversation = self.seed_conversation()
        with mock.patch(
            "paw_backend.memory.journal.service.insert_job",
            side_effect=RuntimeError("queue write failed"),
        ):
            with self.assertRaises(RuntimeError):
                await self.journal.record_user_message(self.user, conversation, "Hi")
        for table in (
            "messages",
            "memory_journal_entries",
            "memory_consolidation_queue",
        ):
            with self.subTest(table):
                self.assertEqual(self.scalar(f"SELECT count(*) FROM {table}"), 0)

    async def test_the_turn_id_is_new_by_default_and_can_be_shared_with_the_reply(self):
        conversation = self.seed_conversation()
        first = await self.record("one", conversation=conversation)
        second = await self.record("two", conversation=conversation)
        self.assertNotEqual(first.turn_id, second.turn_id)

        reply = await self.journal.append_message(
            self.user, conversation, MessageRole.ASSISTANT, "ok", turn_id=first.turn_id
        )

        self.assertEqual(reply.turn_id, first.turn_id)
        turns = self.rows(
            "SELECT role, turn_id FROM messages WHERE conversation_id = :c"
            " ORDER BY event_sequence",
            c=conversation,
        )
        self.assertEqual(
            [(r["role"], r["turn_id"]) for r in turns],
            [
                ("user", first.turn_id),
                ("user", second.turn_id),
                ("assistant", first.turn_id),
            ],
        )

    async def test_priority_is_the_callers_and_sets_the_rank(self):
        conversation = self.seed_conversation()
        ranks = {}
        for priority in (Priority.HIGH, Priority.NORMAL, Priority.LOW, "high"):
            receipt = await self.record(
                f"message {priority}", conversation=conversation, priority=priority
            )
            (job,) = self.jobs_of(receipt.entry_id)
            ranks[str(priority)] = (
                job["priority"],
                job["priority_rank"],
                receipt.priority,
            )
        self.assertEqual(
            ranks,
            {
                "high": ("high", 0, Priority.HIGH),
                "normal": ("normal", 1, Priority.NORMAL),
                "low": ("low", 2, Priority.LOW),
            },
        )

    async def test_the_priority_is_not_decided_from_the_text(self):
        # "Always" / "from now on" do not change the class: the caller decides.
        receipt = await self.record("From now on ALWAYS use tabs, this is a decision.")
        self.assertEqual(self.jobs_of(receipt.entry_id)[0]["priority"], "normal")


@requires_postgres
class EventSequenceTest(AsyncPostgresJournalTestCase):
    async def test_every_role_shares_one_sequence_starting_at_zero(self):
        conversation = self.seed_conversation()
        turn = uuid4()
        sequence = [
            (
                await self.record("a", conversation=conversation, turn_id=turn)
            ).event_sequence,
            (
                await self.journal.append_message(
                    self.user, conversation, MessageRole.ASSISTANT, "b", turn_id=turn
                )
            ).event_sequence,
            (
                await self.journal.append_message(
                    self.user, conversation, MessageRole.TOOL, "c", turn_id=turn
                )
            ).event_sequence,
            (await self.record("d", conversation=conversation)).event_sequence,
        ]
        self.assertEqual(sequence, [0, 1, 2, 3])
        stored = self.rows(
            "SELECT event_sequence, role FROM messages WHERE conversation_id = :c"
            " ORDER BY event_sequence",
            c=conversation,
        )
        self.assertEqual(
            [(r["event_sequence"], r["role"]) for r in stored],
            [(0, "user"), (1, "assistant"), (2, "tool"), (3, "user")],
        )

    async def test_the_entry_and_its_message_carry_the_same_number(self):
        conversation = self.seed_conversation()
        for text in ("a", "b", "c"):
            await self.record(text, conversation=conversation)
        rows = self.rows(
            "SELECT e.event_sequence AS entry_sequence,"
            " m.event_sequence AS message_sequence"
            " FROM memory_journal_entries e JOIN messages m ON m.id = e.message_id"
            " ORDER BY m.event_sequence"
        )
        self.assertEqual(
            [(r["entry_sequence"], r["message_sequence"]) for r in rows],
            [(0, 0), (1, 1), (2, 2)],
        )

    async def test_each_conversation_has_its_own_sequence(self):
        first, second = self.seed_conversation(), self.seed_conversation()
        a = await self.record("a", conversation=first)
        b = await self.record("b", conversation=second)
        c = await self.record("c", conversation=first)
        self.assertEqual(
            [a.event_sequence, b.event_sequence, c.event_sequence], [0, 0, 1]
        )


@requires_postgres
class OwnershipTest(AsyncPostgresJournalTestCase):
    async def test_another_users_conversation_is_not_found_and_nothing_is_saved(self):
        theirs = self.seed_conversation(OTHER_USER_ID)
        with self.assertRaises(ConversationNotFoundError):
            await self.journal.record_user_message(self.user, theirs, "let me in")
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_journal_entries"), 0)

    async def test_an_admin_cannot_write_into_a_users_conversation(self):
        conversation = self.seed_conversation()
        with self.assertRaises(ConversationNotFoundError):
            await self.journal.record_user_message(self.admin, conversation, "hi")
        with self.assertRaises(ConversationNotFoundError):
            await self.journal.append_message(
                self.admin, conversation, MessageRole.ASSISTANT, "hi", turn_id=uuid4()
            )

    async def test_an_unknown_conversation_looks_the_same_as_someone_elses(self):
        with self.assertRaises(ConversationNotFoundError) as unknown:
            await self.journal.record_user_message(self.user, uuid4(), "hi")
        with self.assertRaises(ConversationNotFoundError) as theirs:
            await self.journal.record_user_message(
                self.user, self.seed_conversation(OTHER_USER_ID), "hi"
            )
        self.assertEqual(str(unknown.exception), str(theirs.exception))


@requires_postgres
class AuthorizationTest(AsyncPostgresJournalTestCase):
    async def test_memory_use_is_asked_of_the_authorizer_and_audited_without_text(self):
        receipt = await self.record("a very private sentence")
        (event,) = self.sink.events
        self.assertEqual(event.action, Capability.MEMORY_USE.value)
        self.assertEqual(event.actor_id, USER_ID)
        self.assertNotIn("private sentence", repr(event))
        self.assertIsNotNone(receipt)

    async def test_a_role_without_memory_use_is_denied_before_the_database(self):
        # The backend's own identity (a background worker) holds no capability.
        stranger = Principal(uuid4(), SystemRole.SYSTEM)
        conversation = self.seed_conversation()
        with self.assertRaises(JournalPermissionError):
            await self.journal.record_user_message(stranger, conversation, "hi")
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 0)

    async def test_an_agent_cannot_speak_for_the_user(self):
        agent = AgentActor(
            USER_ID,
            AgentGrant(uuid4(), frozenset({Capability.MEMORY_USE}), ALL_PROJECTS),
        )
        with self.assertRaises(InvalidJournalInputError) as raised:
            await self.journal.record_user_message(
                agent, self.seed_conversation(), "hi"
            )
        self.assertEqual(raised.exception.field, "actor")

    async def test_an_agent_with_a_memory_use_grant_can_append_its_own_events(self):
        conversation = self.seed_conversation()
        agent = AgentActor(
            USER_ID,
            AgentGrant(uuid4(), frozenset({Capability.MEMORY_USE}), ALL_PROJECTS),
        )
        appended = await self.journal.append_message(
            agent, conversation, MessageRole.AGENT, "done", turn_id=uuid4()
        )
        self.assertEqual(appended.event_sequence, 0)

    async def test_an_agent_without_the_grant_is_denied(self):
        conversation = self.seed_conversation()
        agent = AgentActor(
            USER_ID, AgentGrant(uuid4(), frozenset({Capability.CHAT_USE}), ALL_PROJECTS)
        )
        with self.assertRaises(JournalPermissionError):
            await self.journal.append_message(
                agent, conversation, MessageRole.AGENT, "done", turn_id=uuid4()
            )
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 0)

    async def test_an_agent_with_the_grant_reads_the_pending_observations(self):
        conversation = self.seed_conversation()
        await self.record("for the agent", conversation=conversation)
        agent = AgentActor(
            USER_ID,
            AgentGrant(uuid4(), frozenset({Capability.MEMORY_USE}), ALL_PROJECTS),
        )
        pending = await self.journal.pending_observations(agent, conversation)
        status = await self.journal.sync_status(agent, conversation)
        self.assertEqual([p.content for p in pending], ["for the agent"])
        self.assertEqual(status.pending, 1)

    async def test_an_agent_without_the_grant_reads_nothing(self):
        conversation = self.seed_conversation()
        await self.record("private", conversation=conversation)
        agent = AgentActor(
            USER_ID,
            AgentGrant(uuid4(), frozenset({Capability.CHAT_USE}), ALL_PROJECTS),
        )
        with self.assertRaises(JournalPermissionError):
            await self.journal.pending_observations(agent, conversation)
        with self.assertRaises(JournalPermissionError):
            await self.journal.sync_status(agent, conversation)

    async def test_a_user_role_message_cannot_be_appended_without_an_observation(self):
        with self.assertRaises(InvalidJournalInputError) as raised:
            await self.journal.append_message(
                self.user,
                self.seed_conversation(),
                MessageRole.USER,
                "x",
                turn_id=uuid4(),
            )
        self.assertEqual(raised.exception.field, "role")


@requires_postgres
class PendingObservationsTest(AsyncPostgresJournalTestCase):
    async def test_the_unconsolidated_messages_come_back_oldest_first_with_their_text(
        self,
    ):
        conversation = self.seed_conversation()
        first = await self.record("first", conversation=conversation)
        await self.journal.append_message(
            self.user,
            conversation,
            MessageRole.ASSISTANT,
            "a reply",
            turn_id=first.turn_id,
        )
        second = await self.record("second", conversation=conversation)

        pending = await self.journal.pending_observations(self.user, conversation)

        self.assertEqual(
            [(p.entry_id, p.event_sequence, p.content) for p in pending],
            [(first.entry_id, 0, "first"), (second.entry_id, 2, "second")],
        )
        self.assertEqual(
            (pending[0].conversation_id, pending[0].turn_id),
            (conversation, first.turn_id),
        )
        self.assertIsNotNone(pending[0].recorded_at)

    async def test_a_consolidated_observation_is_no_longer_pending(self):
        conversation = self.seed_conversation()
        done = await self.record("done", conversation=conversation)
        await self.record("open", conversation=conversation)
        self.execute(
            "UPDATE memory_journal_entries SET state = 'consolidated',"
            " consolidated_at = now(), outcome = '{}'::jsonb WHERE id = :e",
            e=done.entry_id,
        )
        pending = await self.journal.pending_observations(self.user, conversation)
        self.assertEqual([p.content for p in pending], ["open"])

    async def test_the_limit_keeps_the_oldest(self):
        conversation = self.seed_conversation()
        for n in range(5):
            await self.record(f"m{n}", conversation=conversation)
        pending = await self.journal.pending_observations(
            self.user, conversation, limit=2
        )
        self.assertEqual([p.content for p in pending], ["m0", "m1"])

    async def test_an_observation_with_a_dead_job_is_still_pending(self):
        conversation = self.seed_conversation()
        receipt = await self.record("keep me", conversation=conversation)
        self.execute(
            "UPDATE memory_consolidation_queue SET status = 'dead', attempts = 5,"
            " finished_at = now() WHERE entry_id = :e",
            e=receipt.entry_id,
        )
        pending = await self.journal.pending_observations(self.user, conversation)
        self.assertEqual([p.content for p in pending], ["keep me"])

    async def test_a_conversation_with_nothing_pending_returns_an_empty_list(self):
        conversation = self.seed_conversation()
        self.assertEqual(
            await self.journal.pending_observations(self.user, conversation), []
        )

    async def test_only_the_owner_reads_them_not_even_an_admin(self):
        conversation = self.seed_conversation()
        await self.record("private", conversation=conversation)
        for reader in (self.other_user, self.admin):
            with self.subTest(reader=reader.system_role):
                with self.assertRaises(ConversationNotFoundError):
                    await self.journal.pending_observations(reader, conversation)
                with self.assertRaises(ConversationNotFoundError):
                    await self.journal.sync_status(reader, conversation)

    async def test_the_text_is_not_in_the_repr(self):
        conversation = self.seed_conversation()
        await self.record("very private text", conversation=conversation)
        (pending,) = await self.journal.pending_observations(self.user, conversation)
        self.assertNotIn("private", repr(pending))
        self.assertEqual(pending.content, "very private text")


@requires_postgres
class SyncStatusTest(AsyncPostgresJournalTestCase):
    """The four labels of the UI, from the state of each pending observation's job."""

    async def status_after(self, sql: str = "", **params):
        conversation = self.seed_conversation()
        receipt = await self.record("x", conversation=conversation)
        if sql:
            self.execute(sql, e=receipt.entry_id, **params)
        status = await self.journal.sync_status(self.user, conversation)
        return (
            status.consolidating,
            status.waiting_for_worker,
            status.retrying,
            status.failed,
        )

    async def test_a_fresh_observation_is_being_consolidated(self):
        self.assertEqual(await self.status_after(), (1, 0, 0, 0))

    async def test_each_job_state_has_its_label(self):
        claimed = (
            "UPDATE memory_consolidation_queue SET status = 'claimed',"
            " claimed_by = 'w', claimed_at = now() - interval '1 second',"
            " lease_expires_at = now() + interval '1 hour', claim_count = 1"
            " WHERE entry_id = :e"
        )
        queued_with = (
            "UPDATE memory_consolidation_queue SET last_failure = '{}'"
            " WHERE entry_id = :e"
        )
        dead = (
            "UPDATE memory_consolidation_queue SET status = 'dead', attempts = 3,"
            " finished_at = now() WHERE entry_id = :e"
        )
        cases = [
            ("claimed", claimed, (1, 0, 0, 0)),
            (
                "waiting for the GPU",
                queued_with.format("worker_unavailable"),
                (0, 1, 0, 0),
            ),
            (
                "a timeout is retried",
                queued_with.format("worker_timeout"),
                (0, 0, 1, 0),
            ),
            ("an error is retried", queued_with.format("worker_error"), (0, 0, 1, 0)),
            (
                "garbage is retried",
                queued_with.format("worker_output_invalid"),
                (0, 0, 1, 0),
            ),
            ("a dead letter has failed", dead, (0, 0, 0, 1)),
            (
                "no job at all is a failure",
                "DELETE FROM memory_consolidation_queue WHERE entry_id = :e",
                (0, 0, 0, 1),
            ),
        ]
        for name, sql, expected in cases:
            with self.subTest(name):
                self.clean_tables()
                self.assertEqual(await self.status_after(sql), expected)

    async def test_consolidated_ones_are_synced_and_only_this_conversation_counts(
        self,
    ):
        conversation = self.seed_conversation()
        other = self.seed_conversation()
        done = await self.record("done", conversation=conversation)
        await self.record("elsewhere", conversation=other)
        self.execute(
            "UPDATE memory_journal_entries SET state = 'consolidated',"
            " consolidated_at = now(), outcome = '{}'::jsonb WHERE id = :e",
            e=done.entry_id,
        )
        status = await self.journal.sync_status(self.user, conversation)
        self.assertEqual((status.pending, status.synced), (0, True))
        elsewhere = await self.journal.sync_status(self.user, other)
        self.assertEqual((elsewhere.pending, elsewhere.synced), (1, False))

    async def test_the_latest_job_of_an_entry_decides(self):
        conversation = self.seed_conversation()
        receipt = await self.record("x", conversation=conversation)
        self.execute(
            "UPDATE memory_consolidation_queue SET status = 'dead', attempts = 1,"
            " finished_at = now() WHERE entry_id = :e",
            e=receipt.entry_id,
        )
        self.execute(
            "INSERT INTO memory_consolidation_queue (entry_id, priority, priority_rank)"
            " VALUES (:e, 'low', 2)",
            e=receipt.entry_id,
        )
        status = await self.journal.sync_status(self.user, conversation)
        # The put-back job is queued again: not failed any more.
        self.assertEqual((status.consolidating, status.failed), (1, 0))


if __name__ == "__main__":
    unittest.main()
