"""The GPU / Memory Worker is unavailable: Pending observations are NOT lost.

Acceptance criterion "GPU unavailable時もPendingを失わない" (REQUIREMENTS.md "GPU /
Kaggle Mode": "GPUサービス停止中でもRaw ConversationとPending Observationは保存する。
Memory ConsolidationがGPUを必要とする場合はQueueへ保持し、GPU復帰後に再開する").
Real PostgreSQL; the worker is scripted, and "the GPU comes back" is a change of the
script.
"""

import unittest

from paw_backend.memory.journal import (
    RunOutcome,
    WorkerUnavailableError,
)

from .journal_support import (
    AsyncPostgresJournalTestCase,
    ScriptedWorker,
    memory,
    requires_postgres,
    worker_output,
)


class GpuDownWorker:
    """A worker whose GPU can be switched off and on; records who asked."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.up = False
        self.error = error or WorkerUnavailableError()
        self.inputs: list[str] = []
        self.answers = 0

    async def extract(self, input_text: str) -> str:
        self.inputs.append(input_text)
        if not self.up:
            raise self.error
        self.answers += 1
        return worker_output(memory(f"fact_{self.answers}", content=input_text))


@requires_postgres
class GpuUnavailableTest(AsyncPostgresJournalTestCase):
    async def record_three(self):
        conversation = self.seed_conversation()
        return conversation, [
            await self.record(text, conversation=conversation)
            for text in ("first instruction", "second instruction", "third instruction")
        ]

    async def test_raw_messages_and_observations_are_saved_with_no_worker_at_all(self):
        conversation, receipts = await self.record_three()
        # Nothing consolidated anything, and nothing needed a worker to save.
        self.assertEqual(
            self.scalar("SELECT count(*) FROM messages WHERE role = 'user'"), 3
        )
        pending = await self.journal.pending_observations(self.user, conversation)
        self.assertEqual(
            [(p.event_sequence, p.content) for p in pending],
            [
                (0, "first instruction"),
                (1, "second instruction"),
                (2, "third instruction"),
            ],
        )
        self.assertEqual(len(receipts), 3)

    async def test_an_unavailable_worker_keeps_every_observation_pending_and_queued(
        self,
    ):
        conversation, receipts = await self.record_three()
        worker = GpuDownWorker()
        consolidator = self.new_consolidator(worker)

        results = await consolidator.run_batch()

        # The GPU is asked ONCE per batch, not once per job.
        self.assertEqual([r.outcome for r in results], [RunOutcome.WORKER_UNAVAILABLE])
        self.assertEqual(len(worker.inputs), 1)
        for receipt in receipts:
            with self.subTest(sequence=receipt.event_sequence):
                self.assertEqual(self.entry_row(receipt.entry_id)["state"], "pending")
                (job,) = self.jobs_of(receipt.entry_id)
                self.assertEqual(job["status"], "queued")
        first = self.jobs_of(receipts[0].entry_id)[0]
        self.assertEqual(
            (first["last_failure"], first["attempts"], first["deferrals"]),
            ("worker_unavailable", 0, 1),
        )
        # The next turn still sees all three instructions.
        pending = await self.journal.pending_observations(self.user, conversation)
        self.assertEqual(len(pending), 3)
        status = await self.journal.sync_status(self.user, conversation)
        self.assertEqual(
            (
                status.consolidating,
                status.waiting_for_worker,
                status.retrying,
                status.failed,
            ),
            (2, 1, 0, 0),
        )
        self.assertFalse(status.synced)

    async def test_a_long_outage_never_dead_letters_anything(self):
        conversation, receipts = await self.record_three()
        worker = GpuDownWorker()
        consolidator = self.new_consolidator(worker)
        for _ in range(25):  # far more than max_attempts (5) rounds
            self.make_due()
            await consolidator.run_batch()
        rows = self.rows(
            "SELECT status, attempts, deferrals FROM memory_consolidation_queue"
        )
        self.assertEqual({r["status"] for r in rows}, {"queued"})
        self.assertEqual({r["attempts"] for r in rows}, {0})
        self.assertGreaterEqual(sum(r["deferrals"] for r in rows), 25)
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 3)
        pending = await self.journal.pending_observations(self.user, conversation)
        self.assertEqual(len(pending), 3)
        self.assertEqual(len(receipts), 3)

    async def test_consolidation_resumes_when_the_gpu_returns_in_event_order(self):
        conversation, receipts = await self.record_three()
        worker = GpuDownWorker()
        consolidator = self.new_consolidator(worker)
        await consolidator.run_batch()
        self.assertEqual(await self.pending_count(conversation), 3)

        worker.up = True
        self.make_due()
        results = await consolidator.run_batch()

        self.assertEqual(
            [r.outcome for r in results],
            [RunOutcome.COMPLETED] * 3 + [RunOutcome.IDLE],
        )
        # The oldest instruction is processed first: the event sequence order.
        self.assertEqual(
            worker.inputs[1:],
            ["first instruction", "second instruction", "third instruction"],
        )
        self.assertEqual(await self.pending_count(conversation), 0)
        status = await self.journal.sync_status(self.user, conversation)
        self.assertTrue(status.synced)
        self.assertEqual(sorted(self.active_versions()), ["fact_1", "fact_2", "fact_3"])
        for receipt in receipts:
            self.assertEqual(self.entry_row(receipt.entry_id)["state"], "consolidated")

    async def test_a_worker_that_is_back_but_fails_still_counts_real_failures(self):
        conversation, receipts = await self.record_three()
        worker = ScriptedWorker(
            WorkerUnavailableError(),
            "not json at all",
            fallback=worker_output(),
        )
        consolidator = self.new_consolidator(worker, batch_size=1)

        first = await consolidator.run_once()  # the GPU is down
        self.make_due()
        second = await consolidator.run_once()  # up, but the answer is garbage

        self.assertEqual(
            (first.outcome, second.outcome),
            (RunOutcome.WORKER_UNAVAILABLE, RunOutcome.RETRY_SCHEDULED),
        )
        job = self.jobs_of(receipts[0].entry_id)[0]
        self.assertEqual(
            (job["attempts"], job["deferrals"], job["last_failure"]),
            (1, 1, "worker_output_invalid"),
        )
        self.assertEqual(await self.pending_count(conversation), 3)

    async def test_a_refused_connection_is_read_as_unavailable_too(self):
        await self.record_three()
        worker = GpuDownWorker(error=ConnectionRefusedError("connect failed"))
        result = await self.new_consolidator(worker).run_once()
        self.assertEqual(result.outcome, RunOutcome.WORKER_UNAVAILABLE)

    async def test_the_status_says_waiting_for_the_gpu_then_synced(self):
        conversation, _ = await self.record_three()
        worker = GpuDownWorker()
        consolidator = self.new_consolidator(worker, batch_size=1)
        for _ in range(3):  # each deferred job steps aside for the next one
            await consolidator.run_once()
        waiting = await self.journal.sync_status(self.user, conversation)
        self.assertEqual(waiting.waiting_for_worker, 3)
        self.assertEqual(waiting.pending, 3)

        worker.up = True
        self.make_due()
        await self.new_consolidator(worker).run_batch()
        done = await self.journal.sync_status(self.user, conversation)
        self.assertEqual((done.pending, done.synced), (0, True))

    async def pending_count(self, conversation) -> int:
        pending = await self.journal.pending_observations(self.user, conversation)
        return len(pending)


if __name__ == "__main__":
    unittest.main()
