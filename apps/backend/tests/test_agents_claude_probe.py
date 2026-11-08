"""The Claude connection's health probe (Decision 0100, 2 and 11): the real
adapter and launcher with a fake ``claude`` (``fake_claude_probe_cli.py``).

* the CLI is started with no tool and an empty MCP configuration, one turn at
  most, the fixed prompts and the token only in its environment;
* the verdicts: an answer with ``PAW_PROBE_OK`` is ``connected``; an
  authentication failure is ``expired``; a rate limit, an answer without the
  token, a second model call, a tool or an MCP server in ``system/init``, an
  error result or a CLI that ended without a result are ``unavailable``; a
  model fallback (the CLI's notice or block: the CLI is stopped there) or a
  safety stop is ``unavailable`` with ``model_fallback`` (Decision 0102, 3);
* a probe past its deadline stops the CLI; the session's directory is gone
  afterwards; ``run`` starts nothing.
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from paw_backend.agents.claude_probe import (
    PROBE_PROMPT,
    PROBE_SYSTEM_PROMPT,
    ClaudeProbeAdapter,
)
from paw_backend.agents.claude_stream import (
    UNKNOWN_MODEL,
    ClaudeStream,
    Fallback,
    StreamRefused,
    fallback_note,
)
from paw_backend.agents.cli_session import SubprocessLauncher
from paw_backend.connections import (
    AdapterFailure,
    AdapterRegistry,
    AdapterRequest,
    FailureCode,
    ProbeOutcome,
    Secret,
)

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_claude_probe_cli.py"
# A token built from pieces (no literal a scanner would flag).
TOKEN = "paw-claude-probe-canary-" + "7a2c" + "-token"
GOOD = [
    {"turn": [120, 4]},
    {
        "result": {
            "subtype": "success",
            "is_error": False,
            "result": "PAW_PROBE_OK.",
            "modelUsage": {"m": {"inputTokens": 120, "outputTokens": 4}},
        }
    },
]
# #325: what the CLI wrote in production when the configured model's
# safeguard stopped the probe and it answered on another model.
REFUSED_MODEL, FALLBACK_MODEL = "claude-test-model", "claude-older-model"
FALLBACK_NOTICE = {
    "line": {
        "type": "system",
        "subtype": "model_refusal_fallback",
        "original_model": REFUSED_MODEL,
        "fallback_model": FALLBACK_MODEL,
    }
}
FALLBACK_BLOCK = {
    "line": {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "fallback",
                    "from": {"model": REFUSED_MODEL},
                    "to": {"model": FALLBACK_MODEL},
                },
                {"type": "text", "text": "PAW_PROBE_OK"},
            ]
        },
    }
}
FELL_BACK = [
    FALLBACK_NOTICE,
    {"turn": [120, 4]},
    FALLBACK_BLOCK,
    {
        "result": {
            **GOOD[1]["result"],
            "safety_stops": 2,
            "modelUsage": {
                REFUSED_MODEL: {"inputTokens": 100, "outputTokens": 0},
                FALLBACK_MODEL: {"inputTokens": 20, "outputTokens": 4},
            },
        }
    },
]
NO_ANSWER = {"result": {k: v for k, v in GOOD[1]["result"].items() if k != "result"}}


class ProbeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.report = self.base / "report.json"
        self.scenario = self.base / "scenario.json"
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()

    def adapter(self, **options):
        return ClaudeProbeAdapter(
            SubprocessLauncher(),
            model="claude-test-model",
            session_base=self.sessions,
            executable=(
                sys.executable,
                "-I",
                str(FAKE),
                "--scenario",
                str(self.scenario),
                "--report",
                str(self.report),
            ),
            **options,
        )

    async def probe(self, actions, **scenario):
        self.scenario.write_text(json.dumps({"actions": actions, **scenario}))
        return await self.adapter().check_health(Secret(TOKEN))

    def reported(self):
        return json.loads(self.report.read_text())


class LaunchTest(ProbeCase):
    async def test_the_cli_runs_without_tools_or_servers_and_the_fixed_prompts(self):
        await self.probe(GOOD)
        report = self.reported()
        flags = report["flags"]
        self.assertEqual(flags[flags.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", flags)
        self.assertNotIn("--allowedTools", flags)
        self.assertEqual(flags[flags.index("--max-turns") + 1], "1")
        self.assertEqual(flags[flags.index("--model") + 1], "claude-test-model")
        self.assertEqual(flags[flags.index("--system-prompt") + 1], PROBE_SYSTEM_PROMPT)
        self.assertIn("--restricted", flags)
        self.assertEqual(report["mcp"], {"mcpServers": {}})
        self.assertEqual(report["task"], PROBE_PROMPT)
        # The token is in the environment only.
        self.assertEqual(report["token_length"], len(TOKEN))
        self.assertNotIn(TOKEN, json.dumps(flags))
        self.assertIn("CLAUDE_CONFIG_DIR", report["env"])

    async def test_the_session_directory_is_gone_afterwards(self):
        await self.probe(GOOD)
        self.assertEqual(list(self.sessions.iterdir()), [])

    def test_the_prompts_hold_nothing_but_the_fixed_text(self):
        for text in (PROBE_SYSTEM_PROMPT, PROBE_PROMPT):
            self.assertNotRegex(text, r"[0-9a-f]{8}-|/|@|\d{4}-\d{2}")
        self.assertIn("PAW_PROBE_OK", PROBE_PROMPT)

    def test_the_prompts_are_the_decisions_text(self):
        # Decision 0102, 1 (superseding the wording of Decision 0100, 2): the
        # text itself is pinned; changing it needs a new decision.
        self.assertEqual(PROBE_SYSTEM_PROMPT, "Answer the user's message briefly.")
        self.assertEqual(PROBE_PROMPT, "Please reply with the word PAW_PROBE_OK.")

    def test_the_prompts_name_neither_the_purpose_nor_the_tools(self):
        # #325: words a safeguard reads as reconnaissance are left out.
        for text in (PROBE_SYSTEM_PROMPT, PROBE_PROMPT):
            for word in ("connectivity", "check", "backend", "service", "tool"):
                with self.subTest(text=text, word=word):
                    self.assertNotIn(word, text.lower())

    async def test_run_starts_nothing(self):
        adapter = self.adapter()
        with self.assertRaises(AdapterFailure) as caught:
            await adapter.run(Secret(TOKEN), AdapterRequest("m", "p", 1.0))
        self.assertIs(
            AdapterFailure.code.__get__(caught.exception), FailureCode.INTERNAL_ERROR
        )
        self.assertFalse(self.report.exists())

    def test_it_registers_as_the_claude_connections_adapter(self):
        registry = AdapterRegistry()
        self.assertEqual(registry.register(self.adapter()).value, "claude")


class VerdictTest(ProbeCase):
    async def test_an_answer_with_the_token_is_connected_with_its_tokens(self):
        report = await self.probe(GOOD)
        self.assertEqual(
            (report.outcome, report.failure), (ProbeOutcome.CONNECTED, None)
        )
        self.assertEqual((report.input_tokens, report.output_tokens), (120, 4))
        self.assertEqual(report.model, "claude-test-model")
        self.assertNotIn("PAW_PROBE_OK", repr(report))

    async def test_an_authentication_failure_is_expired(self):
        report = await self.probe(
            [{"retry": "authentication_failed"}, {"sleep": 30}, *GOOD]
        )
        self.assertEqual(
            (report.outcome, report.failure),
            (ProbeOutcome.EXPIRED, FailureCode.EXPIRED),
        )

    async def test_the_other_endings_are_unavailable(self):
        error_result = {
            "result": {
                "subtype": "error_during_execution",
                "is_error": True,
                "modelUsage": {"m": {"inputTokens": 5, "outputTokens": 1}},
            }
        }
        for name, actions, scenario, code in (
            ("a rate limit", [{"retry": "rate_limit"}, {"turn": [5, 1]}, error_result],
             {}, FailureCode.RATE_LIMITED),
            # Codex review of #317: no model call completed.
            ("a rate limit before any call", [{"retry": "rate_limit"}, error_result],
             {}, FailureCode.RATE_LIMITED),
            ("a rate limit and no result", [{"retry": "rate_limit"}, {"exit": 1}],
             {}, FailureCode.RATE_LIMITED),
            ("no token in the answer",
             [GOOD[0], {"result": {**GOOD[1]["result"], "result": "Hello"}}],
             {}, FailureCode.INVALID_RESPONSE),
            ("no answer", [GOOD[0], NO_ANSWER], {}, FailureCode.INVALID_RESPONSE),
            ("two model calls", [GOOD[0], *GOOD], {}, FailureCode.INVALID_RESPONSE),
            ("no model call", [GOOD[1]], {}, FailureCode.INVALID_RESPONSE),
            ("a tool in init", GOOD, {"init_tools": ["Bash"]},
             FailureCode.INVALID_RESPONSE),
            ("EndConversation in init", GOOD, {"init_tools": ["EndConversation"]},
             FailureCode.INVALID_RESPONSE),
            ("an MCP server", GOOD,
             {"mcp_servers": [{"name": "paw", "status": "connected"}]},
             FailureCode.INVALID_RESPONSE),
            ("an error result", [{"turn": [5, 1]}, error_result], {},
             FailureCode.INTERNAL_ERROR),
            ("no result", [GOOD[0], {"exit": 1}], {}, FailureCode.INTERNAL_ERROR),
        ):  # fmt: skip
            with self.subTest(name):
                report = await self.probe(actions, **scenario)
                self.assertEqual(
                    (report.outcome, report.failure), (ProbeOutcome.UNAVAILABLE, code)
                )

    async def test_a_probe_past_its_deadline_stops_the_cli(self):
        self.scenario.write_text(json.dumps({"actions": [{"sleep": 60}, *GOOD]}))
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(1.0):
                await self.adapter().check_health(Secret(TOKEN))
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(list(self.sessions.iterdir()), [])


class FailureLogTest(ProbeCase):
    """Issue #342: an ``internal_error`` / ``invalid_response`` says why in one
    warning: fixed codes only, never the answer, the token or an error's text."""

    def failure_lines(self, logs):
        return [
            r.getMessage()
            for r in logs.records
            if r.getMessage().startswith("Claude probe failed:")
        ]

    async def test_each_failure_is_logged_once_with_why(self):
        error_result = {
            "result": {
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "secret words of the model",
                "modelUsage": {"m": {"inputTokens": 5, "outputTokens": 1}},
            }
        }
        overloaded = {
            "line": {
                "type": "system",
                "subtype": "api_retry",
                "error": "server_error",
                "error_status": 529,
                "message": "upstream said secret words",
            }
        }
        cases = {
            "an error result": (
                [overloaded, {"turn": [5, 1]}, error_result],
                "failure=internal_error why=result_error"
                " result=error_during_execution is_error=yes retry=server_error"
                " http=5xx initialized=yes turns=1",
            ),
            "no result": (
                [GOOD[0], {"exit": 1}],
                "failure=internal_error why=no_result result=- is_error=-",
            ),
            "no token": (
                [GOOD[0], {"result": {**GOOD[1]["result"], "result": "Hello"}}],
                "failure=invalid_response why=answer_without_token result=success",
            ),
            "two calls": ([GOOD[0], *GOOD], "why=second_model_call"),
            "no model call": ([GOOD[1]], "why=no_model_call"),
        }
        for name, (actions, expected) in cases.items():
            with self.subTest(name):
                with self.assertLogs(
                    "paw_backend.agents.claude_probe", "WARNING"
                ) as logs:
                    await self.probe(actions)
                (line,) = self.failure_lines(logs)
                self.assertIn(expected, line)
                for hidden in ("secret words", "PAW_PROBE_OK", "Hello", TOKEN):
                    self.assertNotIn(hidden, line)

    async def test_a_refused_stream_says_so(self):
        with self.assertLogs("paw_backend.agents.claude_probe", "WARNING") as logs:
            await self.probe(GOOD, init_tools=["Bash"])
        (line,) = self.failure_lines(logs)
        self.assertIn("failure=invalid_response why=refused", line)

    async def test_connected_expired_and_rate_limited_log_no_failure(self):
        error_result = {
            "result": {
                "subtype": "error_during_execution",
                "is_error": True,
                "modelUsage": {"m": {"inputTokens": 5, "outputTokens": 1}},
            }
        }
        for actions in (
            GOOD,
            [{"retry": "authentication_failed"}, {"sleep": 30}],
            [{"retry": "rate_limit"}, error_result],
        ):
            with self.subTest(actions=actions):
                with self.assertNoLogs("paw_backend.agents.claude_probe", "WARNING"):
                    await self.probe(actions)


