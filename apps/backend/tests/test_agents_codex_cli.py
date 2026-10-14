"""Codex CLI as a node's runtime through ``codex app-server`` (Decision 0083, 1,
5, 6 and 8; Decision 0087, 4; Decision 0089): the real runtime, adapter,
bridge, relay and launcher, a fake ``codex`` (``fake_codex_cli.py``, the stdio
protocol of ``rust-v0.162.1``) and a fake connection service that does what
``execute_session`` does around the adapter (the gate, ``on_usage``).

* an unverified version (the shipped list is empty) starts nothing with the
  credential; no credential handler, no session;
* the session's config turns every listed tool feature off and has the PAW
  relay as the only server; ``thread/start`` has ``environments: []``;
* each growth of the thread's total is reported once (a repeat is not; a
  shrinking total stops the CLI); a ``False`` answer stops the CLI;
* a ``submit`` ends the session and is the outcome;
* an item that can execute, an unknown item, another server's tool and a
  request from the server stop the CLI; the user message and a compaction do
  not;
* ``unauthorized`` is an expired credential, the usage limit a rate limit;
* the credential is in no argument, environment, config, log or ``repr``.
"""

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from paw_backend.agents.codex_app_server import (
    VERIFIED_VERSIONS,
    CodexStream,
    StreamRefused,
    Turn,
    version_of,
)
from paw_backend.agents.codex_cli import (
    DISABLED_FEATURES,
    CodexCliRuntime,
    CodexSessionAdapter,
    InteractiveSubprocessLauncher,
)
from paw_backend.connections import AgentSessionRequest, Secret, SessionResult
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.runtime import NodeAssignment

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_codex_cli.py"
RELAY = HERE.parent / "deploy" / "cli-session" / "paw_mcp_relay.py"
# A credential built from pieces (no literal a scanner would flag).
TOKEN = '{"tokens": {"access_token": "paw-codex-' + "c4na" + 'ry-token"}}'
VERSION = "0.162.1"


class Placement:
    def __init__(self):
        self.recorded = []

    async def record(self, placement, *, agent, model):
        self.recorded.append((placement, agent, model))


class Policy:
    async def allows(self, assignment):
        return True


class Credentials:
    """Puts the value in ``auth.json`` and takes it away again."""

    def __init__(self):
        self.placed = []
        self.collected = []

    async def place(self, secret, codex_home):
        (codex_home / "auth.json").write_text(secret.reveal())
        self.placed.append(codex_home)

    async def collect(self, codex_home):
        (codex_home / "auth.json").unlink(missing_ok=True)
        self.collected.append(codex_home)


class Connections:
    """What ``ConnectionService.execute_session`` does around the adapter."""

    def __init__(self, adapter, *, budget=10_000_000):
        self.adapter = adapter
        self.turns = []
        self.budget = budget
        self.raw = None

    async def execute_session(self, kind, request, *, gate=None):
        if gate is not None:
            await gate()

        async def on_usage(input_tokens, output_tokens):
            self.turns.append((input_tokens, output_tokens))
            self.budget -= input_tokens + output_tokens
            return self.budget > 0

        raw = await self.adapter.run_session(
            Secret(TOKEN),
            AgentSessionRequest(
                request.model,
                request.prompt,
                request.timeout_seconds,
                on_usage,
                attachment=request.attachment,
            ),
        )
        self.raw = raw
        return SessionResult(uuid.uuid4(), raw.submission, 0, 0, raw.failure, None)


class CodexCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.report = self.base / "report.json"
        self.scenario = self.base / "scenario.json"
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.credentials = Credentials()

    def adapter(self, *, verified=frozenset({VERSION}), credentials=True):
        return CodexSessionAdapter(
            InteractiveSubprocessLauncher(),
            drain_seconds=1.0,
            credentials=self.credentials if credentials else None,
            executable=(
                sys.executable,
                "-I",
                str(FAKE),
                "--scenario",
                str(self.scenario),
                "--report",
                str(self.report),
            ),
            verified_versions=verified,
        )

    async def run_node(
        self, actions, *, role=NodeRole.RESEARCHER, budget=10**7, adapter=None, **extra
    ):
        self.scenario.write_text(json.dumps({"actions": actions, **extra}))
        self.connections = Connections(adapter or self.adapter(), budget=budget)
        self.placement = Placement()
        runtime = CodexCliRuntime(
            model="gpt-5.5-codex",
            session_base=self.sessions,
            relay_command=(sys.executable, "-I", str(RELAY)),
            cloud_policy=Policy(),
        )
        assignment = NodeAssignment(
            task_id=uuid.uuid4(),
            node_key="r",
            role=role,
            title="Find the parser",
            goal="Where is the parser?",
            input={},
            upstream={},
            agent="codex",
            attempt=1,
            approach=0,
            tools=None,
            budget=None,
            worktrees={},
            roots={},
            placement=self.placement,
            connections=self.connections,
        )
        return await asyncio.wait_for(runtime.run_node(assignment), 60)

    def read_report(self):
        return json.loads(self.report.read_text())

    def probes(self):
        path = Path(str(self.report) + ".probes")
        return len(path.read_text().splitlines()) if path.exists() else 0


class SubmitTest(CodexCase):
    async def test_a_submit_ends_the_session_and_is_the_outcome(self):
        with self.assertLogs("paw_backend", level="DEBUG") as logs:
            logging.getLogger("paw_backend").debug("start")
            outcome = await self.run_node(
                [
                    {"usage": [100, 10]},
                    {"call": "submit", "arguments": {"summary": "It is in parse.py"}},
                    {"usage": [5, 1]},
                    {"sleep": 30},
                ]
            )
        self.assertEqual(outcome.result.summary, "It is in parse.py")
        self.assertEqual(self.connections.turns[0], (100, 10))
        report = self.read_report()
        self.assertEqual(report["args"], ["app-server", "--listen", "stdio://"])
        self.assertEqual(self.probes(), 1)
        start = report["requests"]["thread/start"]
        self.assertEqual(start["environments"], [])
        self.assertEqual(start["approvalPolicy"], "never")
        self.assertEqual(start["sandbox"], "read-only")
        self.assertTrue(start["ephemeral"])
        self.assertEqual(start["model"], "gpt-5.5-codex")
        init = report["requests"]["initialize"]
        self.assertTrue(init["capabilities"]["experimentalApi"])
        task = report["requests"]["turn/start"]["input"][0]["text"]
        self.assertIn("Where is the parser?", task)
        config = report["config"]
        for name in DISABLED_FEATURES:
            self.assertIs(config["features"][name], False, name)
        self.assertEqual(config["web_search"], "disabled")
        self.assertEqual(config["forced_login_method"], "chatgpt")
        self.assertEqual(list(config["mcp_servers"]), ["paw"])
        paw = config["mcp_servers"]["paw"]
        self.assertTrue(paw["required"])
        self.assertEqual(paw["enabled_tools"], ["submit"])
        self.assertEqual(paw["default_tools_approval_mode"], "approve")
        self.assertEqual(report["listed"], ["submit"])
        # The credential: in auth.json only (and taken back), nowhere else.
        self.assertEqual(report["auth_length"], len(TOKEN))
        self.assertEqual(len(self.credentials.collected), 1)
        for text in (json.dumps(report["args"]), json.dumps(config), *logs.output):
            self.assertNotIn("c4nary", text)
        self.assertIn("CODEX_HOME", report["env"])
        self.assertEqual(list(self.sessions.iterdir()), [])  # cleaned up
        self.assertEqual(len(self.placement.recorded), 1)

    async def test_a_session_without_submit_is_no_submission(self):
        outcome = await self.run_node([{"usage": [7, 3]}, {"complete": "completed"}])
        self.assertEqual(outcome.error_class, "RuntimeError")
        self.assertTrue(outcome.escalate)
        raw = self.connections.raw
        self.assertEqual((raw.input_tokens, raw.output_tokens), (7, 3))
        self.assertIsNone(raw.failure)

    async def test_a_planner_is_never_sent(self):
        outcome = await self.run_node([], role=NodeRole.PLANNER)
        self.assertEqual(self.placement.recorded, [])
        self.assertFalse(self.report.exists())
        self.assertTrue(outcome.escalate)


