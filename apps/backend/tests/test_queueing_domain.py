"""Contract data of PAW-033 that needs no database: enums, presets, value objects,
argument validation and errors. These tests pass against the stubs (the data and
the validation are implemented, not stubbed)."""

import math
import unittest
import uuid
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.tasks import (
    TaskCommand,
    TaskError,
    TaskState,
    WaitReason,
    plan_transition,
)
from paw_backend.tasks.queueing import (
    ACTION_TASK_COMMANDS,
    ACTIVE_QUEUE_STATUSES,
    DEFAULT_LOOP_POLICY,
    PRESET_LIMITS,
    PRIORITY_RANKS,
    RECORDABLE_KINDS,
    BudgetKind,
    BudgetNotConfiguredError,
    BudgetPreset,
    BudgetStatus,
    BudgetUsage,
    BudgetVerdict,
    Decision,
    DecisionReason,
    FailureRecord,
    InvalidQueueingArgumentError,
    LeaseLostError,
    LoopAssessment,
    LoopPolicy,
    LoopVerdict,
    NextAction,
    Priority,
    QueueingError,
    QueueStatus,
    TaskAlreadyQueuedError,
    limit_for,
)
from paw_backend.tasks.queueing import validation as v

SIGNATURE = "a" * 64


class PriorityTest(unittest.TestCase):
    def test_there_are_exactly_three_priorities_in_start_order(self):
        self.assertEqual([p.value for p in Priority], ["high", "normal", "low"])

    def test_rank_is_smaller_for_a_more_urgent_priority(self):
        self.assertEqual(
            [Priority.HIGH.rank, Priority.NORMAL.rank, Priority.LOW.rank], [0, 1, 2]
        )
        self.assertEqual(dict(PRIORITY_RANKS), {"high": 0, "normal": 1, "low": 2})

    def test_the_rank_table_cannot_be_modified(self):
        with self.assertRaises(TypeError):
            PRIORITY_RANKS[Priority.LOW] = -1  # type: ignore[index]

    def test_queue_statuses(self):
        self.assertEqual(
            [s.value for s in QueueStatus],
            ["queued", "claimed", "completed", "cancelled"],
        )
        self.assertEqual(
            ACTIVE_QUEUE_STATUSES, {QueueStatus.QUEUED, QueueStatus.CLAIMED}
        )


class BudgetKindTest(unittest.TestCase):
    def test_the_six_budget_items_in_canonical_order(self):
        self.assertEqual(
            [k.value for k in BudgetKind],
            [
                "runtime_seconds",
                "steps",
                "retries",
                "tool_calls",
                "tokens",
                "gpu_seconds",
            ],
        )

    def test_runtime_is_measured_not_recorded(self):
        self.assertEqual(
            RECORDABLE_KINDS,
            (
                BudgetKind.STEPS,
                BudgetKind.RETRIES,
                BudgetKind.TOOL_CALLS,
                BudgetKind.TOKENS,
                BudgetKind.GPU_SECONDS,
            ),
        )

    def test_the_three_presets(self):
        self.assertEqual(
            [p.value for p in BudgetPreset], ["standard", "long", "unlimited"]
        )