class FallbackTest(ProbeCase):
    """#325, Decision 0102, 3: a probe whose configured model was refused is
    ``unavailable`` with ``model_fallback`` (never ``connected``, and never
    ``expired`` for that alone); the fallback is also logged."""

    def assert_fallback(self, report):
        self.assertEqual(
            (report.outcome, report.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.MODEL_FALLBACK),
        )
        self.assertEqual(report.model, "claude-test-model")  # the configured one

    async def test_the_production_form_is_a_model_fallback_and_is_logged(self):
        # One model call, the notice first: the CLI is stopped at the notice
        # (the sleep after it is not waited for, nothing after it is read).
        started = time.monotonic()
        with self.assertLogs("paw_backend.agents.claude_probe", "WARNING") as logs:
            report = await self.probe([FELL_BACK[0], {"sleep": 60}, *FELL_BACK[1:]])
        self.assertLess(time.monotonic() - started, 20)
        self.assert_fallback(report)
        self.assertEqual((report.input_tokens, report.output_tokens), (None, None))
        [line] = [m for m in logs.output if "model fallback" in m]
        self.assertIn("configured=claude-test-model", line)
        self.assertIn(f"fallbacks={REFUSED_MODEL}->{FALLBACK_MODEL} ", line)
        # The result was never read.
        self.assertIn("used=- ", line)
        self.assertIn("safety_stops=-", line)
        # Nothing of the conversation, nor the token.
        self.assertNotIn("PAW_PROBE_OK", "\n".join(logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        self.assertEqual(list(self.sessions.iterdir()), [])

    async def test_two_model_calls_with_the_notice_between_are_a_model_fallback(self):
        # The refused call, the notice, then the fallback model's call: the
        # CLI is stopped before the second call is read (only the first
        # call's tokens are known).
        report = await self.probe(
            [{"turn": [100, 0]}, FALLBACK_NOTICE, {"turn": [20, 4]}, *FELL_BACK[2:]]
        )
        self.assert_fallback(report)
        self.assertEqual((report.input_tokens, report.output_tokens), (100, 0))

    async def test_two_model_calls_without_a_notice_stay_an_invalid_response(self):
        # The CLI did not say it fell back: the second call stops it first
        # (Decision 0100, 2), and the result (its safety stops) is not read.
        report = await self.probe([{"turn": [100, 0]}, {"turn": [20, 4]}, FELL_BACK[3]])
        self.assertEqual(
            (report.outcome, report.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE),
        )

    async def test_the_assistant_block_alone_is_a_model_fallback(self):
        self.assert_fallback(await self.probe([GOOD[0], FALLBACK_BLOCK, GOOD[1]]))

    async def test_a_safety_stop_alone_is_a_model_fallback(self):
        stopped = {"result": {**GOOD[1]["result"], "safety_stops": 1}}
        with self.assertLogs("paw_backend.agents.claude_probe", "WARNING") as logs:
            report = await self.probe([GOOD[0], stopped])
        self.assert_fallback(report)
        self.assertEqual((report.input_tokens, report.output_tokens), (120, 4))
        self.assertIn("safety_stops=1", "\n".join(logs.output))

    async def test_no_safety_stop_and_no_fallback_is_connected(self):
        another_name = {
            "claude-test-model-20261101": {"inputTokens": 120, "outputTokens": 4}
        }
        for name, result in (
            ("safety_stops 0", {**GOOD[1]["result"], "safety_stops": 0}),
            ("no safety_stops", GOOD[1]["result"]),
            # Another name in ``modelUsage`` alone is not a fallback (#330).
            ("another model name", {**GOOD[1]["result"], "modelUsage": another_name}),
        ):
            with self.subTest(name):
                report = await self.probe([GOOD[0], {"result": result}])
                self.assertEqual(
                    (report.outcome, report.failure), (ProbeOutcome.CONNECTED, None)
                )

    async def test_a_model_fallback_comes_before_the_other_failures(self):
        error_result = {
            "subtype": "error_during_execution",
            "is_error": True,
            "safety_stops": 1,
        }
        no_answer = {**NO_ANSWER["result"], "safety_stops": 1}
        hello = {**GOOD[1]["result"], "result": "Hello"}
        for name, actions in (
            ("an error result", [GOOD[0], {"result": error_result}]),
            ("a rate limit",
             [{"retry": "rate_limit"}, GOOD[0], {"result": error_result}]),
            ("no answer", [GOOD[0], {"result": no_answer}]),
            ("no token in the answer",
             [GOOD[0], FALLBACK_BLOCK, {"result": hello}]),
            ("no result", [GOOD[0], FALLBACK_BLOCK, {"exit": 1}]),
        ):  # fmt: skip
            with self.subTest(name):
                self.assert_fallback(await self.probe(actions))

    async def test_an_expired_probe_stays_expired(self):
        # The refused credential stops the CLI before the notice is read.
        report = await self.probe(
            [{"retry": "authentication_failed"}, FALLBACK_NOTICE, *FELL_BACK[1:]]
        )
        self.assertEqual(
            (report.outcome, report.failure),
            (ProbeOutcome.EXPIRED, FailureCode.EXPIRED),
        )

    async def test_a_probe_without_fallback_logs_none(self):
        with self.assertNoLogs("paw_backend.agents.claude_probe", "WARNING"):
            await self.probe(GOOD)


class FallbackStreamTest(unittest.TestCase):
    def feed(self, stream, messages):
        return [stream.feed(json.dumps(m)) for m in messages]

    def test_the_fallback_is_kept_once_and_the_stream_says_the_same(self):
        init = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
        result = {"type": "result", **FELL_BACK[3]["result"]}
        plain = ClaudeStream(frozenset(), probe=True)
        fell = ClaudeStream(frozenset(), probe=True)
        expected = self.feed(plain, [init, result])
        got = self.feed(
            fell, [init, FALLBACK_NOTICE["line"], FALLBACK_BLOCK["line"], result]
        )
        self.assertEqual([g for g in got if g is not None], expected[1:])
        self.assertEqual(fell.answer, plain.answer)
        self.assertEqual(fell.fallbacks, [Fallback(REFUSED_MODEL, FALLBACK_MODEL)])
        self.assertEqual(fell.models, frozenset({REFUSED_MODEL, FALLBACK_MODEL}))
        self.assertEqual(fell.safety_stops, 2)
        self.assertTrue(fell.fell_back)
        self.assertEqual(plain.fallbacks, [])

    def test_a_malformed_fallback_is_skipped_never_refused(self):
        stream = ClaudeStream(frozenset())
        for message in (
            {"type": "system", "subtype": "model_refusal_fallback"},
            {
                "type": "system",
                "subtype": "model_refusal_fallback",
                "original_model": "a model\nwith a line",
                "fallback_model": 7,
            },
            {"type": "assistant", "message": {"content": [{"type": "fallback"}]}},
            {"type": "assistant", "message": {"content": "text"}},
            {"type": "assistant", "message": None},
            {"type": "result", "subtype": "success", "safety_stops": "2"},
        ):
            with self.subTest(message):
                stream.feed(json.dumps(message))
        self.assertEqual(stream.fallbacks, [Fallback(UNKNOWN_MODEL, UNKNOWN_MODEL)])
        self.assertIsNone(stream.safety_stops)

    def test_the_note(self):
        stream = ClaudeStream(frozenset())
        self.assertIsNone(fallback_note(stream, "m"))
        # Another name in ``modelUsage`` alone is not a fallback.
        stream.models = frozenset({"m", "m-full-id"})
        stream.safety_stops = 0
        self.assertIsNone(fallback_note(stream, "m"))
        stream.safety_stops = 1
        self.assertEqual(
            fallback_note(stream, "m"),
            "configured=m used=m,m-full-id fallbacks=- safety_stops=1",
        )
        stream.models = None
        stream.fallbacks.append(Fallback("m", "n"))
        self.assertEqual(
            fallback_note(stream, "m"),
            "configured=m used=- fallbacks=m->n safety_stops=1",
        )


class StreamTest(unittest.TestCase):
    def test_a_probe_stream_expects_no_tools(self):
        with self.assertRaises(ValueError):
            ClaudeStream(frozenset({"mcp__paw__bash"}), probe=True)
        stream = ClaudeStream(frozenset(), probe=True)
        stream.check_init({"type": "system", "tools": [], "mcp_servers": []})
        for message, reason in (
            ({"tools": ["EndConversation"], "mcp_servers": []}, "tools"),
            ({"tools": [], "mcp_servers": [{"name": "x"}]}, "mcp"),
            ({"tools": [], "mcp_servers": [], "mcp_server_errors": ["x"]}, "mcp"),
        ):
            with self.subTest(message), self.assertRaises(StreamRefused) as caught:
                stream.check_init(message)
            self.assertEqual(caught.exception.reason, reason)

    def test_only_a_probe_stream_keeps_the_answer(self):
        line = json.dumps({"type": "result", "subtype": "success", "result": "hi"})
        node = ClaudeStream(frozenset())
        node.feed(line)
        self.assertIsNone(node.answer)
        probe = ClaudeStream(frozenset(), probe=True)
        probe.feed(line)
        self.assertEqual(probe.answer, "hi")


if __name__ == "__main__":
    unittest.main()