class TotalsTest(CodexCase):
    async def test_a_completed_turn_without_any_report_is_refused(self):
        outcome = await self.run_node([{"complete": "completed"}])
        raw = self.connections.raw
        self.assertEqual(raw.failure.value, "invalid_response")
        self.assertIsNone(raw.input_tokens)  # not known: the service estimates
        self.assertEqual(outcome.error_class, "ValueError")

    async def test_a_submit_keeps_the_totals_once_its_call_is_counted(self):
        await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {"summary": "done"}},
                {"usage": [5, 1]},
                {"sleep": 30},
            ]
        )
        raw = self.connections.raw
        self.assertEqual((raw.input_tokens, raw.output_tokens), (105, 11))
        self.assertEqual(self.connections.turns, [(100, 10), (5, 1)])

    async def test_a_submit_whose_call_was_not_counted_leaves_them_unknown(self):
        await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {"summary": "done"}},
                {"sleep": 30},
            ]
        )
        self.assertIsNone(self.connections.raw.input_tokens)

    async def test_a_completed_turn_without_the_last_calls_report_is_refused(self):
        # The call that made the submit never reported, though the turn ended.
        await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {"summary": "done"}},
                {"complete": "completed"},
            ]
        )
        raw = self.connections.raw
        self.assertIsNone(raw.input_tokens)
        self.assertEqual(raw.failure.value, "invalid_response")


class VersionTest(CodexCase):
    def test_no_version_is_verified_yet(self):
        self.assertEqual(VERIFIED_VERSIONS, frozenset())

    async def test_the_shipped_list_starts_nothing_with_the_credential(self):
        outcome = await self.run_node(
            [{"usage": [1, 1]}], adapter=self.adapter(verified=VERIFIED_VERSIONS)
        )
        self.assertEqual(self.probes(), 1)
        self.assertFalse(self.report.exists())  # app-server never started
        self.assertEqual(self.credentials.placed, [])
        self.assertEqual(self.connections.raw.failure.value, "invalid_response")
        self.assertEqual(outcome.error_class, "ValueError")

    async def test_another_version_is_refused(self):
        await self.run_node([{"usage": [1, 1]}], version="0.163.0")
        self.assertFalse(self.report.exists())
        self.assertEqual(self.connections.turns, [])

    async def test_without_a_credential_handler_nothing_starts(self):
        await self.run_node([], adapter=self.adapter(credentials=False))
        self.assertEqual(self.probes(), 0)
        self.assertEqual(self.connections.raw.failure.value, "unavailable")


class UsageTest(CodexCase):
    async def test_a_repeated_total_is_not_counted_again(self):
        await self.run_node(
            [
                {"usage": [10, 2]},
                {"repeat_usage": True},
                {"usage": [3, 1]},
                {"complete": "completed"},
            ]
        )
        self.assertEqual(self.connections.turns, [(10, 2), (3, 1)])
        raw = self.connections.raw
        self.assertEqual((raw.input_tokens, raw.output_tokens), (13, 3))

    async def test_a_shrinking_total_stops_the_cli(self):
        outcome = await self.run_node(
            [{"usage": [10, 2]}, {"total": [5, 2]}, {"sleep": 30}]
        )
        self.assertEqual(self.connections.turns, [(10, 2)])
        self.assertEqual(outcome.error_class, "ValueError")

    async def test_a_false_answer_stops_the_cli(self):
        await self.run_node(
            [{"usage": [600, 10]}, {"usage": [1, 1]}, {"sleep": 30}], budget=500
        )
        self.assertEqual(self.connections.turns, [(600, 10)])
        self.assertIsNone(self.connections.raw.failure)


