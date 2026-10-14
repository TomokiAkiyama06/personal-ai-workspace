"""Claude Code as a node's runtime (Decision 0083, 1, 5, 6 and 8; Decision
0087, 4): the real runtime, adapter, bridge, relay and launcher, a fake
``claude`` (``fake_claude_cli.py``) and a fake connection service that does
what ``execute_session`` does around the adapter (the gate, ``on_usage``).

* the CLI is started with the flags of Decision 0087, 4, the task on stdin
  and the token only in its environment;
* every model call is reported once, and a ``False`` answer stops the CLI;
* a ``submit`` through the bridge ends the session (the CLI is stopped) and
  is the node's outcome;
* a built-in tool left on, a missing PAW server, a turn without its output
  count and an authentication failure stop the CLI with their failures;
* no ``submit`` is ``no_submission``; a planner is never sent; the session's
  directory is gone afterwards.
"""

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from paw_backend.agents.claude_cli import ClaudeCliRuntime, ClaudeSessionAdapter
from paw_backend.agents.claude_stream import ClaudeStream, StreamRefused, paw_tool_names
from paw_backend.agents.cli_session import SubprocessLauncher, UnitNotStoppedError
from paw_backend.connections import (
    AgentSessionRequest,
    Secret,
    SessionResult,
)
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.runtime import NodeAssignment

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_claude_cli.py"
RELAY = HERE.parent / "deploy" / "cli-session" / "paw_mcp_relay.py"
# A token built from pieces (no literal a scanner would flag).
TOKEN = "paw-claude-canary-" + "5e1d" + "-token"


class Placement:
    def __init__(self):
        self.recorded = []

    async def record(self, placement, *, agent, model):
        self.recorded.append((placement, agent, model))


class Policy:
    async def allows(self, assignment):
        return True


class Connections:
    """What ``ConnectionService.execute_session`` does around the adapter."""

    def __init__(self, adapter, *, budget=10_000_000):
        self.adapter = adapter
        self.turns = []
        self.budget = budget

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


class ClaudeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.report = self.base / "report.json"
        self.scenario = self.base / "scenario.json"
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()

    def adapter(self):
        return ClaudeSessionAdapter(
            SubprocessLauncher(),
            executable=(
                sys.executable,
                "-I",
                str(FAKE),
                "--scenario",
                str(self.scenario),
                "--report",
                str(self.report),
            ),
        )

    async def run_node(
        self, actions, *, role=NodeRole.RESEARCHER, budget=10**7, **extra
    ):
        self.scenario.write_text(json.dumps({"actions": actions, **extra}))
        adapter = self.adapter()
        self.connections = Connections(adapter, budget=budget)
        self.placement = Placement()
        runtime = ClaudeCliRuntime(
            model="claude-sonnet-5",
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
            agent="claude",
            attempt=1,
            approach=0,
            tools=None,
            budget=None,
            worktrees={},
            roots={},
            placement=self.placement,
            connections=self.connections,
        )
        outcome = await asyncio.wait_for(runtime.run_node(assignment), 60)
        return outcome

    def read_report(self):
        return json.loads(self.report.read_text())


