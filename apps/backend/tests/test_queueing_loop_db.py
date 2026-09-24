"""``LoopDetector`` on a real PostgreSQL. Skipped unless ``PAW_TEST_DATABASE_URL``
is set (except the constructor checks)."""

import asyncio
import unittest
import uuid

from paw_backend.tasks import TaskNotFoundError
from paw_backend.tasks.queueing import (
    DEFAULT_LOOP_POLICY,
    FailureRecord,
    InvalidQueueingArgumentError,
    LoopAssessment,
    LoopDetector,
    LoopPolicy,
    LoopVerdict,
    failure_signature,
)

from .queueing_support import PostgresQueueingTestCase, requires_postgres

V = LoopVerdict
# sha256("ToolError\x1frun_tests\x1ftimeout after <n>s on <hex>")
TIMEOUT = "e8deabc6dfab7d7589e92aa2700d0a163c45ac2e11022ef3e9e7feef902ebcfd"


class ConstructorTest(unittest.TestCase):
    def test_the_policy_defaults_and_is_validated(self):
        self.assertEqual(LoopDetector(object()).policy, DEFAULT_LOOP_POLICY)
        policy = LoopPolicy(repeat_threshold=2, window_size=4, max_alternatives=0)
        self.assertEqual(LoopDetector(object(), policy).policy, policy)
        for bad in (None, {"window_size": 3}, 3):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    LoopDetector(object(), bad)
                self.assertEqual(caught.exception.parameter, "policy")


class LoopTestCase(PostgresQueueingTestCase):
    async def fail(
        self, task_id, message="Timeout after 30s on 0x7FFF", approach=0, **kw
    ):
        arguments = dict(error_class="ToolError", step="run_tests", message=message)
        arguments.update(kw)
        return await self.loop_detector.record_failure(
            task_id, approach=approach, **arguments
        )

    async def stored(self, task_id=None) -> list[dict]:
        sql = "SELECT * FROM loop_failure_signatures"
        if task_id is not None:
            return await self.rows(sql + " WHERE task_id = :t ORDER BY seq", t=task_id)
        return await self.rows(sql + " ORDER BY seq")


