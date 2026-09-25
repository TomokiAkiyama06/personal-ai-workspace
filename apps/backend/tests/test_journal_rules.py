"""The consolidator's decisions, as tables (no database): ``rules.py``.

Every branch of ``plan_item``, the order of two events, the confirmation state that
is stored, the high-risk screen and the retry delay.
"""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from paw_backend.memory.journal import (
    Backoff,
    InvalidJournalInputError,
    ItemResult,
    WorkerMemory,
    WorkerScope,
    WorkerState,
    limits,
)
from paw_backend.memory.journal.rules import (
    CurrentMemory,
    OrderKey,
    is_high_risk,
    is_newer,
    plan_item,
    stored_state,
)
from paw_backend.memory.models import ConfirmationState, MemoryStatus

T0 = datetime(2030, 1, 1, tzinfo=UTC)
CONVERSATION_A = UUID(int=1)
CONVERSATION_B = UUID(int=2)


def order(sequence=5, *, conversation=CONVERSATION_A, seconds=0) -> OrderKey:
    return OrderKey(conversation, sequence, T0 + timedelta(seconds=seconds))


def worker_memory(**fields) -> WorkerMemory:
    values = {
        "key": "indent_style",
        "scope": WorkerScope.USER,
        "state": WorkerState.INFERRED,
        "supersedes": None,
        "content": "Use tabs.",
        "conflicts_with": (),
    }
    values.update(fields)
    return WorkerMemory(**values)


def current(
    content="Use spaces.",
    status=MemoryStatus.ACTIVE,
    state=ConfirmationState.INFERRED,
) -> CurrentMemory:
    return CurrentMemory(status, state, content)


class BackoffTest(unittest.TestCase):
    def test_the_delay_doubles_from_thirty_seconds_up_to_fifteen_minutes(self):
        self.assertEqual(
            [Backoff().delay_seconds(n) for n in range(1, 9)],
            [30, 60, 120, 240, 480, 900, 900, 900],
        )

    def test_a_factor_of_one_is_a_constant_delay(self):
        backoff = Backoff(base_seconds=45, factor=1, max_seconds=100)
        self.assertEqual({backoff.delay_seconds(n) for n in (1, 2, 50)}, {45})

    def test_a_huge_failure_count_is_answered_at_once_with_the_cap(self):
        self.assertEqual(Backoff().delay_seconds(limits.MAX_CLAIM_COUNT), 900)
        self.assertEqual(
            Backoff(base_seconds=1, factor=10, max_seconds=86_400).delay_seconds(
                limits.MAX_CLAIM_COUNT
            ),
            86_400,
        )

    def test_the_cap_may_equal_the_base(self):
        self.assertEqual(Backoff(base_seconds=60, max_seconds=60).delay_seconds(9), 60)

    def test_bad_settings_are_refused(self):
        bad = [
            {"base_seconds": 0},
            {"base_seconds": -1},
            {"base_seconds": True},
            {"base_seconds": 1.5},
            {"base_seconds": limits.MAX_BACKOFF_SECONDS + 1},
            {"factor": 0},
            {"factor": limits.MAX_BACKOFF_FACTOR + 1},
            {"factor": "2"},
            {"max_seconds": 0},
            {"max_seconds": 29},  # below the base (30)
            {"max_seconds": limits.MAX_BACKOFF_SECONDS + 1},
        ]
        for settings in bad:
            with self.subTest(settings):
                with self.assertRaises(InvalidJournalInputError):
                    Backoff(**settings)

    def test_the_failure_count_is_checked(self):
        for failures in (0, -1, True, 1.0, "1", None, limits.MAX_CLAIM_COUNT + 1):
            with self.subTest(failures):
                with self.assertRaises(InvalidJournalInputError):
                    Backoff().delay_seconds(failures)


