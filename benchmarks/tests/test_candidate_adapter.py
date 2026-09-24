"""Contract tests for the provider-neutral candidate adapter interface."""

import asyncio
import json
import unittest
from collections.abc import Mapping
from dataclasses import fields
from operator import setitem

from benchmarks.candidate_adapter import (
    AttemptControl,
    CancellationReason,
    CancellationToken,
    CandidateAdapter,
    CandidateErrorCode,
    CandidateIdentity,
    CandidateRequest,
    CandidateResult,
    CandidateStatus,
    ContextLimits,
    PromptConfig,
    RetryPolicy,
    ToolDefinition,
    Usage,
)


def _thaw(value):
    """Convert a frozen JSON snapshot back to plain dict/list values."""

    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class RecordingAdapter(CandidateAdapter):
    def __init__(self, runtime: str):
        self._identity = CandidateIdentity(
            candidate_id=f"candidate-{runtime}", model="example-model", runtime=runtime
        )
        self.calls: list[tuple[CandidateRequest, AttemptControl]] = []

    @property
    def identity(self) -> CandidateIdentity:
        return self._identity

    async def run_attempt(
        self, request: CandidateRequest, control: AttemptControl
    ) -> CandidateResult:
        self.calls.append((request, control))
        if control.cancellation.cancelled:
            return CandidateResult(
                CandidateStatus.CANCELLED,
                error_code=CandidateErrorCode.BACKEND_CANCELLED,
            )
        return CandidateResult(
            CandidateStatus.COMPLETED,
            output="candidate response",
            usage=Usage(input_tokens=10, output_tokens=2),
        )


