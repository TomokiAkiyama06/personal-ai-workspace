"""The reasoning-history seam (Decision 0083, section 3; #200).

The default keeps every message, the reasoning included (the benchmark's
behaviour), and never changes or shares the runtime's own history.
"""

import unittest

from paw_backend.agents import (
    KeepAllReasoning,
    ReasoningHistoryPolicy,
    check_reasoning_policy,
    reasoning_chars,
)

HISTORY = [
    {"role": "system", "content": "You are a coding agent."},
    {"role": "user", "content": "Fix the bug."},
    {
        "role": "assistant",
        "content": None,
        "reasoning_content": "The bug is in parse(); look first.",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "ls"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
    {"role": "assistant", "content": "Done.", "reasoning_content": "All good."},
]


class KeepAllTest(unittest.TestCase):
    def test_the_history_goes_back_unchanged_with_its_reasoning(self):
        policy = KeepAllReasoning()
        self.assertIsInstance(policy, ReasoningHistoryPolicy)
        prepared = policy.prepare(HISTORY)
        self.assertEqual(prepared, HISTORY)
        self.assertEqual(
            [m.get("reasoning_content") for m in prepared],
            [None, None, "The bug is in parse(); look first.", None, "All good."],
        )

    def test_it_is_a_copy_the_runtime_history_is_never_shared(self):
        history = [dict(m) for m in HISTORY]
        prepared = KeepAllReasoning().prepare(history)
        prepared[2]["tool_calls"][0]["function"]["name"] = "changed"
        prepared[4]["reasoning_content"] = "changed"
        prepared.append({"role": "user", "content": "more"})
        self.assertEqual(history, HISTORY)

    def test_the_repr_says_nothing_of_the_history(self):
        self.assertEqual(repr(KeepAllReasoning()), "KeepAllReasoning()")


class CheckTest(unittest.TestCase):
    def test_a_policy_is_a_plain_callable_of_the_messages(self):
        check_reasoning_policy(KeepAllReasoning())

        class Async:
            async def prepare(self, messages):
                return messages

        class NoArguments:
            def prepare(self):
                return []

        for policy in (object(), Async(), NoArguments(), None):
            with self.subTest(type(policy).__name__), self.assertRaises(TypeError):
                check_reasoning_policy(policy)


class CharsTest(unittest.TestCase):
    def test_only_the_length_of_the_reasoning_is_counted(self):
        self.assertEqual(
            reasoning_chars(HISTORY),
            len("The bug is in parse(); look first.") + len("All good."),
        )
        self.assertEqual(reasoning_chars([{"reasoning_content": 5}]), 0)
        self.assertEqual(reasoning_chars([]), 0)
