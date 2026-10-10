"""The closed failure table of the agent runtimes (Decision 0083, section 4).

Every row is pinned here, one by one: the class recorded, whether it escalates,
and its fixed text. The invariants: every class is one a runtime may report
(``RUNTIME_ERROR_CLASSES``), no row says ``retryable=False``, and only the
cloud-fallback path keeps ``HybridRuntime``'s ``ComputeUnavailable``.
"""

import asyncio
import unittest

from paw_backend.agents import ErrorClassifier, Failure, RuntimeFailure
from paw_backend.agents import failures as failures_module
from paw_backend.agents.failures import (
    COMPUTE_UNAVAILABLE,
    FAILURE_TABLE,
    FALLBACK_ROW,
    GATE_REFUSALS,
)
from paw_backend.compute import runtimes as compute_runtimes
from paw_backend.orchestrator.errors import (
    ADAPTER_ERROR,
    OUT_OF_MEMORY_CLASSES,
    RUNTIME_ERROR_CLASSES,
    InvalidNodeResultError,
    NodeStopped,
    ResultReason,
    StopReason,
    runtime_error_class,
)

F = Failure
# (failure, the class recorded, escalate): Decision 0083's table, row by row.
EXPECTED = [
    (F.OUT_OF_MEMORY, "AgentOutOfMemory", False),
    (F.SERVER_UNAVAILABLE, "ConnectionError", False),
    (F.CLOUD_POLICY_DENIED, "ConnectionError", True),
    (F.CONNECTION_QUOTA_EXCEEDED, "ConnectionError", True),
    (F.CONNECTION_UNAVAILABLE, "ConnectionError", True),
    (F.TOKEN_FLOOR, "ConnectionError", True),
    (F.CONNECTION_BUSY, "ConnectionError", True),
    (F.TIMEOUT, "TimeoutError", False),
    (F.CONTEXT_LIMIT, "RuntimeError", True),
    (F.STEP_LIMIT, "RuntimeError", True),
    (F.NO_TOOL_CALLS, "RuntimeError", True),
    (F.NO_SUBMISSION, "RuntimeError", True),
    (F.MALFORMED_RESPONSE, "ValueError", False),
    (F.RATE_LIMITED, "ConnectionError", False),
    (F.CREDENTIAL_EXPIRED, "ConnectionError", True),
    (F.SANDBOX_UNAVAILABLE, "PermissionError", False),
    (F.INVALID_RESULT, "InvalidNodeResultError", False),
    (F.OTHER, "AdapterError", False),
]