class SubmitTest(ClaudeCase):
    async def test_a_submit_ends_the_session_and_is_the_outcome(self):
        outcome = await self.run_node(
            [
                {"turn": [100, 10]},
                {"call": "submit", "arguments": {"summary": "It is in parse.py"}},
                {"turn": [5, 1]},
                {"sleep": 30},
            ]
        )
        self.assertEqual(outcome.result.summary, "It is in parse.py")
        self.assertEqual(self.connections.turns[0], (100, 10))
        report = self.read_report()
        flags = report["flags"]
        for flag in (
            "-p",
            "--include-partial-messages",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--restricted",
        ):
            self.assertIn(flag, flags)
        self.assertEqual(flags[flags.index("--tools") + 1], "")
        self.assertEqual(flags[flags.index("--allowedTools") + 1], "mcp__paw__*")
        self.assertEqual(flags[flags.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(flags[flags.index("--output-format") + 1], "stream-json")
        self.assertEqual(report["token_length"], len(TOKEN))
        self.assertNotIn(TOKEN, json.dumps(flags))
        self.assertIn("Where is the parser?", report["task"])
        self.assertEqual(report["listed"], ["submit"])
        self.assertIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", report["env"])
        self.assertEqual(list(self.sessions.iterdir()), [])  # cleaned up
        self.assertEqual(len(self.placement.recorded), 1)

    async def test_a_session_without_submit_is_no_submission(self):
        result = {
            "subtype": "success",
            "is_error": False,
            "modelUsage": {"m": {"inputTokens": 7, "outputTokens": 3}},
        }
        outcome = await self.run_node([{"turn": [7, 3]}, {"result": result}])
        self.assertEqual(outcome.error_class, "RuntimeError")
        self.assertTrue(outcome.escalate)
        raw = self.connections.raw
        self.assertEqual((raw.input_tokens, raw.output_tokens), (7, 3))

    async def test_a_planner_is_never_sent(self):
        outcome = await self.run_node([], role=NodeRole.PLANNER)
        self.assertEqual(self.placement.recorded, [])
        self.assertFalse(self.report.exists())
        self.assertTrue(outcome.escalate)


class StopTest(ClaudeCase):
    async def test_a_false_answer_stops_the_cli(self):
        await self.run_node(
            [{"turn": [600, 10]}, {"turn": [1, 1]}, {"sleep": 30}], budget=500
        )
        self.assertEqual(self.connections.turns, [(600, 10)])
        # A stop the service asked for is not a failure of the session (the
        # service records it as cancelled).
        self.assertIsNone(self.connections.raw.failure)

    async def test_a_builtin_tool_left_on_stops_the_cli(self):
        outcome = await self.run_node(
            [{"turn": [1, 1]}, {"sleep": 30}],
            init_tools=["mcp__paw__submit", "EndConversation", "Bash"],
        )
        self.assertEqual(self.connections.turns, [])
        self.assertEqual(outcome.error_class, "ValueError")

    async def test_a_missing_paw_server_stops_the_cli(self):
        outcome = await self.run_node(
            [{"sleep": 30}], mcp_servers=[{"name": "paw", "status": "failed"}]
        )
        self.assertEqual(outcome.error_class, "ValueError")

    async def test_a_turn_without_its_output_count_stops_the_cli(self):
        outcome = await self.run_node(
            [{"turn": [5, 5], "no_delta": True}, {"sleep": 30}]
        )
        self.assertEqual(self.connections.turns, [])
        self.assertEqual(outcome.error_class, "ValueError")

    async def test_an_authentication_failure_is_an_expired_credential(self):
        outcome = await self.run_node(
            [{"retry": "authentication_failed"}, {"sleep": 30}]
        )
        self.assertEqual(outcome.error_class, "ConnectionError")
        self.assertTrue(outcome.escalate)
        self.assertEqual(self.connections.raw.failure.value, "expired")


class LauncherTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancelled_start_leaves_no_process(self):
        import os

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        marker = directory / "pid"
        # A "CLI" that never reads its stdin: the prompt's write blocks.
        script = (
            "import os, time, pathlib;"
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        from paw_backend.agents.cli_session import CliLaunch, CliSessionSpec

        spec = CliSessionSpec(uuid.uuid4(), directory, "t" * 40, ("relay",))
        launch = CliLaunch(
            (sys.executable, "-c", script),
            {},
            Secret(TOKEN),
            "CLAUDE_CODE_OAUTH_TOKEN",
            b"x" * (8 * 1024 * 1024),
            spec,
        )
        starting = asyncio.create_task(SubprocessLauncher().start(launch))
        for _ in range(100):
            if marker.exists() and marker.read_text():
                break
            await asyncio.sleep(0.05)
        starting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await starting
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class UnstoppedTest(ClaudeCase):
    async def test_a_cli_that_cannot_be_stopped_keeps_its_directory(self):
        class StuckLauncher(SubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                real_stop = process.stop

                async def stop():
                    await real_stop()  # (the test's process does end)
                    raise OSError("the session's unit could not be stopped")

                process.stop = stop
                return process

        def adapter():
            return ClaudeSessionAdapter(
                StuckLauncher(),
                executable=(
                    sys.executable,
                    "-I",
                    str(FAKE),
                    "--scenario",
                    str(self.scenario),
                    "--report",
                    str(self.report),
                ),
            )

        self.adapter = adapter
        outcome = await self.run_node(
            [{"call": "submit", "arguments": {"summary": "s"}}, {"sleep": 30}]
        )
        self.assertEqual(len(list(self.sessions.iterdir())), 1)  # kept
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error_class, "AdapterError")

    async def test_a_stop_interrupted_by_cancellation_keeps_its_directory(self):
        # The session's deadline cancels the attempt while the adapter is
        # stopping the CLI (a slow systemctl stop): the stop is finished
        # first, it fails, so the files stay; the cancellation goes on.
        stopping = asyncio.Event()

        class SlowStuckLauncher(SubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                real_stop = process.stop

                async def stop():
                    stopping.set()
                    await asyncio.sleep(0.5)
                    await real_stop()  # (the test's process does end)
                    raise UnitNotStoppedError("the session's unit could not be stopped")

                process.stop = stop
                return process

        def adapter():
            return ClaudeSessionAdapter(
                SlowStuckLauncher(),
                executable=(
                    sys.executable,
                    "-I",
                    str(FAKE),
                    "--scenario",
                    str(self.scenario),
                    "--report",
                    str(self.report),
                ),
            )

        self.adapter = adapter
        running = asyncio.create_task(
            self.run_node(
                [{"call": "submit", "arguments": {"summary": "s"}}, {"sleep": 30}]
            )
        )
        await asyncio.wait_for(stopping.wait(), 20)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(len(list(self.sessions.iterdir())), 1)  # kept

    async def test_a_start_whose_cleanup_cannot_stop_keeps_its_directory(self):
        # The start failed (a timeout, a cancellation) and the stops that
        # cleaned it up failed too: the unit may still run with the session's
        # files mounted, although no process was handed to the adapter.
        class StartStuckLauncher(SubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                await process.stop()  # (the test's process does end)
                raise UnitNotStoppedError(
                    "the session's unit could not be stopped"
                ) from TimeoutError()

        def adapter():
            return ClaudeSessionAdapter(
                StartStuckLauncher(),
                executable=(
                    sys.executable,
                    "-I",
                    str(FAKE),
                    "--scenario",
                    str(self.scenario),
                    "--report",
                    str(self.report),
                ),
            )

        self.adapter = adapter
        outcome = await self.run_node([{"sleep": 30}])
        self.assertEqual(len(list(self.sessions.iterdir())), 1)  # kept
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error_class, "AdapterError")


class ReportTest(ClaudeCase):
    async def test_totals_without_any_turn_are_refused(self):
        result = {
            "subtype": "success",
            "is_error": False,
            "modelUsage": {"m": {"inputTokens": 7, "outputTokens": 3}},
        }
        await self.run_node([{"result": result}])
        self.assertEqual(self.connections.raw.failure.value, "invalid_response")


class GroupStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_child_that_ignores_sigterm_is_killed_too(self):
        import os
        import signal as signals

        from paw_backend.agents.cli_session import CliLaunch, CliSessionSpec

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        marker = directory / "child"
        child = (
            "import signal, time, os, pathlib;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        leader = (
            "import subprocess, sys, time;"
            f"subprocess.Popen([sys.executable, '-c', {child!r}],"
            " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
            " stderr=subprocess.DEVNULL);"
            "time.sleep(60)"
        )
        spec = CliSessionSpec(uuid.uuid4(), directory, "t" * 40, ("relay",))
        launch = CliLaunch(
            (sys.executable, "-c", leader),
            {},
            Secret(TOKEN),
            "CLAUDE_CODE_OAUTH_TOKEN",
            b"",
            spec,
        )
        process = await SubprocessLauncher().start(launch)
        for _ in range(100):
            if marker.exists() and marker.read_text():
                break
            await asyncio.sleep(0.05)
        await process.stop()
        pid = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            os.kill(pid, signals.SIGKILL)
            self.fail("the child that ignored SIGTERM outlived the stop")


class StreamTest(unittest.TestCase):
    def init(self, tools, servers=None, errors=None):
        message = {
            "type": "system",
            "subtype": "init",
            "tools": tools,
            "mcp_servers": servers or [{"name": "paw", "status": "connected"}],
        }
        if errors:
            message["mcp_server_errors"] = errors
        return json.dumps(message)

    def test_the_tools_must_be_exactly_the_paw_tools(self):
        expected = paw_tool_names(["bash", "submit"])
        ClaudeStream(expected).feed(self.init(sorted(expected) + ["EndConversation"]))
        ClaudeStream(expected).feed(self.init(sorted(expected)))
        for tools in (
            ["mcp__paw__submit"],
            sorted(expected) + ["Read"],
            sorted(expected) + ["mcp__other__x"],
        ):
            with self.subTest(tools=tools), self.assertRaises(StreamRefused):
                ClaudeStream(expected).feed(self.init(tools))
        with self.assertRaises(StreamRefused):
            ClaudeStream(expected).feed(
                self.init(sorted(expected), errors=[{"name": "x", "type": "invalid"}])
            )
        with self.assertRaises(StreamRefused):
            ClaudeStream(expected).feed(
                self.init(
                    sorted(expected),
                    servers=[
                        {"name": "paw", "status": "connected"},
                        {"name": "extra", "status": "connected"},
                    ],
                )
            )

    def test_a_call_is_counted_once_with_its_real_output(self):
        stream = ClaudeStream(frozenset())
        start = {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 20,
                        "cache_read_input_tokens": 30,
                        "output_tokens": 1,
                    },
                },
            },
        }
        delta = {
            "type": "stream_event",
            "event": {"type": "message_delta", "usage": {"output_tokens": 7}},
        }
        final = {
            "type": "stream_event",
            "event": {"type": "message_delta", "usage": {"output_tokens": 9}},
        }
        stop = {"type": "stream_event", "event": {"type": "message_stop"}}
        for message in (start, delta, final):
            self.assertIsNone(stream.feed(json.dumps(message)))
        turn = stream.feed(json.dumps(stop))
        self.assertEqual((turn.input_tokens, turn.output_tokens), (60, 9))
        # The same message again (an id seen before) is not counted twice.
        for message in (start, final):
            stream.feed(json.dumps(message))
        self.assertIsNone(stream.feed(json.dumps(stop)))
        # The assistant messages' placeholder counts are not read.
        assistant = {
            "type": "assistant",
            "message": {"id": "msg_2", "usage": {"output_tokens": 1}},
        }
        self.assertIsNone(stream.feed(json.dumps(assistant)))

    def test_the_result_totals(self):
        stream = ClaudeStream(frozenset())
        totals = stream.feed(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "modelUsage": {
                        "a": {
                            "inputTokens": 1,
                            "cacheReadInputTokens": 2,
                            "outputTokens": 3,
                        },
                        "b": {
                            "inputTokens": 4,
                            "cacheCreationInputTokens": 5,
                            "outputTokens": 6,
                        },
                    },
                }
            )
        )
        self.assertEqual((totals.input_tokens, totals.output_tokens), (12, 9))
        self.assertFalse(totals.is_error)
        crashed = stream.feed(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "modelUsage": {"a": {"inputTokens": 0, "outputTokens": 0}},
                }
            )
        )
        self.assertIsNone(crashed.input_tokens)
        self.assertTrue(crashed.is_error)

    def test_malformed_lines_are_refused(self):
        with self.assertRaises(StreamRefused):
            ClaudeStream(frozenset()).feed("{not json")
        self.assertIsNone(ClaudeStream(frozenset()).feed("   "))


if __name__ == "__main__":
    unittest.main()