class OrderTest(unittest.TestCase):
    def test_in_one_conversation_the_event_sequence_decides_not_the_time(self):
        cases = [
            # entry, applied, newer?
            (order(5), order(4), True),
            (order(5), order(5), False),  # never newer than itself
            (order(4), order(5), False),
            # The clock says the opposite: the sequence still wins.
            (order(5, seconds=-100), order(4, seconds=100), True),
            (order(4, seconds=100), order(5, seconds=-100), False),
            (order(0), order(0, seconds=50), False),
        ]
        for entry, applied, expected in cases:
            with self.subTest(
                entry=entry.event_sequence, applied=applied.event_sequence
            ):
                self.assertIs(is_newer(entry, applied), expected)

    def test_across_conversations_the_recorded_time_decides(self):
        newer = order(0, conversation=CONVERSATION_A, seconds=10)
        older = order(99, conversation=CONVERSATION_B, seconds=5)
        # The sequence numbers of different conversations mean nothing to each other.
        self.assertTrue(is_newer(newer, older))
        self.assertFalse(is_newer(older, newer))

    def test_an_exact_tie_across_conversations_is_broken_by_the_conversation_id(self):
        a = order(3, conversation=CONVERSATION_A, seconds=7)
        b = order(3, conversation=CONVERSATION_B, seconds=7)
        self.assertNotEqual(is_newer(a, b), is_newer(b, a))  # exactly one is newer
        self.assertTrue(is_newer(b, a))  # the larger id


class StoredStateTest(unittest.TestCase):
    def test_a_worker_can_never_mint_a_confirmed_memory(self):
        self.assertEqual(stored_state(WorkerState.INFERRED), ConfirmationState.INFERRED)
        self.assertEqual(
            stored_state(WorkerState.CONFIRMED), ConfirmationState.OBSERVED
        )
        for state in WorkerState:
            self.assertNotEqual(stored_state(state), ConfirmationState.CONFIRMED)
            self.assertNotEqual(stored_state(state), ConfirmationState.REJECTED)


class HighRiskTest(unittest.TestCase):
    def test_the_keys_of_the_requirements_examples_are_held(self):
        for key in (
            "subscription_review_before_merge",  # the requirements' own example
            "confirm_before_main_merge",
            "auto-merge",
            "MergePolicy",  # split at the capital
            "allow.delete",
        ):
            with self.subTest(key):
                self.assertTrue(is_high_risk(key))

    def test_each_area_by_text(self):
        held = [
            "Always merge pull requests to main.",
            "You may delete old branches.",
            "This is a destructive operation.",
            "Give the intern admin permission.",
            "Change the ACL of the repo.",
            "Publish the release notes.",
            "Make the project public.",
            "My API token is stored in the file.",
            "The password is in the vault.",
            "Send the report to the customer.",
            "Upload the logs to an external service.",
            "Use sudo when installing.",
            "Force push is fine.",
            "マージ権限を与える",
            "ブランチを削除してよい",
            "公開範囲を広げる",
            "外部に送信する",
            "認証情報を保存する",
            "ＭＥＲＧＥ　ｔｏ　ｍａｉｎ",  # full width
        ]
        for text in held:
            with self.subTest(text):
                self.assertTrue(is_high_risk(text))

    def test_ordinary_preferences_are_not_held(self):
        ordinary = [
            "indent_style",
            "Use tabs for indentation.",
            "Prefers pytest over unittest.",
            "Answers in Japanese.",
            "The keyboard layout is Dvorak.",
            "emerge is Gentoo's package manager.",  # 'emerge' is not 'merge'
            "The tokenizer is fast.",  # 'tokenizer' is not 'token'
            "Enforce line length 88.",  # 'enforce' is not 'force'
            "日本語で回答する",
            "",
        ]
        for text in ordinary:
            with self.subTest(text):
                self.assertFalse(is_high_risk(text))

    def test_none_and_empty_texts_are_skipped_and_any_text_can_hit(self):
        self.assertFalse(is_high_risk())
        self.assertFalse(is_high_risk(None, ""))
        self.assertTrue(is_high_risk("fine", None, "delete it"))


