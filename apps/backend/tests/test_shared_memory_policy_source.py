"""Loading the System Security Policy safely (``memory.shared.policy``): no database."""

import asyncio
import logging
import unittest

from paw_backend.memory.shared import (
    PolicySourceError,
    StaticPolicySource,
    SystemPolicyItem,
    load_policies,
)

from .shared_memory_support import policy

SECRET = "postgres://user:hunter2@db.internal/policies"


class ItemsSource:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def items(self):
        self.calls += 1
        return self.result


class RaisingSource:
    async def items(self):
        raise ConnectionError(SECRET)


class SlowSource:
    """Very slow, but bounded: a missing timeout must fail the test, not hang it."""

    async def items(self):
        await asyncio.sleep(5)
        return ()


class StaticPolicySourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_returns_its_items_as_a_tuple(self):
        items = [policy("a", "merge"), policy("b", "deploy")]
        self.assertEqual(await StaticPolicySource(items).items(), tuple(items))

    async def test_it_is_empty_by_default(self):
        self.assertEqual(await StaticPolicySource().items(), ())

    async def test_it_copies_its_input(self):
        items = [policy("a", "merge")]
        source = StaticPolicySource(items)
        items.append(policy("b", "deploy"))
        self.assertEqual(len(await source.items()), 1)

    def test_a_bad_list_is_refused_when_the_source_is_built(self):
        bad = [
            [policy("a", "merge"), policy("a", "deploy")],  # duplicate id
            ["not an item"],
            "abc",
            {"a": policy("a", "merge")},
            [policy(f"p{n}", "merge") for n in range(501)],
        ]
        for items in bad:
            with self.subTest(kind=type(items).__name__, size=len(items)):
                with self.assertRaises(PolicySourceError):
                    StaticPolicySource(items)

    def test_five_hundred_items_are_allowed(self):
        StaticPolicySource([policy(f"p{n}", "merge") for n in range(500)])


class LoadPoliciesTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_valid_source_is_loaded_as_a_tuple(self):
        items = [policy("a", "merge"), policy("b", "deploy")]
        loaded = await load_policies(ItemsSource(items), timeout_seconds=1)
        self.assertEqual(loaded, tuple(items))
        self.assertIsInstance(loaded, tuple)

    async def test_a_tuple_is_accepted(self):
        items = (policy("a", "merge"),)
        self.assertEqual(
            await load_policies(ItemsSource(items), timeout_seconds=1), items
        )

    async def test_the_source_is_asked_every_time(self):
        source = ItemsSource([])
        await load_policies(source, timeout_seconds=1)
        await load_policies(source, timeout_seconds=1)
        self.assertEqual(source.calls, 2)

    async def test_an_error_of_the_source_fails_closed_without_its_text(self):
        with self.assertLogs(
            "paw_backend.memory.shared.policy", logging.WARNING
        ) as logs:
            with self.assertRaises(PolicySourceError) as caught:
                await load_policies(RaisingSource(), timeout_seconds=1)
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        joined = " ".join(logs.output)
        self.assertIn("ConnectionError", joined)
        self.assertNotIn("hunter2", joined)

    async def test_a_source_that_does_not_answer_in_time_fails_closed(self):
        async with asyncio.timeout(10):  # generous outer guard, the source sleeps 5 s
            with self.assertRaises(PolicySourceError):
                await load_policies(SlowSource(), timeout_seconds=0.05)

    async def test_answers_that_break_the_contract_fail_closed(self):
        item = policy("a", "merge")
        bad_answers = [
            None,
            "abc",
            {"a": item},
            {item},
            iter([item]),
            (x for x in [item]),
            [item, "not an item"],
            [item, policy("a", "deploy")],  # the same id twice
            [policy(f"p{n}", "merge") for n in range(501)],
            [{"policy_id": "a", "subject": "merge", "statement": "s"}],
        ]
        for answer in bad_answers:
            with self.subTest(answer=type(answer).__name__):
                with self.assertRaises(PolicySourceError):
                    await load_policies(ItemsSource(answer), timeout_seconds=1)

    async def test_an_item_that_only_looks_like_an_item_is_refused(self):
        class Lookalike:
            policy_id = "a"
            subject = "merge"
            statement = "s"

        with self.assertRaises(PolicySourceError):
            await load_policies(ItemsSource([Lookalike()]), timeout_seconds=1)

    async def test_cancellation_is_not_swallowed(self):
        task = asyncio.ensure_future(load_policies(SlowSource(), timeout_seconds=30))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_the_item_type_is_the_contract_type(self):
        loaded = await load_policies(
            ItemsSource([SystemPolicyItem("a", "merge", "s")]), timeout_seconds=1
        )
        self.assertIsInstance(loaded[0], SystemPolicyItem)


if __name__ == "__main__":
    unittest.main()
