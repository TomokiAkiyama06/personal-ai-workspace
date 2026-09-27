"""What happens when the worker, the output or the write fails; lease fencing.

Real PostgreSQL, a scripted worker. Every failure leaves the observation ``pending``
(nothing is lost), writes nothing to Memory, and is retried or dead-lettered by the
rules of the queue. A worker whose lease is gone can not apply its answer.
"""

import asyncio
import logging
import traceback
import unittest
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from paw_backend.memory.journal import (
    ItemResult,
    JournalDatabaseError,
    JournalError,
    MemoryJournal,
    RunOutcome,
)
from paw_backend.memory.metadata import metadata_change_actor
from paw_backend.memory.models import ActorType, MessageRole

from .journal_support import (
    OTHER_USER_ID,
    USER_ID,
    AsyncPostgresJournalTestCase,
    ScriptedWorker,
    memory,
    requires_postgres,
    worker_output,
)
from .memory_support import sync_database_url

SECRET = "SECRET-MARKER-" + "7f3a9c"  # stands for a conversation's text


class LogCapture(logging.Handler):
    """Collects every record of the ``paw_backend`` loggers, fully rendered."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.rendered: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = self.format(record)
        self.rendered.append(text)

    def text(self) -> str:
        return "\n".join(self.rendered)


class FailureTestCase(AsyncPostgresJournalTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.logs = LogCapture()
        logger = logging.getLogger("paw_backend")
        previous = logger.level
        logger.addHandler(self.logs)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, previous)
        self.addCleanup(logger.removeHandler, self.logs)

    async def run_jobs(self, worker, count=1, **options):
        consolidator = self.new_consolidator(worker, **options)
        return [await consolidator.run_once() for _ in range(count)]

    def assert_nothing_written_and_still_pending(self, receipt):
        self.assertEqual(self.scalar("SELECT count(*) FROM memories"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 0)
        self.assertEqual(
            self.scalar("SELECT count(*) FROM memory_consolidation_keys"), 0
        )
        entry = self.entry_row(receipt.entry_id)
        self.assertEqual((entry["state"], entry["outcome"]), ("pending", None))


@requires_postgres
class InvalidOutputTest(FailureTestCase):
    async def test_an_output_that_breaks_the_contract_is_dropped_whole(self):
        good = memory("fine")
        cases = {
            "not json": "sorry, I cannot do that",
            "a list": "[]",
            "no memories member": "{}",
            "an unknown member": '{"memories": [], "extra": 1}',
            "a wrong scope": worker_output(good, memory("k", scope="team")),
            "a wrong state": worker_output(good, memory("k", state="observed")),
            "a blank key": worker_output(good, memory("   ")),
            "a missing supersedes": worker_output(
                good,
                {"key": "k", "scope": "user", "state": "inferred", "content": "c"},
            ),
            "not text": None,
            "bytes": b'{"memories": []}',
            "an oversized answer": "{" + " " * 500_000 + "}",
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.clean_tables()
                receipt = await self.record("Use tabs.")
                worker = ScriptedWorker(answer)
                (result,) = await self.run_jobs(worker)
                self.assertEqual(
                    (result.outcome, result.failure.value),
                    (RunOutcome.RETRY_SCHEDULED, "worker_output_invalid"),
                )
                # The valid first memory of a bad output is NOT applied either.
                self.assert_nothing_written_and_still_pending(receipt)
                job = self.jobs_of(receipt.entry_id)[0]
                self.assertEqual(
                    (job["status"], job["attempts"], job["last_failure"]),
                    ("queued", 1, "worker_output_invalid"),
                )

    async def test_repeated_garbage_ends_in_the_dead_letter_and_the_text_survives(self):
        queue = self.new_queue(max_attempts=3)
        conversation = self.seed_conversation()
        receipt = await self.record("Remember: I use uv.", conversation=conversation)
        consolidator = self.new_consolidator(
            ScriptedWorker(fallback="garbage"), queue=queue
        )

        outcomes = []
        for _ in range(3):
            outcomes.append((await consolidator.run_once()).outcome)
            self.make_due()

        self.assertEqual(
            outcomes,
            [
                RunOutcome.RETRY_SCHEDULED,
                RunOutcome.RETRY_SCHEDULED,
                RunOutcome.DEAD_LETTERED,
            ],
        )
        self.assertEqual((await consolidator.run_once()).outcome, RunOutcome.IDLE)
        # The instruction is still there for the next turn, and shown as failed.
        pending = await self.journal.pending_observations(self.user, conversation)
        self.assertEqual([p.content for p in pending], ["Remember: I use uv."])
        status = await self.journal.sync_status(self.user, conversation)
        self.assertEqual((status.failed, status.consolidating), (1, 0))
        # An operator puts it back once the cause is fixed.
        await queue.enqueue(receipt.entry_id)
        (result,) = await self.run_jobs(
            ScriptedWorker(worker_output(memory("uv"))), queue=queue
        )
        self.assertEqual(result.outcome, RunOutcome.COMPLETED)
        self.assertEqual(sorted(self.active_versions()), ["uv"])


@requires_postgres
class WorkerErrorTest(FailureTestCase):
    async def test_a_worker_exception_counts_an_attempt_and_leaks_nothing(self):
        receipt = await self.record(f"my secret is {SECRET}")
        worker = ScriptedWorker(RuntimeError(f"model crashed on {SECRET}"))
        (result,) = await self.run_jobs(worker)
        self.assertEqual(
            (result.outcome, result.failure.value),
            (RunOutcome.RETRY_SCHEDULED, "worker_error"),
        )
        self.assert_nothing_written_and_still_pending(receipt)
        self.assertEqual(self.jobs_of(receipt.entry_id)[0]["attempts"], 1)
        self.assertNotIn(SECRET, self.logs.text())
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, repr(self.jobs_of(receipt.entry_id)))

    async def test_a_worker_that_takes_too_long_counts_as_a_timeout(self):
        receipt = await self.record("slow one")

        async def hang(_text: str) -> str:
            await asyncio.sleep(30)
            return worker_output()

        (result,) = await self.run_jobs(
            ScriptedWorker(hang), worker_timeout_seconds=0.2
        )

        self.assertEqual(
            (result.outcome, result.failure.value),
            (RunOutcome.RETRY_SCHEDULED, "worker_timeout"),
        )
        job = self.jobs_of(receipt.entry_id)[0]
        self.assertEqual((job["attempts"], job["deferrals"]), (1, 0))
        self.assert_nothing_written_and_still_pending(receipt)

    async def test_a_cancelled_run_is_recovered_when_its_lease_ends(self):
        receipt = await self.record("Use tabs.")
        started, release = asyncio.Event(), asyncio.Event()

        async def block(_text: str) -> str:
            started.set()
            await release.wait()
            return worker_output()

        run = asyncio.ensure_future(
            self.new_consolidator(ScriptedWorker(block)).run_once()
        )
        await started.wait()
        run.cancel()  # the worker process is going down mid-job
        await asyncio.gather(run, return_exceptions=True)
        job = self.jobs_of(receipt.entry_id)[0]
        self.assertEqual(job["status"], "claimed")  # leased, nobody will finish it
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")

        self.expire_lease(job["id"])
        (result,) = await self.run_jobs(
            ScriptedWorker(worker_output(memory("indent_style"))), worker_id="worker-2"
        )

        self.assertEqual(result.outcome, RunOutcome.COMPLETED)
        self.assertEqual(sorted(self.active_versions()), ["indent_style"])
        done = self.jobs_of(receipt.entry_id)[0]
        self.assertEqual(
            (done["status"], done["claim_count"], done["attempts"]),
            ("completed", 2, 1),  # the crashed claim counted an attempt
        )

    async def test_a_job_whose_workers_kept_dying_is_dead_lettered_without_a_call(self):
        queue = self.new_queue(max_attempts=2)
        receipt = await self.record("poison")
        for _ in range(2):
            job = await queue.claim_next("worker-x")
            self.expire_lease(job.id)
        worker = ScriptedWorker()
        (result,) = await self.run_jobs(worker, queue=queue)
        self.assertEqual(result.outcome, RunOutcome.DEAD_LETTERED)
        self.assertEqual(worker.inputs, [])
        self.assertEqual(self.jobs_of(receipt.entry_id)[0]["status"], "dead")
        self.assert_nothing_written_and_still_pending(receipt)


@requires_postgres
class ApplyFailureTest(FailureTestCase):
    async def test_a_failed_write_rolls_back_the_whole_output(self):
        receipt = await self.record("two facts")
        # The registry says "second" belongs to a memory whose only version is
        # ANOTHER user's: reading it finds nothing, so the write meets an existing
        # version 1 (a corrupted state): the database refuses it.
        foreign = self.seed_key_memory(
            "theirs", "not yours", "inferred", owner=OTHER_USER_ID, register=False
        )
        self.execute(
            "INSERT INTO memory_consolidation_keys (owner_user_id, key, memory_id,"
            " applied_conversation_id, applied_event_sequence, applied_recorded_at)"
            " VALUES (:o, 'second', :m, :c, 0, '2000-01-01T00:00:00+00:00')",
            o=self.user.user_id,
            m=foreign,
            c=uuid4(),
        )
        worker = ScriptedWorker(worker_output(memory("first"), memory("second")))

        (result,) = await self.run_jobs(worker)

        self.assertEqual(
            (result.outcome, result.failure.value),
            (RunOutcome.RETRY_SCHEDULED, "apply_failed"),
        )
        # "first" was written before "second" failed: it is gone with the rollback.
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM memory_consolidation_keys"
                " WHERE owner_user_id = :o AND key = 'first'",
                o=self.user.user_id,
            ),
            0,
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM memory_versions WHERE title = 'first'"), 0
        )
        entry = self.entry_row(receipt.entry_id)
        self.assertEqual((entry["state"], entry["outcome"]), ("pending", None))
        job = self.jobs_of(receipt.entry_id)[0]
        self.assertEqual((job["status"], job["attempts"]), ("queued", 1))
        # Nothing of the other user changed either.
        (theirs,) = self.versions(OTHER_USER_ID)
        self.assertEqual((theirs["content"], theirs["status"]), ("not yours", "active"))

    async def test_a_concurrent_manual_edit_is_not_overwritten(self):
        """Lost update: an edit that commits between our read and our write.

        The edit does not take the consolidator's advisory lock (a manual edit is
        another component). Our ``UPDATE ... WHERE status = 'active'`` finds the
        version already superseded and the write fails instead of overwriting; the
        retry sees the new confirmed version and holds the candidate.
        """
        memory_id = self.seed_key_memory("indent_style", "Use tabs.", "inferred")
        receipt = await self.record("Use spaces.")
        worker = ScriptedWorker(
            worker_output(memory("indent_style", content="Use spaces.")),
            worker_output(memory("indent_style", content="Use spaces.")),
        )
        consolidator = self.new_consolidator(worker)

        connection = self.engine.connect()
        editor = connection.begin()
        self.addCleanup(connection.close)
        # A person edits: the database records a status change with its actor and
        # refuses one that names nobody (revision 0071).
        connection.execute(metadata_change_actor(ActorType.USER, USER_ID))
        connection.execute(
            text(
                "UPDATE memory_versions SET status = 'superseded'"
                " WHERE memory_id = :m AND version_number = 1"
            ),
            {"m": memory_id},
        )
        connection.execute(
            text(
                "INSERT INTO memory_versions (memory_id, version_number, scope,"
                " owner_user_id, memory_type, title, content, status,"
                " confirmation_state, freshness_policy, actor_type, actor_user_id)"
                " VALUES (:m, 2, 'user', :o, 'preference', 'indent_style',"
                " 'Use tabs and 4-wide.', 'active', 'confirmed', 'permanent',"
                " 'user', :o)"
            ),
            {"m": memory_id, "o": self.user.user_id},
        )
        run = self.spawn(consolidator.run_once())
        # The consolidator read version 1 as active and is now blocked on the edit.
        await self.wait_until_a_backend_waits_for_a_lock()
        editor.commit()

        result = await run

        self.assertEqual(
            (result.outcome, result.failure.value),
            (RunOutcome.RETRY_SCHEDULED, "apply_failed"),
        )
        versions = self.versions()
        self.assertEqual(
            [(v["version_number"], v["status"], v["content"]) for v in versions],
            [(1, "superseded", "Use tabs."), (2, "active", "Use tabs and 4-wide.")],
        )
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")

        # The retry meets the confirmed version and only holds the candidate.
        self.make_due()
        again = await consolidator.run_once()
        self.assertEqual(again.items, (ItemResult.HELD_CONFIRMED,))
        self.assertEqual(len(self.versions()), 2)

    async def test_a_concurrent_change_of_the_memory_it_supersedes_is_not_overwritten(
        self,
    ):
        """Only the row count of the superseding UPDATE can notice this one.

        The new version of ``ide`` does not clash with anything in the schema (it
        is a different memory); the memory it retires is changed by a person while
        the consolidator waits for the row. The UPDATE then matches no row, and the
        consolidator must fail rather than relate a version that is no longer the
        active one.
        """
        old = self.seed_key_memory("editor", "Uses vim.", "inferred")
        receipt = await self.record("Now VS Code.")
        worker = ScriptedWorker(
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
            worker_output(memory("ide", content="Uses VS Code.", supersedes="editor")),
        )
        consolidator = self.new_consolidator(worker)

        connection = self.engine.connect()
        person = connection.begin()
        self.addCleanup(connection.close)
        connection.execute(metadata_change_actor(ActorType.USER, USER_ID))
        connection.execute(
            text(
                "UPDATE memory_versions SET status = 'deprecated' WHERE memory_id = :m"
            ),
            {"m": old},
        )
        run = self.spawn(consolidator.run_once())
        await self.wait_until_a_backend_waits_for_a_lock()
        person.commit()

        result = await run

        self.assertEqual(
            (result.outcome, result.failure.value),
            (RunOutcome.RETRY_SCHEDULED, "apply_failed"),
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_relations"), 0)
        self.assertEqual([v["key"] for v in self.versions()], ["editor"])
        self.assertEqual(self.versions()[0]["status"], "deprecated")
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")

        # The retry sees the deprecated memory and writes the new one on its own.
        self.make_due()
        again = await consolidator.run_once()
        self.assertEqual(again.items, (ItemResult.CREATED,))
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_relations"), 0)
        self.assertEqual(sorted(self.active_versions()), ["ide"])


@requires_postgres
class LeaseFencingEndToEndTest(FailureTestCase):
    async def test_a_worker_that_lost_its_lease_cannot_apply_its_answer(self):
        receipt = await self.record("Use tabs.")
        queue = self.new_queue()
        second = self.new_consolidator(
            ScriptedWorker(
                worker_output(memory("indent_style", content="from the second"))
            ),
            queue=queue,
            worker_id="worker-2",
        )
        seen = []

        async def slow_first(_text: str) -> str:
            # While the first worker "thinks", its lease runs out and another
            # worker takes the job and finishes it.
            self.expire_lease(self.jobs_of(receipt.entry_id)[0]["id"])
            seen.append(await second.run_once())
            return worker_output(memory("indent_style", content="from the first"))

        first = self.new_consolidator(ScriptedWorker(slow_first), queue=queue)

        result = await first.run_once()

        self.assertEqual(result.outcome, RunOutcome.LEASE_LOST)
        self.assertEqual(seen[0].outcome, RunOutcome.COMPLETED)
        (only,) = self.versions()
        self.assertEqual(only["content"], "from the second")
        job = self.jobs_of(receipt.entry_id)[0]
        self.assertEqual((job["status"], job["claim_count"]), ("completed", 2))
        self.assertEqual(self.entry_row(receipt.entry_id)["state"], "consolidated")

    async def test_a_lease_that_ran_out_before_the_write_is_not_honoured(self):
        receipt = await self.record("Use tabs.")

        async def expires_then_answers(_text: str) -> str:
            self.expire_lease(self.jobs_of(receipt.entry_id)[0]["id"])
            return worker_output(memory("indent_style"))

        (result,) = await self.run_jobs(ScriptedWorker(expires_then_answers))

        self.assertEqual(result.outcome, RunOutcome.LEASE_LOST)
        self.assert_nothing_written_and_still_pending(receipt)
        # Nobody holds it now: the next claim picks it up again.
        (again,) = await self.run_jobs(
            ScriptedWorker(worker_output(memory("indent_style"))), worker_id="worker-2"
        )
        self.assertEqual(again.outcome, RunOutcome.COMPLETED)

    async def test_a_deleted_conversation_makes_the_lease_lost_and_writes_nothing(self):
        conversation = self.seed_conversation()
        await self.record("Use tabs.", conversation=conversation)

        async def deleted_meanwhile(_text: str) -> str:
            self.execute("DELETE FROM conversations WHERE id = :c", c=conversation)
            return worker_output(memory("indent_style"))

        (result,) = await self.run_jobs(ScriptedWorker(deleted_meanwhile))
        self.assertEqual(result.outcome, RunOutcome.LEASE_LOST)
        self.assertEqual(self.scalar("SELECT count(*) FROM memory_versions"), 0)

    async def test_an_already_consolidated_entry_is_not_asked_of_the_worker_again(self):
        receipt = await self.record("Use tabs.")
        await self.run_jobs(ScriptedWorker(worker_output(memory("indent_style"))))
        # A duplicate job (say, from a manual re-queue that raced the completion).
        self.execute(
            "INSERT INTO memory_consolidation_queue (entry_id, priority, priority_rank)"
            " VALUES (:e, 'normal', 1)",
            e=receipt.entry_id,
        )
        worker = ScriptedWorker()
        (result,) = await self.run_jobs(worker)
        self.assertEqual(result.outcome, RunOutcome.ALREADY_DONE)
        self.assertEqual(worker.inputs, [])
        self.assertEqual(len(self.versions()), 1)
        self.assertEqual(
            [j["status"] for j in self.jobs_of(receipt.entry_id)],
            ["completed", "completed"],
        )


def exception_chain(error: BaseException) -> list[BaseException]:
    """``error`` and every exception reachable by ``__cause__`` / ``__context__``."""
    seen: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None or any(current is known for known in seen):
            continue
        seen.append(current)
        pending += [current.__cause__, current.__context__]
    return seen


@requires_postgres
class DatabaseErrorPrivacyTest(FailureTestCase):
    """A database error of a write never carries the message's content.

    SQLAlchemy's error text lists the bound parameters (``content``), and
    PostgreSQL's ``DETAIL`` of a check violation quotes the failing row. The engine
    here is built WITHOUT ``hide_parameters`` (the shared ``Database`` sets it; the
    journal must not depend on that), and the failures are provoked by a check
    constraint and a trigger that the test adds as the schema's owner.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        database = self.new_database()
        # The URL with the psycopg driver (it serves the async engine as well).
        database._engine = create_async_engine(
            sync_database_url(), hide_parameters=False
        )
        self.leaky = database
        self.journal = MemoryJournal(database, self.authorizer)

    def reject_the_secret_by_a_check(self) -> None:
        self.execute(
            "ALTER TABLE messages ADD CONSTRAINT paw_test_no_secret"
            f" CHECK (position('{SECRET}' IN content) = 0) NOT VALID"
        )
        self.addCleanup(
            self.execute,
            "ALTER TABLE messages DROP CONSTRAINT IF EXISTS paw_test_no_secret",
        )

    def reject_every_message_by_a_trigger(self) -> None:
        # The trigger's own message quotes the content: the driver's text leaks too.
        self.execute(
            "CREATE FUNCTION paw_test_reject_message() RETURNS trigger"
            " LANGUAGE plpgsql AS $$ BEGIN"
            " RAISE EXCEPTION 'rejected: %', NEW.content; END $$"
        )
        self.execute(
            "CREATE TRIGGER paw_test_reject_message BEFORE INSERT ON messages"
            " FOR EACH ROW EXECUTE FUNCTION paw_test_reject_message()"
        )
        self.addCleanup(
            self.execute, "DROP FUNCTION IF EXISTS paw_test_reject_message() CASCADE"
        )

    def assert_leaks_nothing(self, error: JournalDatabaseError) -> None:
        chain = exception_chain(error)
        self.assertEqual(chain, [error])  # no __cause__, no __context__
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for shown in (
            str(error),
            repr(error),
            repr(error.args),
            "".join(traceback.format_exception(error)),
        ):
            self.assertNotIn(SECRET, shown)
        self.assertNotIn(SECRET, self.logs.text())

    def assert_nothing_saved(self) -> None:
        for table in (
            "messages",
            "memory_journal_entries",
            "memory_consolidation_queue",
        ):
            self.assertEqual(self.scalar(f"SELECT count(*) FROM {table}"), 0, table)

    async def test_the_engine_would_leak_the_content_so_the_test_can_see_it(self):
        # Without the journal's translation the same failure shows the content:
        # the assertions below would catch a regression.
        self.reject_the_secret_by_a_check()
        conversation = self.seed_conversation()
        with self.assertRaises(DBAPIError) as caught:
            async with self.leaky.session() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO messages (conversation_id, turn_id,"
                        " event_sequence, role, content)"
                        " VALUES (:c, :t, 0, 'user', :content)"
                    ),
                    {"c": conversation, "t": uuid4(), "content": SECRET},
                )
        self.assertIn(SECRET, str(caught.exception))

    async def test_a_refused_user_message_is_a_typed_error_without_the_content(self):
        self.reject_the_secret_by_a_check()
        conversation = self.seed_conversation()

        with self.assertRaises(JournalDatabaseError) as caught:
            await self.journal.record_user_message(
                self.user, conversation, f"my password is {SECRET}"
            )

        self.assertIsInstance(caught.exception, JournalError)
        self.assertEqual(caught.exception.code, "journal_database_error")
        self.assertEqual(caught.exception.sqlstate, "23514")  # check violation
        self.assert_leaks_nothing(caught.exception)
        self.assert_nothing_saved()

    async def test_a_refused_appended_message_is_a_typed_error_without_the_content(
        self,
    ):
        conversation = self.seed_conversation()
        self.reject_every_message_by_a_trigger()

        with self.assertRaises(JournalDatabaseError) as caught:
            await self.journal.append_message(
                self.user,
                conversation,
                MessageRole.ASSISTANT,
                f"the token is {SECRET}",
                turn_id=uuid4(),
            )

        self.assertEqual(caught.exception.sqlstate, "P0001")  # raise_exception
        self.assert_leaks_nothing(caught.exception)
        self.assert_nothing_saved()

    async def test_the_journal_works_again_once_the_database_accepts(self):
        self.reject_the_secret_by_a_check()
        conversation = self.seed_conversation()
        with self.assertRaises(JournalDatabaseError):
            await self.journal.record_user_message(self.user, conversation, SECRET)
        receipt = await self.journal.record_user_message(
            self.user, conversation, "fine"
        )
        self.assertEqual(receipt.event_sequence, 0)

    def test_a_sqlstate_that_is_not_a_code_is_not_kept(self):
        self.assertIsNone(JournalDatabaseError(f"x {SECRET}").sqlstate)
        self.assertEqual(JournalDatabaseError("23505").sqlstate, "23505")
        self.assertEqual(
            str(JournalDatabaseError("23505")), "Journal database operation failed"
        )