class TableTest(unittest.TestCase):
    def test_every_row_as_the_decision_says(self):
        self.assertEqual({f for f, _, _ in EXPECTED}, set(Failure))
        for failure, error_class, escalate in EXPECTED:
            with self.subTest(failure):
                outcome = ErrorClassifier.outcome(failure)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_class, error_class)
                self.assertIs(outcome.escalate, escalate)
                # Never "not retryable": that would neither retry nor escalate.
                self.assertIs(outcome.retryable, True)
                # The fixed text of the reason, and the class is recorded as is.
                self.assertEqual(outcome.message, failure.value)
                self.assertEqual(runtime_error_class(outcome.error_class), error_class)

    def test_every_class_is_one_a_runtime_may_report(self):
        for failure, row in FAILURE_TABLE.items():
            with self.subTest(failure):
                self.assertIn(row.error_class, RUNTIME_ERROR_CLASSES)
        self.assertIn(
            ErrorClassifier.outcome(F.OUT_OF_MEMORY).error_class, OUT_OF_MEMORY_CLASSES
        )

    def test_the_texts_are_distinct_fixed_codes(self):
        texts = [failure.value for failure in Failure]
        self.assertEqual(len(texts), len(set(texts)))
        for text in texts:
            self.assertRegex(text, r"^[a-z_]+$")

    def test_a_refused_gate_on_the_cloud_fallback_keeps_the_local_rung(self):
        # HybridRuntime's cloud path: "no capacity now", no escalation; the name
        # is HybridRuntime's own, recorded as AdapterError as today.
        self.assertEqual(COMPUTE_UNAVAILABLE, compute_runtimes.COMPUTE_UNAVAILABLE)
        self.assertEqual(FALLBACK_ROW.error_class, COMPUTE_UNAVAILABLE)
        self.assertEqual(
            GATE_REFUSALS,
            {
                F.CLOUD_POLICY_DENIED,
                F.CONNECTION_QUOTA_EXCEEDED,
                F.CONNECTION_UNAVAILABLE,
                F.TOKEN_FLOOR,
                F.CONNECTION_BUSY,
            },
        )
        for refusal in GATE_REFUSALS:
            with self.subTest(refusal):
                outcome = ErrorClassifier.outcome(refusal, fallback=True)
                self.assertEqual(outcome.error_class, COMPUTE_UNAVAILABLE)
                self.assertIs(outcome.escalate, False)
                self.assertIs(outcome.retryable, True)
                self.assertEqual(
                    runtime_error_class(outcome.error_class), ADAPTER_ERROR
                )
        # The other rows do not depend on the path.
        for failure in set(Failure) - GATE_REFUSALS:
            with self.subTest(failure):
                self.assertEqual(
                    ErrorClassifier.row(failure, fallback=True),
                    ErrorClassifier.row(failure),
                )

    def test_an_unknown_failure_is_refused(self):
        with self.assertRaises(ValueError):
            ErrorClassifier.outcome("cuda_oom")
        with self.assertRaises(ValueError):
            RuntimeFailure("whatever")

    def test_the_table_is_checked_when_it_is_imported(self):
        original = dict(failures_module._ROWS)
        try:
            failures_module._ROWS[F.OTHER] = failures_module.FailureRow("MyError")
            with self.assertRaises(RuntimeError):
                failures_module._check_table()
            failures_module._ROWS[F.OTHER] = original[F.OTHER]
            del failures_module._ROWS[F.TIMEOUT]
            with self.assertRaises(RuntimeError):
                failures_module._check_table()
        finally:
            failures_module._ROWS.clear()
            failures_module._ROWS.update(original)
        failures_module._check_table()


class FromErrorTest(unittest.TestCase):
    def test_a_runtime_failure_is_its_row(self):
        outcome = ErrorClassifier.from_error(RuntimeFailure(F.CONTEXT_LIMIT))
        self.assertEqual(
            (outcome.error_class, outcome.escalate, outcome.message),
            ("RuntimeError", True, "context_limit"),
        )
        outcome = ErrorClassifier.from_error(
            InvalidNodeResultError(ResultReason.BAD_TYPE)
        )
        self.assertEqual(outcome.error_class, "InvalidNodeResultError")

    def test_anything_else_is_an_adapter_error_and_its_text_is_not_read(self):
        secret = "sk_" + "live_not_a_real_key"

        class Hostile(Exception):
            pass

        for error in (Hostile(secret), ValueError(secret), OSError(secret)):
            with self.subTest(type(error).__name__):
                outcome = ErrorClassifier.from_error(error)
                self.assertEqual(outcome.error_class, ADAPTER_ERROR)
                self.assertEqual(outcome.message, "adapter_error")
                self.assertNotIn(secret, repr(outcome))

    def test_a_stop_a_cancellation_and_memory_error_pass(self):
        for error in (
            NodeStopped(StopReason.BUDGET_EXCEEDED),
            asyncio.CancelledError(),
            MemoryError(),
        ):
            with self.subTest(type(error).__name__), self.assertRaises(type(error)):
                ErrorClassifier.from_error(error)

    def test_the_failure_message_is_fixed(self):
        error = RuntimeFailure(F.OUT_OF_MEMORY)
        self.assertEqual(str(error), "The agent runtime failed (out_of_memory)")
        self.assertIs(error.failure, F.OUT_OF_MEMORY)
