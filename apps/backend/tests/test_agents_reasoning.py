"""The reasoning-history seam (Decision 0083, section 3) and its policies (#200).

``KeepAllReasoning`` keeps every message, the reasoning included (the
benchmark's behaviour). ``DropOlderReasoning`` (Decision 0076, 1; the main
model's default) drops the reasoning of all but the last two steps once the
request's prompt estimate passes 32,768 tokens. Neither changes or shares the
runtime's own history.
"""

import copy
import unittest

from paw_backend.agents import (
    MAIN_REASONING_POLICY,
    DropOlderReasoning,
    KeepAllReasoning,
    LocalModelSetup,
    ReasoningHistoryPolicy,
    Usage,
    check_reasoning_policy,
    estimate_prompt_tokens,
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

        class NoEstimate:
            def prepare(self, messages):
                return list(messages)

        check_reasoning_policy(DropOlderReasoning())
        for policy in (object(), Async(), NoArguments(), NoEstimate(), None):
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


def _step(n: int, *, reasoning: str | None = None, field: str = "reasoning_content"):
    """An assistant step with a tool call and the tool's result."""
    assistant = {
        "role": "assistant",
        "content": f"step {n}",
        "tool_calls": [
            {
                "id": f"c{n}",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "ls"}'},
            }
        ],
    }
    if reasoning is not None:
        assistant[field] = reasoning
    return [assistant, {"role": "tool", "tool_call_id": f"c{n}", "content": f"r{n}"}]


def _conversation(steps: int) -> list[dict]:
    history = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the bug."},
    ]
    for n in range(1, steps + 1):
        history += _step(n, reasoning=f"thought {n}")
    return history


def _reasoning_of(messages) -> list[str | None]:
    return [
        m.get("reasoning_content") for m in messages if m.get("role") == "assistant"
    ]


class DropOlderTest(unittest.TestCase):
    def test_the_main_default_is_decision_0076(self):
        policy = MAIN_REASONING_POLICY
        self.assertIsInstance(policy, DropOlderReasoning)
        self.assertIsInstance(policy, ReasoningHistoryPolicy)
        self.assertEqual((policy.keep_last, policy.min_prompt_tokens), (2, 32_768))
        self.assertEqual(policy, DropOlderReasoning())
        self.assertEqual(
            repr(policy), "DropOlderReasoning(keep_last=2, min_prompt_tokens=32768)"
        )
        check_reasoning_policy(policy)

    def test_nothing_is_dropped_up_to_the_threshold(self):
        history = _conversation(5)
        policy = DropOlderReasoning()
        for prompt_tokens in (None, 0, 1_000, 32_768):
            with self.subTest(prompt_tokens):
                prepared = policy.prepare(history, prompt_tokens=prompt_tokens)
                self.assertEqual(prepared, history)

    def test_past_the_threshold_only_the_last_two_keep_their_reasoning(self):
        history = _conversation(5)
        prepared = DropOlderReasoning().prepare(history, prompt_tokens=32_769)
        self.assertEqual(
            _reasoning_of(prepared), [None, None, None, "thought 4", "thought 5"]
        )
        # Only the reasoning goes: the text, the tool calls and the tool results
        # stay, in the same order.
        self.assertEqual(
            prepared,
            [
                {k: v for k, v in m.items() if k != "reasoning_content"}
                if m.get("reasoning_content") in ("thought 1", "thought 2", "thought 3")
                else m
                for m in history
            ],
        )

    def test_both_names_of_the_reasoning_field_are_dropped(self):
        history = [
            {"role": "user", "content": "go"},
            *_step(1, reasoning="old", field="reasoning"),
            {
                "role": "assistant",
                "content": "x",
                "reasoning": "older",
                "reasoning_content": "older",
            },
            *_step(3, reasoning="new"),
        ]
        prepared = DropOlderReasoning(keep_last=1).prepare(
            history, prompt_tokens=100_000
        )
        assistants = [m for m in prepared if m["role"] == "assistant"]
        for message in assistants[:-1]:
            self.assertNotIn("reasoning", message)
            self.assertNotIn("reasoning_content", message)
        self.assertEqual(assistants[-1]["reasoning_content"], "new")

    def test_the_last_steps_are_counted_among_all_assistant_messages(self):
        # As the benchmark's harness: an answer without reasoning still counts
        # as one of the last ``keep_last`` steps.
        history = [
            {"role": "user", "content": "go"},
            *_step(1, reasoning="a"),
            *_step(2, reasoning="b"),
            *_step(3),
        ]
        prepared = DropOlderReasoning().prepare(history, prompt_tokens=40_000)
        self.assertEqual(_reasoning_of(prepared), [None, "b", None])

    def test_keep_last_zero_drops_every_reasoning(self):
        prepared = DropOlderReasoning(keep_last=0, min_prompt_tokens=0).prepare(
            _conversation(3), prompt_tokens=1
        )
        self.assertEqual(_reasoning_of(prepared), [None, None, None])

    def test_the_input_is_never_changed_or_shared(self):
        history = _conversation(4)
        snapshot = copy.deepcopy(history)
        prepared = DropOlderReasoning().prepare(history, prompt_tokens=50_000)
        prepared[-2]["tool_calls"][0]["function"]["name"] = "changed"
        prepared[-2]["reasoning_content"] = "changed"
        self.assertEqual(history, snapshot)

    def test_a_dropped_reasoning_never_comes_back(self):
        # The runtime goes on from what ``prepare`` returns (Decision 0076, 1):
        # once dropped, a step's reasoning is gone even when the next estimate
        # is under the threshold again (it is smaller without that reasoning).
        policy = DropOlderReasoning()
        history = policy.prepare(_conversation(4), prompt_tokens=33_000)
        self.assertEqual(_reasoning_of(history), [None, None, "thought 3", "thought 4"])
        history += _step(5, reasoning="thought 5")
        history = policy.prepare(history, prompt_tokens=30_000)
        self.assertEqual(
            _reasoning_of(history), [None, None, "thought 3", "thought 4", "thought 5"]
        )
        history = policy.prepare(history, prompt_tokens=34_000)
        self.assertEqual(
            _reasoning_of(history), [None, None, None, "thought 4", "thought 5"]
        )

    def test_it_logs_nothing(self):
        with self.assertNoLogs(level="DEBUG"):
            DropOlderReasoning().prepare(_conversation(5), prompt_tokens=90_000)

    def test_its_settings_are_checked(self):
        for kwargs in (
            {"keep_last": -1},
            {"keep_last": True},
            {"keep_last": 2.0},
            {"min_prompt_tokens": -1},
            {"min_prompt_tokens": False},
            {"min_prompt_tokens": "32768"},
        ):
            with self.subTest(kwargs), self.assertRaises((TypeError, ValueError)):
                DropOlderReasoning(**kwargs)

    def test_a_wrong_estimate_is_refused(self):
        policy = DropOlderReasoning()
        for prompt_tokens in (-1, True, 1.5, "40000"):
            with (
                self.subTest(prompt_tokens),
                self.assertRaises((TypeError, ValueError)),
            ):
                policy.prepare(_conversation(3), prompt_tokens=prompt_tokens)