@requires_postgres
class LoggingPrivacyTest(FailureTestCase):
    async def test_no_log_line_error_or_row_carries_the_conversation(self):
        conversation = self.seed_conversation()
        text = f"remember this: {SECRET}"
        await self.record(text, conversation=conversation)
        await self.record(text, conversation=conversation)
        await self.record(text, conversation=conversation)
        answers = [
            worker_output(memory("fact", content=f"has {SECRET}")),
            f"not json {SECRET}",
            ValueError(f"boom {SECRET}"),
        ]
        consolidator = self.new_consolidator(ScriptedWorker(*answers))
        results = [await consolidator.run_once() for _ in range(3)]

        self.assertEqual(
            [r.outcome for r in results],
            [
                RunOutcome.COMPLETED,
                RunOutcome.RETRY_SCHEDULED,
                RunOutcome.RETRY_SCHEDULED,
            ],
        )
        self.assertTrue(self.logs.rendered)  # something was logged ...
        self.assertNotIn(SECRET, self.logs.text())  # ... but never the text
        self.assertNotIn(SECRET, repr(results))
        # The audit trail holds ids and capability names only.
        pending = await self.journal.pending_observations(self.user, conversation)
        self.assertNotIn(SECRET, repr(pending))
        self.assertNotIn(SECRET, repr(list(self.sink.events)))
        # And what was stored about the failed jobs is a closed code, not a message.
        jobs = self.rows("SELECT last_failure FROM memory_consolidation_queue")
        self.assertNotIn(SECRET, repr(jobs))


if __name__ == "__main__":
    unittest.main()
