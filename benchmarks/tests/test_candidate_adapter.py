"""Contract tests for the provider-neutral candidate adapter interface."""

import asyncio
import unittest
from dataclasses import fields

from benchmarks.candidate_adapter import (
    AttemptControl,
    CancellationReason,
    CancellationToken,
    CandidateAdapter,
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
                CandidateStatus.CANCELLED, error_code="backend_cancelled"
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

    def test_backend_controls_retry_attempts(self):
        policy = RetryPolicy(max_attempts=2)
        self.assertTrue(policy.allows_attempt(1))
        self.assertTrue(policy.allows_attempt(2))
        self.assertFalse(policy.allows_attempt(3))

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
            CandidateStatus.FAILED, error_code="provider_unavailable", retryable=True
        )
        self.assertTrue(failed.retryable)
        self.assertTrue(RetryPolicy(max_attempts=2).should_retry(1, failed))


if __name__ == "__main__":
    unittest.main()
