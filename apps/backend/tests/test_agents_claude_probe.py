"""The Claude connection's health probe (Decision 0100, 2 and 11): the real
adapter and launcher with a fake ``claude`` (``fake_claude_probe_cli.py``).

* the CLI is started with no tool and an empty MCP configuration, one turn at
  most, the fixed prompts and the token only in its environment;
* the verdicts: an answer with ``PAW_PROBE_OK`` is ``connected``; an
  authentication failure is ``expired``; a rate limit, an answer without the
  token, a second model call, a tool or an MCP server in ``system/init``, an
  error result or a CLI that ended without a result are ``unavailable``;
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
            self.assertIn("PAW_PROBE_OK", text)
            self.assertNotRegex(text, r"[0-9a-f]{8}-|/|@|\d{4}-\d{2}")

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


class FallbackTest(ProbeCase):
    """#325: a fallback is noted; the verdict stays what it was (whether a
    probe that fell back is ``connected`` is a human decision)."""

    async def test_a_fallback_is_logged_and_the_verdict_is_unchanged(self):
        plain = await self.probe(GOOD)
        with self.assertLogs("paw_backend.agents.claude_probe", "WARNING") as logs:
            report = await self.probe(FELL_BACK)
        self.assertEqual(
            (report.outcome, report.failure, report.input_tokens, report.output_tokens),
            (plain.outcome, plain.failure, plain.input_tokens, plain.output_tokens),
        )
        self.assertEqual(report.model, "claude-test-model")  # the configured one
        [line] = [m for m in logs.output if "model fallback" in m]
        self.assertIn("configured=claude-test-model", line)
        self.assertIn(f"used={FALLBACK_MODEL},{REFUSED_MODEL}", line)
        self.assertIn(f"fallbacks={REFUSED_MODEL}->{FALLBACK_MODEL} ", line)
        self.assertIn("safety_stops=2", line)
        # Nothing of the conversation, nor the token.
        self.assertNotIn("PAW_PROBE_OK", "\n".join(logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    async def test_a_probe_without_fallback_logs_none(self):
        with self.assertNoLogs("paw_backend.agents.claude_probe", "WARNING"):
            await self.probe(GOOD)

    async def test_a_fallback_after_a_failure_keeps_the_failure(self):
        report = await self.probe(FELL_BACK[:3] + [NO_ANSWER])
        self.assertEqual(
            (report.outcome, report.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE),
        )


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