class StopTest(CodexCase):
    async def assert_stopped(self, actions):
        outcome = await self.run_node([*actions, {"usage": [1, 1]}, {"sleep": 30}])
        self.assertEqual(self.connections.turns, [])
        self.assertEqual(outcome.error_class, "ValueError")
        self.assertEqual(self.connections.raw.failure.value, "invalid_response")

    async def test_an_item_that_can_execute_stops_the_cli(self):
        for kind in (
            "commandExecution",
            "fileChange",
            "dynamicToolCall",
            "collabAgentToolCall",
            "webSearch",
            "imageGeneration",
            "somethingNew",
        ):
            with self.subTest(kind=kind):
                await self.assert_stopped([{"item": {"type": kind, "id": "i"}}])

    async def test_another_servers_tool_stops_the_cli(self):
        item = {"type": "mcpToolCall", "id": "i", "server": "other", "tool": "submit"}
        await self.assert_stopped([{"item": item}])

    async def test_a_request_from_the_server_stops_the_cli(self):
        await self.assert_stopped(
            [{"request": "item/commandExecution/requestApproval"}]
        )

    async def test_non_tool_items_pass(self):
        await self.run_node(
            [
                {"item": {"type": "agentMessage", "id": "a", "text": "hi"}},
                {"item": {"type": "reasoning", "id": "r"}},
                {"item": {"type": "contextCompaction", "id": "c"}},
                {"usage": [2, 1]},
                {"complete": "completed"},
            ]
        )
        self.assertEqual(self.connections.turns, [(2, 1)])
        self.assertIsNone(self.connections.raw.failure)

    async def test_unauthorized_is_an_expired_credential(self):
        outcome = await self.run_node(
            [{"error": "unauthorized"}, {"complete": "failed"}]
        )
        self.assertEqual(outcome.error_class, "ConnectionError")
        self.assertTrue(outcome.escalate)
        self.assertEqual(self.connections.raw.failure.value, "expired")

    async def test_the_usage_limit_is_a_rate_limit(self):
        await self.run_node(
            [{"complete": "failed", "turn_error": "usageLimitExceeded"}]
        )
        self.assertEqual(self.connections.raw.failure.value, "rate_limited")

    async def test_a_retried_error_is_not_a_failure(self):
        await self.run_node(
            [
                {"error": "serverOverloaded", "will_retry": True},
                {"usage": [1, 1]},
                {"complete": "completed"},
            ]
        )
        self.assertIsNone(self.connections.raw.failure)

    async def test_a_crash_leaves_the_totals_unknown(self):
        await self.run_node([{"usage": [4, 4]}, {"exit": 3}])
        raw = self.connections.raw
        self.assertEqual(raw.failure.value, "internal_error")
        self.assertIsNone(raw.input_tokens)
        self.assertEqual(self.connections.turns, [(4, 4)])


class ReprTest(unittest.TestCase):
    def test_the_adapter_shows_nothing(self):
        adapter = CodexSessionAdapter(InteractiveSubprocessLauncher())
        self.assertEqual(repr(adapter), "CodexSessionAdapter()")


class StreamTest(unittest.TestCase):
    def ready(self):
        stream = CodexStream({"submit"}, frozenset({VERSION}))
        stream.feed(json.dumps({"id": 1, "result": {"userAgent": f"x/{VERSION} (L)"}}))
        stream.feed(json.dumps({"id": 2, "result": {"thread": {"id": "t"}}}))
        return stream

    def usage(self, total_in, total_out, thread="t"):
        total = {"inputTokens": total_in, "outputTokens": total_out}
        return json.dumps(
            {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": thread, "tokenUsage": {"total": total}},
            }
        )

    def test_the_version_of_the_user_agent(self):
        self.assertEqual(
            version_of("codex_cli_rs/0.162.1 (Linux 6; x86_64) t"), "0.162.1"
        )
        self.assertIsNone(version_of("nothing"))
        with self.assertRaises(StreamRefused):
            CodexStream(set()).feed(
                json.dumps({"id": 1, "result": {"userAgent": f"x/{VERSION}"}})
            )

    def test_the_growth_of_the_total_is_one_turn(self):
        stream = self.ready()
        self.assertEqual(stream.feed(self.usage(10, 1)), Turn(10, 1))
        self.assertIsNone(stream.feed(self.usage(10, 1)))
        self.assertEqual(stream.feed(self.usage(15, 4)), Turn(5, 3))
        with self.assertRaises(StreamRefused):
            stream.feed(self.usage(14, 4))
        with self.assertRaises(StreamRefused):
            self.ready().feed(self.usage(1, 1, thread="other"))

    def test_malformed_lines_are_refused(self):
        with self.assertRaises(StreamRefused):
            CodexStream(set()).feed("{not json")
        self.assertIsNone(CodexStream(set()).feed("  "))


if __name__ == "__main__":
    unittest.main()