class EstimateTest(unittest.TestCase):
    def test_the_estimate_is_the_last_usage_and_the_new_tool_bytes_over_3(self):
        usage = Usage(prompt_tokens=30_000, completion_tokens=1_000)
        self.assertEqual(estimate_prompt_tokens(usage, []), 31_000)
        # UTF-8 bytes, not characters: "é" is 2 bytes, "日" is 3.
        self.assertEqual(
            estimate_prompt_tokens(usage, ["abcdef", "é", "日"]), 31_000 + 11 // 3
        )
        self.assertEqual(estimate_prompt_tokens(usage, ["a" * 3_000]), 32_000)

    def test_without_a_previous_answer_there_is_no_estimate(self):
        # The first request of an attempt: nothing to drop yet (the harness).
        self.assertIsNone(estimate_prompt_tokens(None, ["x" * 999_999]))

    def test_the_policies_take_the_estimate(self):
        usage = Usage(prompt_tokens=32_000, completion_tokens=700)
        history = _conversation(4)
        under = estimate_prompt_tokens(usage, ["a" * 200])
        over = estimate_prompt_tokens(usage, ["a" * 300])
        self.assertEqual((under, over), (32_766, 32_800))
        policy = DropOlderReasoning()
        self.assertEqual(policy.prepare(history, prompt_tokens=under), history)
        self.assertEqual(
            _reasoning_of(policy.prepare(history, prompt_tokens=over)),
            [None, None, "thought 3", "thought 4"],
        )
        self.assertEqual(
            KeepAllReasoning().prepare(history, prompt_tokens=over), history
        )


class SetupTest(unittest.TestCase):
    """The main model's local agent takes its policy from ``LocalModelSetup``
    (the deployment's settings, given to ``create_app``)."""

    def test_the_main_model_drops_the_older_reasoning_by_default(self):
        setup = LocalModelSetup("http://127.0.0.1:8000/v1")
        self.assertIs(setup.reasoning_history, MAIN_REASONING_POLICY)
        self.assertIn(
            "reasoning_history=DropOlderReasoning(keep_last=2, "
            "min_prompt_tokens=32768)",
            repr(setup),
        )

    def test_keep_all_stays_selectable(self):
        setup = LocalModelSetup(
            "http://127.0.0.1:8000/v1", reasoning_history=KeepAllReasoning()
        )
        self.assertIsInstance(setup.reasoning_history, KeepAllReasoning)
        other = LocalModelSetup(
            "http://127.0.0.1:8000/v1",
            reasoning_history=DropOlderReasoning(keep_last=4),
        )
        self.assertEqual(other.reasoning_history.keep_last, 4)

    def test_a_wrong_policy_is_refused_at_start(self):
        class OneArgument:
            def prepare(self, messages):
                return list(messages)

        for policy in (None, object(), OneArgument()):
            with self.subTest(type(policy).__name__), self.assertRaises(TypeError):
                LocalModelSetup("http://127.0.0.1:8000/v1", reasoning_history=policy)