class PresetDataTest(unittest.TestCase):
    def test_every_preset_defines_every_budget_item(self):
        self.assertEqual(set(PRESET_LIMITS), set(BudgetPreset))
        for preset, limits in PRESET_LIMITS.items():
            with self.subTest(preset=preset):
                self.assertEqual(set(limits), set(BudgetKind))

    def test_standard_and_long_have_positive_integer_limits(self):
        for preset in (BudgetPreset.STANDARD, BudgetPreset.LONG):
            for kind in BudgetKind:
                with self.subTest(preset=preset, kind=kind):
                    limit = PRESET_LIMITS[preset][kind]
                    self.assertIs(type(limit), int)
                    self.assertGreater(limit, 0)

    def test_long_allows_more_than_standard_for_every_item(self):
        for kind in BudgetKind:
            with self.subTest(kind=kind):
                self.assertGreater(
                    PRESET_LIMITS[BudgetPreset.LONG][kind],
                    PRESET_LIMITS[BudgetPreset.STANDARD][kind],
                )

    def test_unlimited_removes_every_numeric_limit(self):
        for kind in BudgetKind:
            with self.subTest(kind=kind):
                self.assertIsNone(PRESET_LIMITS[BudgetPreset.UNLIMITED][kind])

    def test_the_presets_are_immutable_data(self):
        with self.assertRaises(TypeError):
            PRESET_LIMITS[BudgetPreset.STANDARD] = {}  # type: ignore[index]
        with self.assertRaises(TypeError):
            PRESET_LIMITS[BudgetPreset.STANDARD][BudgetKind.STEPS] = 1  # type: ignore[index]

    def test_limit_for_reads_the_table(self):
        self.assertEqual(
            limit_for(BudgetPreset.STANDARD, BudgetKind.STEPS),
            PRESET_LIMITS[BudgetPreset.STANDARD][BudgetKind.STEPS],
        )
        self.assertIsNone(limit_for(BudgetPreset.UNLIMITED, BudgetKind.TOKENS))

    def test_limit_for_rejects_strings_instead_of_members(self):
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            limit_for("standard", BudgetKind.STEPS)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.parameter, "preset")
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            limit_for(BudgetPreset.LONG, "steps")  # type: ignore[arg-type]
        self.assertEqual(caught.exception.parameter, "kind")


class BudgetUsageTest(unittest.TestCase):
    def test_remaining_is_the_distance_to_the_limit(self):
        usage = BudgetUsage(BudgetKind.STEPS, consumed=7, limit=10)
        self.assertEqual(usage.remaining, 3)

    def test_remaining_is_zero_at_and_beyond_the_limit(self):
        self.assertEqual(BudgetUsage(BudgetKind.STEPS, 10, 10).remaining, 0)
        self.assertEqual(BudgetUsage(BudgetKind.STEPS, 15, 10).remaining, 0)

    def test_remaining_is_none_when_unlimited(self):
        self.assertIsNone(BudgetUsage(BudgetKind.TOKENS, 5, None).remaining)

    def test_a_zero_limit_is_valid(self):
        self.assertEqual(BudgetUsage(BudgetKind.RETRIES, 0, 0).remaining, 0)

    def test_invalid_values_are_rejected(self):
        bad = [
            dict(kind="steps", consumed=1, limit=1),
            dict(kind=BudgetKind.STEPS, consumed=-1, limit=1),
            dict(kind=BudgetKind.STEPS, consumed=True, limit=1),
            dict(kind=BudgetKind.STEPS, consumed=1.0, limit=1),
            dict(kind=BudgetKind.STEPS, consumed=1, limit=-1),
            dict(kind=BudgetKind.STEPS, consumed=1, limit=False),
            dict(kind=BudgetKind.STEPS, consumed=1, limit=2.5),
        ]
        for arguments in bad:
            with (
                self.subTest(**arguments),
                self.assertRaises(InvalidQueueingArgumentError),
            ):
                BudgetUsage(**arguments)


def usages(**consumed: int) -> tuple[BudgetUsage, ...]:
    return tuple(
        BudgetUsage(kind, consumed.get(kind.value, 0), 100) for kind in BudgetKind
    )


class BudgetVerdictTest(unittest.TestCase):
    def test_an_ok_verdict(self):
        verdict = BudgetVerdict(BudgetStatus.OK, (), usages())
        self.assertEqual(verdict.exceeded, ())
        self.assertEqual(verdict.usage_of(BudgetKind.TOKENS).limit, 100)

    def test_an_exceeded_verdict_names_the_kinds(self):
        verdict = BudgetVerdict(
            BudgetStatus.EXCEEDED, (BudgetKind.STEPS, BudgetKind.TOKENS), usages()
        )
        self.assertEqual(verdict.exceeded, (BudgetKind.STEPS, BudgetKind.TOKENS))

    def test_the_status_must_agree_with_the_exceeded_kinds(self):
        with self.assertRaises(InvalidQueueingArgumentError):
            BudgetVerdict(BudgetStatus.OK, (BudgetKind.STEPS,), usages())
        with self.assertRaises(InvalidQueueingArgumentError):
            BudgetVerdict(BudgetStatus.EXCEEDED, (), usages())

    def test_exceeded_kinds_must_be_in_canonical_order_without_duplicates(self):
        for exceeded in (
            (BudgetKind.TOKENS, BudgetKind.STEPS),
            (BudgetKind.STEPS, BudgetKind.STEPS),
            ("steps",),
        ):
            with (
                self.subTest(exceeded=exceeded),
                self.assertRaises(InvalidQueueingArgumentError),
            ):
                BudgetVerdict(BudgetStatus.EXCEEDED, exceeded, usages())

    def test_usage_must_list_every_kind_once_in_canonical_order(self):
        with self.assertRaises(InvalidQueueingArgumentError):
            BudgetVerdict(BudgetStatus.OK, (), usages()[:-1])
        with self.assertRaises(InvalidQueueingArgumentError):
            BudgetVerdict(BudgetStatus.OK, (), tuple(reversed(usages())))
        with self.assertRaises(InvalidQueueingArgumentError):
            BudgetVerdict(BudgetStatus.OK, (), [*usages()])  # type: ignore[arg-type]