@requires_postgres
class RecordFailureTest(LoopTestCase):
    async def test_the_same_failure_repeated_leads_to_an_alternative_then_escalation(
        self,
    ):
        (task_id,) = await self.make_tasks(1)
        first_approach = [
            await self.fail(task_id, "Timeout after 30s on 0x7FFF"),
            await self.fail(task_id, "timeout after 45s on 0xABC123"),
            await self.fail(task_id, "TIMEOUT AFTER 9s on 0x1"),
        ]
        self.assertEqual(
            first_approach,
            [
                LoopAssessment(V.CONTINUE, TIMEOUT, 0, 1),
                LoopAssessment(V.CONTINUE, TIMEOUT, 0, 2),
                LoopAssessment(V.TRY_ALTERNATIVE, TIMEOUT, 0, 3),
            ],
        )
        second_approach = [
            await self.fail(task_id, "Timeout after 31s on 0x7FFF", approach=1),
            await self.fail(task_id, "Timeout after 32s on 0x7FFF", approach=1),
            await self.fail(task_id, "Timeout after 33s on 0x7FFF", approach=1),
        ]
        self.assertEqual(
            second_approach,
            [
                LoopAssessment(V.CONTINUE, TIMEOUT, 1, 1),
                LoopAssessment(V.CONTINUE, TIMEOUT, 1, 2),
                LoopAssessment(V.ESCALATE, TIMEOUT, 1, 3),
            ],
        )

    async def test_a_different_failure_is_not_a_loop(self):
        (task_id,) = await self.make_tasks(1)
        await self.fail(task_id)
        await self.fail(task_id)
        other = await self.fail(task_id, "Connection refused")
        self.assertEqual(other.verdict, V.CONTINUE)
        self.assertEqual(other.repeats, 1)
        self.assertEqual(
            other.signature,
            failure_signature("ToolError", "run_tests", "Connection refused"),
        )

    async def test_class_and_step_are_part_of_the_signature(self):
        (task_id,) = await self.make_tasks(1)
        await self.fail(task_id)
        await self.fail(task_id)
        changed_class = await self.fail(task_id, error_class="OtherError")
        changed_step = await self.fail(task_id, step="build")
        self.assertEqual((changed_class.repeats, changed_step.repeats), (1, 1))
        self.assertEqual(changed_class.verdict, V.CONTINUE)

    async def test_only_signatures_are_stored_never_the_failure(self):
        (task_id,) = await self.make_tasks(1)
        await self.fail(
            task_id,
            message="Authorization: Bearer sk-live-SECRET-9999 rejected "
            "for /home/alice/keys",
            error_class="SecretLeakError",
            step="deploy-to-Production",
        )
        rows = await self.stored(task_id)
        self.assertEqual(len(rows), 1)
        dump = repr(rows).lower()
        for fragment in (
            "secret",
            "bearer",
            "alice",
            "leakerror",
            "production",
            "rejected",
        ):
            self.assertNotIn(fragment, dump)
        self.assertRegex(rows[0]["signature"], r"^[0-9a-f]{64}$")

    async def test_the_window_keeps_only_the_newest_records(self):
        (task_id,) = await self.make_tasks(1)
        letters = "abcdefghijklmnopqrstuvwxy"  # 25 different failures
        for letter in letters:
            await self.fail(task_id, message=f"failure {letter}")
        rows = await self.stored(task_id)
        self.assertEqual(len(rows), 10)
        expected = [
            failure_signature("ToolError", "run_tests", f"failure {letter}")
            for letter in letters[-10:]
        ]
        self.assertEqual([row["signature"] for row in rows], expected)
        self.assertEqual(
            [record.signature for record in await self.loop_detector.history(task_id)],
            expected,
        )

    async def test_the_window_is_kept_per_task(self):
        first, second = await self.make_tasks(2)
        for _ in range(15):
            await self.fail(first)
        for _ in range(4):
            await self.fail(second)
        self.assertEqual(len(await self.stored(first)), 10)
        self.assertEqual(len(await self.stored(second)), 4)
        self.assertEqual((await self.loop_detector.assess(second)).repeats, 4)

    async def test_the_window_follows_the_policy(self):
        (task_id,) = await self.make_tasks(1)
        policy = LoopPolicy(repeat_threshold=2, window_size=4, max_alternatives=0)
        detector = LoopDetector(self.database, policy)
        results = [
            await detector.record_failure(
                task_id, error_class="E", step="s", message="same"
            )
            for _ in range(6)
        ]
        self.assertEqual([r.verdict for r in results], [V.CONTINUE] + [V.ESCALATE] * 5)
        self.assertEqual([r.repeats for r in results], [1, 2, 3, 4, 4, 4])
        self.assertEqual(len(await self.stored(task_id)), 4)

    async def test_an_unknown_task_is_not_found_and_nothing_is_stored(self):
        with self.assertRaises(TaskNotFoundError):
            await self.fail(uuid.uuid4())
        self.assertEqual(await self.stored(), [])

    async def test_arguments_are_validated_before_anything_is_stored(self):
        (task_id,) = await self.make_tasks(1)
        secret = "sk-secret-abc"
        cases = [
            ("task_id", dict(task_id=str(task_id))),
            ("error_class", dict(error_class="")),
            ("error_class", dict(error_class=None)),
            ("error_class", dict(error_class="E" * 201)),
            ("step", dict(step="")),
            ("step", dict(step="s" * 101)),
            ("message", dict(message=None)),
            ("message", dict(message=5)),
            ("approach", dict(approach=-1)),
            ("approach", dict(approach=101)),
            ("approach", dict(approach=True)),
            ("approach", dict(approach="0")),
            ("error_class", dict(error_class=secret + "\n")),
        ]
        for parameter, overrides in cases:
            arguments = dict(
                task_id=task_id, error_class="E", step="s", message="m", approach=0
            )
            arguments.update(overrides)
            call_task = arguments.pop("task_id")
            with self.subTest(parameter=parameter, overrides=overrides):
                with self.assertRaises(InvalidQueueingArgumentError) as caught:
                    await self.loop_detector.record_failure(call_task, **arguments)
                self.assertEqual(caught.exception.parameter, parameter)
                self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(await self.stored(), [])

    async def test_simultaneous_failures_of_one_task_are_all_recorded_within_the_window(
        self,
    ):
        (task_id,) = await self.make_tasks(1)
        detectors = [self.new_loop_detector() for _ in range(12)]
        results = await asyncio.gather(
            *(
                d.record_failure(task_id, error_class="E", step="s", message="same")
                for d in detectors
            )
        )
        self.assertEqual(len(results), 12)
        rows = await self.stored(task_id)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({row["seq"] for row in rows}), 10)
        final = await self.loop_detector.assess(task_id)
        self.assertEqual((final.verdict, final.repeats), (V.TRY_ALTERNATIVE, 10))