class PlanItemTest(unittest.TestCase):
    """One case per rule of ``plan_item``, in the order of its docstring."""

    def plan(self, item=None, *, entry=None, cur=None, applied=None, target=None):
        return plan_item(
            item or worker_memory(),
            entry=entry or order(5),
            current=cur,
            applied=applied,
            supersedes_target=target,
        )

    def test_the_rules(self):
        R, C, W = ItemResult, current, worker_memory
        confirmed = C(state=ConfirmationState.CONFIRMED)
        old_confirmed = C(
            status=MemoryStatus.SUPERSEDED, state=ConfirmationState.CONFIRMED
        )
        cases = [
            # name, plan arguments, expected
            ("shared scope", dict(item=W(scope=WorkerScope.SHARED)), R.REFUSED_SHARED),
            ("no content", dict(item=W(content=None)), R.NO_CONTENT),
            ("high-risk key", dict(item=W(key="allow_merge")), R.HELD_HIGH_RISK),
            (
                "high-risk content",
                dict(item=W(content="Delete files.")),
                R.HELD_HIGH_RISK,
            ),
            ("a new key", dict(), R.CREATED),
            (
                "a new key superseding a confirmed one",
                dict(target=confirmed),
                R.HELD_CONFIRMED,
            ),
            ("a new key superseding a weaker one", dict(target=C()), R.CREATED),
            (
                "a new key superseding an old confirmed one",
                dict(target=old_confirmed),
                R.CREATED,
            ),
            (
                "deprecated by the user",
                dict(cur=C(status=MemoryStatus.DEPRECATED)),
                R.BLOCKED,
            ),
            ("history", dict(cur=C(status=MemoryStatus.HISTORY)), R.BLOCKED),
            ("rejected", dict(cur=C(state=ConfirmationState.REJECTED)), R.BLOCKED),
            ("an older turn", dict(cur=C(), applied=order(6)), R.STALE),
            ("the same turn", dict(cur=C(), applied=order(5)), R.STALE),
            ("a newer turn", dict(cur=C(), applied=order(4)), R.UPDATED),
            ("the same content", dict(cur=C(content="Use tabs.")), R.DUPLICATE),
            (
                "the same content, other whitespace",
                dict(cur=C(content=" Use tabs.\n")),
                R.DUPLICATE,
            ),
            (
                "a confirmed memory, same content",
                dict(cur=C(content="Use tabs.", state=ConfirmationState.CONFIRMED)),
                R.DUPLICATE,
            ),
            (
                "a confirmed memory, other content",
                dict(cur=confirmed),
                R.HELD_CONFIRMED,
            ),
            (
                "an observed memory, other content",
                dict(cur=C(state=ConfirmationState.OBSERVED)),
                R.UPDATED,
            ),
            (
                "a superseded latest version",
                dict(cur=C(status=MemoryStatus.SUPERSEDED)),
                R.UPDATED,
            ),
            ("an old confirmed latest version", dict(cur=old_confirmed), R.UPDATED),
            (
                "an update superseding a confirmed memory",
                dict(cur=C(), target=confirmed),
                R.HELD_CONFIRMED,
            ),
            (
                "an update superseding a weaker memory",
                dict(cur=C(), target=C()),
                R.UPDATED,
            ),
            (
                "project scope is a recommendation",
                dict(item=W(scope=WorkerScope.PROJECT)),
                R.CREATED,
            ),
            (
                "repo scope is a recommendation",
                dict(item=W(scope=WorkerScope.REPO)),
                R.CREATED,
            ),
            (
                "a confirmed claim of the worker",
                dict(item=W(state=WorkerState.CONFIRMED)),
                R.CREATED,
            ),
        ]
        for name, arguments, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.plan(**arguments), expected)

    def test_the_first_matching_rule_wins(self):
        risky_and_shared = worker_memory(scope=WorkerScope.SHARED, key="merge")
        self.assertEqual(self.plan(risky_and_shared), ItemResult.REFUSED_SHARED)
        risky_without_content = worker_memory(key="merge", content=None)
        self.assertEqual(self.plan(risky_without_content), ItemResult.NO_CONTENT)
        # A high-risk item is held even where the key is new AND where it is blocked.
        risky = worker_memory(key="delete_everything")
        for cur in (None, current(status=MemoryStatus.DEPRECATED), current()):
            with self.subTest(cur=cur):
                self.assertEqual(self.plan(risky, cur=cur), ItemResult.HELD_HIGH_RISK)
        # A blocked memory is blocked even if the turn is stale or the content equal.
        deprecated = current(content="Use tabs.", status=MemoryStatus.DEPRECATED)
        self.assertEqual(
            self.plan(cur=deprecated, applied=order(9)), ItemResult.BLOCKED
        )

    def test_only_the_written_results_wrote_memory(self):
        self.assertEqual(
            {result for result in ItemResult if result.wrote_memory},
            {ItemResult.CREATED, ItemResult.UPDATED},
        )

    def test_no_result_of_a_worker_output_can_be_a_confirmed_write(self):
        # The rules give no way to write a confirmed version: the state stored is
        # ``stored_state`` and the only writes are CREATED / UPDATED.
        for state in WorkerState:
            item = worker_memory(state=state)
            result = self.plan(item)
            self.assertEqual(result, ItemResult.CREATED)
            self.assertNotEqual(stored_state(item.state), ConfirmationState.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