class LoopValueObjectTest(unittest.TestCase):
    def test_a_failure_record_needs_a_lowercase_sha256_hex_and_an_approach(self):
        record = FailureRecord(SIGNATURE, 0)
        self.assertEqual((record.signature, record.approach), (SIGNATURE, 0))
        self.assertEqual(FailureRecord("0123456789abcdef" * 4, 100).approach, 100)

    def test_invalid_failure_records_are_rejected(self):
        bad = [
            ("A" * 64, 0),  # uppercase
            ("a" * 63, 0),
            ("a" * 65, 0),
            ("g" * 64, 0),
            (SIGNATURE + "\n", 0),
            (None, 0),
            (SIGNATURE, -1),
            (SIGNATURE, 101),
            (SIGNATURE, True),
            (SIGNATURE, 1.0),
        ]
        for signature, approach in bad:
            with self.subTest(signature=signature, approach=approach):
                with self.assertRaises(InvalidQueueingArgumentError):
                    FailureRecord(signature, approach)

    def test_the_default_loop_policy_thresholds(self):
        self.assertEqual(
            (
                DEFAULT_LOOP_POLICY.repeat_threshold,
                DEFAULT_LOOP_POLICY.window_size,
                DEFAULT_LOOP_POLICY.max_alternatives,
            ),
            (3, 10, 1),
        )

    def test_policy_bounds(self):
        LoopPolicy(repeat_threshold=2, window_size=2, max_alternatives=0)
        for arguments in (
            dict(repeat_threshold=1),
            dict(repeat_threshold=0),
            dict(repeat_threshold=True),
            dict(repeat_threshold=3.0),
            dict(window_size=1),
            dict(repeat_threshold=5, window_size=4),
            dict(max_alternatives=-1),
            dict(max_alternatives=101),
        ):
            with (
                self.subTest(**arguments),
                self.assertRaises(InvalidQueueingArgumentError),
            ):
                LoopPolicy(**arguments)

    def test_an_assessment_validates_its_parts(self):
        LoopAssessment(LoopVerdict.CONTINUE, None, None, 0)
        LoopAssessment(LoopVerdict.ESCALATE, SIGNATURE, 1, 3)
        for arguments in (
            (LoopVerdict.CONTINUE, None, None, -1),
            ("continue", None, None, 0),
            (LoopVerdict.CONTINUE, "short", None, 0),
            (LoopVerdict.CONTINUE, None, -1, 0),
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(InvalidQueueingArgumentError),
            ):
                LoopAssessment(*arguments)

    def test_the_loop_verdicts(self):
        self.assertEqual(
            [x.value for x in LoopVerdict], ["continue", "try_alternative", "escalate"]
        )