def example_request() -> CandidateRequest:
    return CandidateRequest(
        task_id="PAW-example",
        prompt=PromptConfig(system="Follow repository policy.", task="Fix the issue."),
        tools=(
            ToolDefinition(
                name="read_file",
                description="Read a repository file.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
        ),
        context_limits=ContextLimits(
            max_context_tokens=32_768, max_output_tokens=4_096
        ),
    )


class CandidateAdapterContractTest(unittest.TestCase):
    def test_local_codex_and_claude_share_one_interface(self):
        request = example_request()
        for runtime in ("local", "codex", "claude"):
            with self.subTest(runtime=runtime):
                adapter: CandidateAdapter = RecordingAdapter(runtime)
                control = AttemptControl(1, 60.0, CancellationToken())
                result = asyncio.run(adapter.run_attempt(request, control))
                self.assertEqual(result.status, CandidateStatus.COMPLETED)
                self.assertEqual(adapter.identity.runtime, runtime)

    def test_prompt_tools_and_context_limits_are_explicit(self):
        request = example_request()
        self.assertEqual(request.prompt.system, "Follow repository policy.")
        self.assertEqual(request.tools[0].input_schema["type"], "object")
        self.assertEqual(request.context_limits.max_context_tokens, 32_768)
        self.assertEqual(request.context_limits.max_output_tokens, 4_096)

    def test_tool_schema_is_a_deep_immutable_snapshot(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        tool = ToolDefinition("read_file", "Read a repository file.", schema)
        schema["properties"]["path"]["type"] = "integer"
        schema["required"].append("recursive")

        self.assertEqual(tool.input_schema["properties"]["path"]["type"], "string")
        self.assertEqual(tool.input_schema["required"], ("path",))
        with self.assertRaises(TypeError):
            tool.input_schema["properties"]["path"]["type"] = "integer"

    def test_reused_request_gives_every_candidate_the_same_tool_schema(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        request = CandidateRequest(
            task_id="PAW-reuse",
            prompt=PromptConfig(system="Follow policy.", task="Fix the issue."),
            tools=(ToolDefinition("read_file", "Read a file.", schema),),
            context_limits=ContextLimits(32_768, 4_096),
        )
        expected = json.dumps(_thaw(request.tools[0].input_schema), sort_keys=True)

        test = self

        class MutatingAdapter(RecordingAdapter):
            async def run_attempt(self, request, control):
                exposed = request.tools[0].input_schema
                for mutate in (
                    lambda: setitem(exposed, "type", "array"),
                    lambda: setitem(exposed["properties"], "extra", {}),
                    lambda: setitem(exposed["properties"]["path"], "type", "x"),
                ):
                    with test.assertRaises(TypeError):
                        mutate()
                return await super().run_attempt(request, control)

        schema["type"] = "array"  # the caller mutates its own dict afterwards
        schema["properties"]["path"]["type"] = "integer"
        for runtime in ("local", "codex", "claude"):
            asyncio.run(
                MutatingAdapter(runtime).run_attempt(
                    request, AttemptControl(1, 60.0, CancellationToken())
                )
            )
            self.assertEqual(
                json.dumps(_thaw(request.tools[0].input_schema), sort_keys=True),
                expected,
            )
        self.assertEqual(request.tools[0].input_schema["type"], "object")

    def test_invalid_complete_tool_schema_is_rejected(self):
        with self.assertRaises(ValueError):
            ToolDefinition("bad", "Bad schema.", {"type": "object", "required": 1})

    def test_tool_schema_must_be_finite_acyclic_json_schema(self):
        circular: dict = {"type": "object"}
        circular["properties"] = circular
        too_deep: dict = {"type": "object"}
        node = too_deep
        for _ in range(5_000):
            node["properties"] = {}
            node = node["properties"]
        invalid = {
            "nan default": {"type": "object", "default": float("nan")},
            "infinite bound": {
                "type": "object",
                "properties": {"n": {"type": "number", "maximum": float("inf")}},
            },
            "non-json value": {"type": "object", "default": object()},
            "unknown type name": {
                "type": "object",
                "properties": {"a": {"type": "strng"}},
            },
            "circular reference": circular,
            "excessive nesting": too_deep,
        }
        for label, schema in invalid.items():
            with (
                self.subTest(label),
                self.assertRaisesRegex(ValueError, "valid JSON-serializable"),
            ):
                ToolDefinition("bad", "Bad schema.", schema)
        with self.assertRaisesRegex(TypeError, "string keys"):
            ToolDefinition("bad", "Bad schema.", {"type": "object", 1: "x"})
        with self.assertRaisesRegex(TypeError, "must be a mapping"):
            ToolDefinition("bad", "Bad schema.", ["type", "object"])  # type: ignore[arg-type]

    def test_backend_controls_retry_attempts(self):
        policy = RetryPolicy(max_attempts=2)
        self.assertTrue(policy.allows_attempt(1))
        self.assertTrue(policy.allows_attempt(2))
        self.assertFalse(policy.allows_attempt(3))

    def test_backend_does_not_retry_non_retryable_or_exhausted_results(self):
        policy = RetryPolicy(max_attempts=2)
        retryable = CandidateResult(
            CandidateStatus.FAILED,
            error_code=CandidateErrorCode.PROVIDER_UNAVAILABLE,
            retryable=True,
        )
        non_retryable = CandidateResult(
            CandidateStatus.FAILED,
            error_code=CandidateErrorCode.INVALID_REQUEST,
        )
        self.assertFalse(policy.should_retry(1, non_retryable))
        self.assertFalse(policy.should_retry(2, retryable))

    def test_backend_controls_timeout_and_cancellation(self):
        token = CancellationToken()
        control = AttemptControl(attempt=2, timeout_seconds=7.5, cancellation=token)
        self.assertEqual(control.timeout_seconds, 7.5)
        self.assertTrue(token.cancel(CancellationReason.USER_REQUESTED))
        self.assertFalse(token.cancel(CancellationReason.POLICY))

        result = asyncio.run(
            RecordingAdapter("local").run_attempt(example_request(), control)
        )
        self.assertEqual(result.status, CandidateStatus.CANCELLED)
        self.assertEqual(token.reason, CancellationReason.USER_REQUESTED)

    def test_invalid_limits_policies_and_tool_schemas_are_rejected(self):
        with self.assertRaises(ValueError):
            ContextLimits(max_context_tokens=100, max_output_tokens=101)
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            ToolDefinition("bad", "Bad schema.", {"type": "array"})
        with self.assertRaises(ValueError):
            AttemptControl(1, float("inf"), CancellationToken())
        with self.assertRaises(TypeError):
            CancellationToken().cancel("may-contain-private-content")  # type: ignore[arg-type]

    def test_duplicate_tool_names_are_rejected(self):
        tool = example_request().tools[0]
        with self.assertRaises(ValueError):
            CandidateRequest(
                task_id="PAW-duplicate-tool",
                prompt=PromptConfig(system="Follow policy.", task="Fix the issue."),
                tools=(tool, tool),
                context_limits=ContextLimits(32_768, 4_096),
            )

    def test_candidate_result_rejects_every_invalid_combination(self):
        failed = CandidateStatus.FAILED
        code = CandidateErrorCode.INTERNAL_ERROR
        cases = (
            # (label, kwargs, exception type, message pattern)
            (
                "status is not an enum member",
                {"status": "completed", "output": "x"},
                TypeError,
                "status must be a CandidateStatus",
            ),
            (
                "retryable is not a bool",
                {"status": failed, "error_code": code, "retryable": 1},
                TypeError,
                "retryable must be a bool",
            ),
            (
                "usage is not Usage",
                {"status": CandidateStatus.COMPLETED, "output": "x", "usage": {}},
                TypeError,
                "usage must be Usage",
            ),
            (
                "completed without output",
                {"status": CandidateStatus.COMPLETED},
                ValueError,
                "completed result requires output",
            ),
            (
                "completed with non-text output",
                {"status": CandidateStatus.COMPLETED, "output": 123},
                ValueError,
                "completed result requires output",
            ),
            (
                "completed with error_code",
                {
                    "status": CandidateStatus.COMPLETED,
                    "output": "x",
                    "error_code": code,
                },
                ValueError,
                "cannot contain failure details",
            ),
            (
                "completed but retryable",
                {"status": CandidateStatus.COMPLETED, "output": "x", "retryable": True},
                ValueError,
                "cannot contain failure details",
            ),
            (
                "failed with output",
                {"status": failed, "output": "x", "error_code": code},
                ValueError,
                "cannot contain output",
            ),
            (
                "timed out with output",
                {
                    "status": CandidateStatus.TIMED_OUT,
                    "output": "",
                    "error_code": CandidateErrorCode.DEADLINE_EXCEEDED,
                },
                ValueError,
                "cannot contain output",
            ),
            (
                "failed without error_code",
                {"status": failed},
                ValueError,
                "requires error_code",
            ),
            (
                "cancelled without error_code",
                {"status": CandidateStatus.CANCELLED},
                ValueError,
                "requires error_code",
            ),
            (
                "error_code is not an enum member",
                {"status": failed, "error_code": "internal_error"},
                TypeError,
                "error_code must be a CandidateErrorCode",
            ),
            (
                "cancelled and retryable",
                {
                    "status": CandidateStatus.CANCELLED,
                    "error_code": CandidateErrorCode.BACKEND_CANCELLED,
                    "retryable": True,
                },
                ValueError,
                "cancelled attempt cannot be retryable",
            ),
        )
        for label, kwargs, error_type, pattern in cases:
            with self.subTest(label), self.assertRaisesRegex(error_type, pattern):
                CandidateResult(**kwargs)

    def test_candidate_result_accepts_each_valid_shape(self):
        completed = CandidateResult(CandidateStatus.COMPLETED, output="")
        self.assertEqual(completed.output, "")
        self.assertIsNone(completed.error_code)
        self.assertFalse(completed.retryable)
        for status, code, retryable in (
            (CandidateStatus.FAILED, CandidateErrorCode.RATE_LIMITED, True),
            (CandidateStatus.FAILED, CandidateErrorCode.INVALID_REQUEST, False),
            (CandidateStatus.TIMED_OUT, CandidateErrorCode.DEADLINE_EXCEEDED, True),
            (CandidateStatus.CANCELLED, CandidateErrorCode.BACKEND_CANCELLED, False),
        ):
            with self.subTest(status=status, retryable=retryable):
                result = CandidateResult(status, error_code=code, retryable=retryable)
                self.assertIsNone(result.output)
                self.assertIs(result.error_code, code)
                self.assertIs(result.retryable, retryable)

    def test_retry_policy_rejects_misuse(self):
        policy = RetryPolicy(max_attempts=2)
        with self.assertRaisesRegex(TypeError, "must be a CandidateResult"):
            policy.should_retry(1, "failed")  # type: ignore[arg-type]
        for attempt in (0, -1, 2.0, True, None):
            with self.subTest(attempt=attempt):
                self.assertFalse(policy.allows_attempt(attempt))  # type: ignore[arg-type]

    def test_error_code_is_a_closed_set_of_stable_identifiers(self):
        for code in CandidateErrorCode:
            with self.subTest(code=code.name):
                self.assertRegex(code.value, r"^[a-z][a-z0-9_]{0,63}$")
        self.assertEqual(
            {code.value for code in CandidateErrorCode},
            {
                "backend_cancelled",
                "provider_unavailable",
                "rate_limited",
                "invalid_request",
                "tool_failure",
                "deadline_exceeded",
                "internal_error",
            },
        )

    def test_provider_exception_text_cannot_become_an_error_code(self):
        secret = "sk-live-0123456789"
        try:
            raise RuntimeError(f"401 for prompt 'fix payroll': Bearer {secret}")
        except RuntimeError as provider_error:
            leaked_text = str(provider_error)
        with self.assertRaises(TypeError) as raised:
            CandidateResult(CandidateStatus.FAILED, error_code=leaked_text)  # type: ignore[arg-type]
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("payroll", str(raised.exception))

    def test_candidate_identity_has_no_secret_or_credential_extension_point(self):
        public_fields = {item.name for item in fields(CandidateIdentity)}
        self.assertEqual(
            public_fields, {"candidate_id", "model", "runtime", "quantization"}
        )
        for forbidden in ("secret", "credential", "api_key", "token"):
            self.assertNotIn(forbidden, public_fields)

    def test_raw_provider_errors_do_not_have_a_result_field(self):
        result_fields = {item.name for item in fields(CandidateResult)}
        self.assertNotIn("exception", result_fields)
        self.assertNotIn("error_message", result_fields)
        failed = CandidateResult(
            CandidateStatus.FAILED,
            error_code=CandidateErrorCode.PROVIDER_UNAVAILABLE,
            retryable=True,
        )
        self.assertTrue(failed.retryable)
        self.assertTrue(RetryPolicy(max_attempts=2).should_retry(1, failed))
        with self.assertRaises(TypeError):
            CandidateResult(
                CandidateStatus.FAILED,
                error_code="provider error containing private content",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