@requires_postgres
class HistoryTest(LoopTestCase):
    async def test_history_is_oldest_first_with_the_approach(self):
        (task_id,) = await self.make_tasks(1)
        await self.fail(task_id, "one")
        await self.fail(task_id, "two", approach=1)
        await self.fail(task_id, "one", approach=2)
        history = await self.loop_detector.history(task_id)
        self.assertIsInstance(history, tuple)
        self.assertEqual(
            history,
            (
                FailureRecord(failure_signature("ToolError", "run_tests", "one"), 0),
                FailureRecord(failure_signature("ToolError", "run_tests", "two"), 1),
                FailureRecord(failure_signature("ToolError", "run_tests", "one"), 2),
            ),
        )

    async def test_a_task_without_failures_has_an_empty_history(self):
        (task_id,) = await self.make_tasks(1)
        self.assertEqual(await self.loop_detector.history(task_id), ())
        self.assertEqual(await self.loop_detector.history(uuid.uuid4()), ())
        self.assertEqual(
            await self.loop_detector.assess(task_id),
            LoopAssessment(V.CONTINUE, None, None, 0),
        )

    async def test_assess_repeats_the_last_assessment_any_number_of_times(self):
        (task_id,) = await self.make_tasks(1)
        for _ in range(2):
            await self.fail(task_id)
        last = await self.fail(task_id)
        self.assertEqual(last.verdict, V.TRY_ALTERNATIVE)
        results = [await self.loop_detector.assess(task_id) for _ in range(3)]
        self.assertEqual(results, [last, last, last])
        self.assertEqual(len(await self.stored(task_id)), 3)

    async def test_assess_and_history_validate_the_task_id(self):
        for call in (
            lambda: self.loop_detector.assess("x"),
            lambda: self.loop_detector.history("x"),
            lambda: self.loop_detector.clear("x"),
        ):
            with self.assertRaises(InvalidQueueingArgumentError) as caught:
                await call()
            self.assertEqual(caught.exception.parameter, "task_id")


@requires_postgres
class ClearTest(LoopTestCase):
    async def test_clear_forgets_the_failures_of_one_task(self):
        first, second = await self.make_tasks(2)
        for _ in range(3):
            await self.fail(first)
        await self.fail(second)
        self.assertEqual(await self.loop_detector.clear(first), 3)
        self.assertEqual(await self.loop_detector.history(first), ())
        self.assertEqual(
            await self.loop_detector.assess(first),
            LoopAssessment(V.CONTINUE, None, None, 0),
        )
        self.assertEqual(len(await self.stored(second)), 1)

    async def test_clearing_nothing_returns_zero(self):
        (task_id,) = await self.make_tasks(1)
        self.assertEqual(await self.loop_detector.clear(task_id), 0)
        self.assertEqual(await self.loop_detector.clear(uuid.uuid4()), 0)

    async def test_after_a_clear_the_detection_starts_over(self):
        (task_id,) = await self.make_tasks(1)
        for _ in range(3):
            await self.fail(task_id)
        await self.loop_detector.clear(task_id)
        again = await self.fail(task_id)
        self.assertEqual((again.verdict, again.repeats), (V.CONTINUE, 1))


if __name__ == "__main__":
    unittest.main()