class DecisionDataTest(unittest.TestCase):
    def test_next_actions(self):
        self.assertEqual(
            [a.value for a in NextAction],
            ["continue", "try_alternative", "escalate_agent", "wait_for_user", "fail"],
        )
        self.assertEqual(
            [r.value for r in DecisionReason],
            [
                "none",
                "budget_exceeded",
                "loop_try_alternative",
                "loop_escalate",
                "loop_escalation_unavailable",
            ],
        )

    def test_decision_validates_its_parts(self):
        Decision(NextAction.CONTINUE, DecisionReason.NONE, (), LoopVerdict.CONTINUE)
        with self.assertRaises(InvalidQueueingArgumentError):
            Decision("continue", DecisionReason.NONE, (), LoopVerdict.CONTINUE)
        with self.assertRaises(InvalidQueueingArgumentError):
            Decision(NextAction.CONTINUE, DecisionReason.NONE, [], LoopVerdict.CONTINUE)

    def test_only_waiting_and_failing_change_the_task_state(self):
        self.assertEqual(set(ACTION_TASK_COMMANDS), set(NextAction))
        self.assertIsNone(ACTION_TASK_COMMANDS[NextAction.CONTINUE])
        self.assertIsNone(ACTION_TASK_COMMANDS[NextAction.TRY_ALTERNATIVE])
        self.assertIsNone(ACTION_TASK_COMMANDS[NextAction.ESCALATE_AGENT])
        self.assertEqual(
            ACTION_TASK_COMMANDS[NextAction.WAIT_FOR_USER],
            (TaskCommand.WAIT, WaitReason.USER),
        )
        self.assertEqual(
            ACTION_TASK_COMMANDS[NextAction.FAIL], (TaskCommand.FAIL, None)
        )

    def test_the_mapped_commands_are_legal_for_a_running_task(self):
        for action in (NextAction.WAIT_FOR_USER, NextAction.FAIL):
            command, wait_reason = ACTION_TASK_COMMANDS[action]
            with self.subTest(action=action):
                plan = plan_transition(
                    TaskState.RUNNING, command, wait_reason=wait_reason
                )
                self.assertEqual(
                    plan.target,
                    TaskState.WAITING
                    if action is NextAction.WAIT_FOR_USER
                    else TaskState.FAILED,
                )


class ValidationTest(unittest.TestCase):
    def assertRejects(self, parameter, function, *args, **kwargs):
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.parameter, parameter)
        return caught.exception

    def test_amounts_accept_only_non_negative_integers_up_to_the_cap(self):
        for good in (0, 1, 10**12):
            self.assertEqual(v.check_amount("amount", good), good)
        for bad in (
            -1,
            10**12 + 1,
            True,
            False,
            1.0,
            0.5,
            math.nan,
            math.inf,
            -math.inf,
            "5",
            None,
            b"5",
            [5],
        ):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("amount", v.check_amount, "amount", bad)

    def test_entry_ids(self):
        self.assertEqual(v.check_entry_id(1), 1)
        self.assertEqual(v.check_entry_id(2**63 - 1), 2**63 - 1)
        for bad in (0, -1, 2**63, True, "1", 1.0, None):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("entry_id", v.check_entry_id, bad)

    def test_approach(self):
        self.assertEqual(v.check_approach(0), 0)
        self.assertEqual(v.check_approach(100), 100)
        for bad in (-1, 101, True, "0", 0.0, None):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("approach", v.check_approach, bad)

    def test_uuids_are_not_parsed_from_strings(self):
        value = uuid.uuid4()
        self.assertIs(v.check_uuid("task_id", value), value)
        self.assertRejects("task_id", v.check_uuid, "task_id", str(value))
        self.assertRejects("task_id", v.check_uuid, "task_id", None)
        self.assertRejects("task_id", v.check_uuid, "task_id", value.int)

    def test_a_point_in_time_must_be_timezone_aware(self):
        aware = datetime(2030, 1, 1, tzinfo=UTC)
        self.assertIs(v.check_now("now", aware), aware)
        tokyo = datetime(2030, 1, 1, tzinfo=timezone(timedelta(hours=9)))
        self.assertIs(v.check_now("now", tokyo), tokyo)
        for bad in (datetime(2030, 1, 1), "2030-01-01T00:00:00Z", 1.0, None):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("now", v.check_now, "now", bad)

    def test_worker_ids(self):
        for good in ("w", "worker-1", "gpu-box.local:8000", "A" * 100, "a@b/c_d"):
            self.assertEqual(v.check_worker_id(good), good)
        for bad in (
            "",
            " ",
            "-lead",
            ".lead",
            "a b",
            "a\n",
            "\nabc",
            "x" * 101,
            "ワーカー",
            "wörker",
            None,
            5,
            b"w",
        ):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("worker_id", v.check_worker_id, bad)

    def test_labels_are_non_blank_bounded_and_free_of_control_characters(self):
        self.assertEqual(
            v.check_error_class("builtins.ValueError"), "builtins.ValueError"
        )
        self.assertEqual(v.check_step_name("run tests"), "run tests")
        self.assertEqual(v.check_error_class("E" * 200), "E" * 200)
        self.assertEqual(v.check_step_name("s" * 100), "s" * 100)
        for bad in ("", "  ", "a\x00b", "a\nb", "a\x1fb", "a\x7fb", "E" * 201, None, 3):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("error_class", v.check_error_class, bad)
        for bad in ("", "\t", "s" * 101, "a\rb", None, 3):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("step", v.check_step_name, bad)

    def test_a_message_is_any_string(self):
        for good in ("", "multi\nline\ttext", "x" * 100_000):
            self.assertEqual(v.check_message(good), good)
        for bad in (None, 5, b"x", ["x"]):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("message", v.check_message, bad)

    def test_text_that_utf8_cannot_encode_is_rejected_everywhere(self):
        # A lone surrogate code point (for example from the JSON text "\\ud800") is
        # a valid Python str, but ``str.encode("utf-8")`` raises on it.
        for bad in (
            "\ud800",
            "\udfff",
            "a\ud800",
            "\udc00b",
            "ab\udbffcd",
            "\ud83d\ude00",  # a surrogate PAIR written as two str characters
            "x" * 5000 + "\ud800",  # beyond the 2000 characters that are used
        ):
            with self.subTest(bad=ascii(bad[-3:])):
                self.assertRejects("message", v.check_message, bad)
                self.assertRejects("error_class", v.check_error_class, bad[-50:])
                self.assertRejects("step", v.check_step_name, bad[-50:])
        # The neighbours of the surrogate range and astral characters are text.
        for good in ("\ud7ff", "\ue000", "\U0001f600", "caf\u00e9"):
            with self.subTest(good=ascii(good)):
                self.assertEqual(v.check_message(good), good)
                self.assertEqual(v.check_error_class(good), good)
                self.assertEqual(v.check_step_name(good), good)

    def test_a_message_may_contain_nul_because_it_is_only_hashed(self):
        self.assertEqual(v.check_message("a\x00b"), "a\x00b")

    def test_members_must_be_enum_members_not_their_values(self):
        self.assertIs(
            v.check_member("kind", BudgetKind.STEPS, BudgetKind), BudgetKind.STEPS
        )
        self.assertRejects("kind", v.check_member, "kind", "steps", BudgetKind)
        self.assertRejects("kind", v.check_member, "kind", Priority.HIGH, BudgetKind)

    def test_signatures(self):
        self.assertEqual(v.check_signature(SIGNATURE), SIGNATURE)
        for bad in ("", "A" * 64, "a" * 65, None, 5):
            with self.subTest(bad=repr(bad)):
                self.assertRejects("signature", v.check_signature, bad)


class ErrorTest(unittest.TestCase):
    def test_every_error_is_a_task_error_with_its_own_code(self):
        errors = [
            QueueingError(),
            InvalidQueueingArgumentError("worker_id"),
            TaskAlreadyQueuedError(),
            LeaseLostError(),
            BudgetNotConfiguredError(),
        ]
        for error in errors:
            self.assertIsInstance(error, TaskError)
            self.assertIsInstance(error, QueueingError)
        self.assertEqual(
            [e.code for e in errors],
            [
                "queueing_error",
                "invalid_queueing_argument",
                "task_already_queued",
                "queue_lease_lost",
                "budget_not_configured",
            ],
        )

    def test_messages_are_fixed_and_the_parameter_is_the_only_variable_part(self):
        self.assertEqual(
            str(InvalidQueueingArgumentError("now")), "Invalid value for now"
        )
        self.assertEqual(
            str(TaskAlreadyQueuedError()), "Task already has an active queue entry"
        )
        self.assertEqual(
            str(LeaseLostError()), "Queue entry is not leased to this worker"
        )
        self.assertEqual(
            str(BudgetNotConfiguredError()), "No budget preset is set for this task"
        )

    def test_a_validation_error_never_contains_the_rejected_value(self):
        secret = "sk-secret-value-123"
        with self.assertRaises(InvalidQueueingArgumentError) as caught:
            v.check_worker_id(secret + " with space")
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))
        self.assertEqual(caught.exception.args, ("Invalid value for worker_id",))


if __name__ == "__main__":
    unittest.main()
