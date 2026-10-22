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
import re
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from paw_backend.agents.cli_session import UnitNotStoppedError
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
from paw_backend.connections import (
    AgentSessionRequest,
    FailureCode,
    Secret,
    SessionResult,
)
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

    async def collect(self, secret, codex_home, write_back):
        (codex_home / "auth.json").unlink(missing_ok=True)
        self.collected.append(codex_home)


class Connections:
    """What ``ConnectionService.execute_session`` does around the adapter."""

    def __init__(
        self,
        adapter,
        *,
        budget=10_000_000,
        write_back=None,
        bind_session=None,
        still_held=None,
    ):
        self.adapter = adapter
        self.write_back = write_back
        self.bind_session = bind_session
        self.still_held = still_held
        self.cli_running = []  # the adapter said its CLI could not be stopped
        self.turns = []
        self.budget = budget
        self.connection_id = uuid.uuid4()
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
                connection_id=self.connection_id,
                write_back=self.write_back,
                cli_running=lambda: self.cli_running.append(True),
                bind_session=self.bind_session,
                still_held=self.still_held,
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
        self.write_back = None  # the service's write-back, when a test sets one
        # The service's slot binding and check (Decision 0096), when set.
        self.bind_session = None
        self.still_held = None

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
        self,
        actions,
        *,
        role=NodeRole.RESEARCHER,
        budget=10**7,
        adapter=None,
        fallback=False,
        **extra,
    ):
        self.scenario.write_text(json.dumps({"actions": actions, **extra}))
        self.connections = Connections(
            adapter or self.adapter(),
            budget=budget,
            write_back=self.write_back,
            bind_session=self.bind_session,
            still_held=self.still_held,
        )
        self.placement = Placement()
        runtime = CodexCliRuntime(
            model="gpt-5.5-codex",
            session_base=self.sessions,
            relay_command=(sys.executable, "-I", str(RELAY)),
            cloud_policy=Policy(),
        )
        self.task_id = uuid.uuid4()
        assignment = NodeAssignment(
            task_id=self.task_id,
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
        run = runtime.run_as_fallback if fallback else runtime.run_node
        return await asyncio.wait_for(run(assignment), 60)

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

    async def test_the_session_start_is_logged_once(self):
        # Decision 0091, 4: the egress log's session=<uuid> is tied to the
        # task, the node, the connection and the CLI by one backend line.
        with self.assertLogs("paw_backend.agents.cli_session", "INFO") as logs:
            await self.run_node(
                [{"call": "submit", "arguments": {"summary": "It is in parse.py"}}]
            )
        (line,) = [line for line in logs.output if "CLI session started" in line]
        session = re.search(r" session=([0-9a-f-]{36}) ", line)
        self.assertIsNotNone(session)
        uuid.UUID(session.group(1))
        self.assertIn(f" task={self.task_id} ", line)
        self.assertIn(" node=r ", line)
        self.assertIn(f" connection={self.connections.connection_id} ", line)
        self.assertTrue(line.endswith(" cli=codex"))
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        self.assertNotIn("Where is the parser?", "\n".join(logs.output))

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

    async def test_an_earlier_calls_report_does_not_count_for_the_submit(self):
        # A first call (refused: no summary) was reported; the submit's item
        # line comes only after the bridge ended. Its own report must be read.
        await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {}},
                {"usage": [1, 1]},
                {"call": "submit", "arguments": {"summary": "done"}, "item_late": True},
                {"usage": [5, 1]},
                {"sleep": 30},
            ]
        )
        raw = self.connections.raw
        self.assertEqual(self.connections.turns, [(100, 10), (1, 1), (5, 1)])
        self.assertEqual((raw.input_tokens, raw.output_tokens), (106, 12))

    async def test_a_refusal_after_the_submit_fails_the_attempt(self):
        outcome = await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {"summary": "done"}},
                {"item": {"type": "commandExecution", "id": "x"}},
                {"sleep": 30},
            ]
        )
        self.assertEqual(self.connections.raw.failure.value, "invalid_response")
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error_class, "ValueError")

    async def test_a_refusal_after_the_submits_report_fails_the_attempt(self):
        # The submit's own report makes the totals known; an item that can
        # execute right after it must still be read and refused.
        outcome = await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {"summary": "done"}},
                {"sleep": 0.3},  # the reader is draining by now
                {"usage": [5, 1]},
                {"item": {"type": "commandExecution", "id": "x"}},
                {"sleep": 30},
            ]
        )
        self.assertEqual(self.connections.raw.failure.value, "invalid_response")
        self.assertIsNone(outcome.result)


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


class OutOfMemoryTest(CodexCase):
    """Decision 0092, 4: the CLI's unit ended with ``Result=oom-kill``
    (``CliProcess.oom_killed``, read before the stop)."""

    def oom_adapter(self, killed="app-server"):
        class OomLauncher(InteractiveSubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                if killed in launch.argv:
                    process.oom_killed = True
                return process

        return CodexSessionAdapter(
            OomLauncher(),
            drain_seconds=1.0,
            credentials=self.credentials,
            executable=(
                sys.executable,
                "-I",
                str(FAKE),
                "--scenario",
                str(self.scenario),
                "--report",
                str(self.report),
            ),
            verified_versions=frozenset({VERSION}),
        )

    async def test_without_a_submit_it_is_agent_out_of_memory(self):
        outcome = await self.run_node(
            [{"usage": [7, 3]}, {"complete": "completed"}],
            adapter=self.oom_adapter(),
        )
        self.assertEqual(outcome.error_class, "AgentOutOfMemory")
        self.assertEqual(outcome.message, "out_of_memory")
        self.assertFalse(outcome.escalate)
        self.assertEqual(list(self.sessions.iterdir()), [])  # cleaned up

    async def test_it_comes_before_the_other_failures(self):
        outcome = await self.run_node(
            [{"usage": [7, 3]}, {"complete": "failed"}],
            adapter=self.oom_adapter(),
        )
        self.assertEqual(outcome.error_class, "AgentOutOfMemory")

    async def test_a_result_submitted_before_stands(self):
        outcome = await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {"summary": "It is in parse.py"}},
                {"sleep": 30},
            ],
            adapter=self.oom_adapter(),
        )
        self.assertEqual(outcome.result.summary, "It is in parse.py")

    async def test_an_oom_kill_of_the_version_probe(self):
        # Codex review #258 (P2): the probe ended without a version it knows
        # because it was killed: out of memory, not a malformed answer.
        outcome = await self.run_node(
            [{"usage": [1, 1]}],
            adapter=self.oom_adapter(killed="--version"),
            version="0.163.0",
        )
        self.assertFalse(self.report.exists())  # app-server never started
        self.assertEqual(outcome.error_class, "AgentOutOfMemory")


class UnstoppedTest(CodexCase):
    """A CLI whose stop failed (the unit's ``systemctl stop``) may still run
    with the session's files mounted: its directory stays for the operator
    and the attempt is an adapter error (as Claude's)."""

    def stuck_adapter(self, launcher):
        return CodexSessionAdapter(
            launcher,
            drain_seconds=1.0,
            credentials=self.credentials,
            executable=(
                sys.executable,
                "-I",
                str(FAKE),
                "--scenario",
                str(self.scenario),
                "--report",
                str(self.report),
            ),
            verified_versions=frozenset({VERSION}),
        )

    async def test_a_cli_that_cannot_be_stopped_keeps_its_directory(self):
        class StuckLauncher(InteractiveSubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                if "app-server" not in launch.argv:
                    return process
                real_stop = process.stop

                async def stop():
                    await real_stop()  # (the test's process does end)
                    raise UnitNotStoppedError("the session's unit could not be stopped")

                process.stop = stop
                return process

        outcome = await self.run_node(
            [
                {"usage": [100, 10]},
                {"call": "submit", "arguments": {"summary": "s"}},
                {"sleep": 30},
            ],
            adapter=self.stuck_adapter(StuckLauncher()),
        )
        self.assertEqual(len(list(self.sessions.iterdir())), 1)  # kept
        # The service is told: the connection's session slot stays held.
        self.assertEqual(self.connections.cli_running, [True])
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error_class, "AdapterError")
        self.assertEqual(len(self.credentials.collected), 1)

    async def test_a_version_probe_that_cannot_be_stopped_keeps_its_directory(self):
        class StuckLauncher(InteractiveSubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                real_stop = process.stop

                async def stop():
                    await real_stop()
                    raise UnitNotStoppedError("the session's unit could not be stopped")

                process.stop = stop
                return process

        outcome = await self.run_node(
            [{"usage": [1, 1]}], adapter=self.stuck_adapter(StuckLauncher())
        )
        self.assertFalse(self.report.exists())  # app-server never started
        self.assertEqual(self.credentials.placed, [])
        self.assertEqual(len(list(self.sessions.iterdir())), 1)  # kept
        self.assertEqual(outcome.error_class, "AdapterError")
        # No login was placed: the connection's slot is not kept for it.
        self.assertEqual(self.connections.cli_running, [])

    async def test_a_stop_interrupted_by_cancellation_keeps_its_directory(self):
        stopping = asyncio.Event()

        class SlowStuckLauncher(InteractiveSubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                if "app-server" not in launch.argv:
                    return process
                real_stop = process.stop

                async def stop():
                    stopping.set()
                    await asyncio.sleep(0.5)
                    await real_stop()
                    raise UnitNotStoppedError("the session's unit could not be stopped")

                process.stop = stop
                return process

        running = asyncio.create_task(
            self.run_node(
                [
                    {"usage": [100, 10]},
                    {"call": "submit", "arguments": {"summary": "s"}},
                    {"sleep": 30},
                ],
                adapter=self.stuck_adapter(SlowStuckLauncher()),
            )
        )
        await asyncio.wait_for(stopping.wait(), 20)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(len(list(self.sessions.iterdir())), 1)  # kept

    async def test_a_start_whose_cleanup_cannot_stop_keeps_its_directory(self):
        class StartStuckLauncher(InteractiveSubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                if "app-server" not in launch.argv:
                    return process
                await process.stop()
                raise UnitNotStoppedError(
                    "the session's unit could not be stopped"
                ) from TimeoutError()

        outcome = await self.run_node(
            [{"sleep": 30}], adapter=self.stuck_adapter(StartStuckLauncher())
        )
        self.assertEqual(len(list(self.sessions.iterdir())), 1)  # kept
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error_class, "AdapterError")
        self.assertEqual(len(self.credentials.collected), 1)

    async def test_a_stopped_cli_still_removes_its_directory(self):
        await self.run_node([{"usage": [1, 1]}, {"complete": "completed"}])
        self.assertEqual(list(self.sessions.iterdir()), [])


class SlotBindingTest(CodexCase):
    """Decision 0096, 2 B: the session is recorded on the connection's slot
    before its login is placed; and its fencing: before the unit starts and
    before the write-back the session checks that it still holds the slot,
    and once that fails it gives up and leaves its directory (and the login
    in it) to the collection loop."""

    SUBMIT = [
        {"usage": [100, 10]},
        {"call": "submit", "arguments": {"summary": "s"}},
        {"sleep": 30},
    ]

    def bind(self, *, bound=True, held=()):
        self.events = []
        answers = list(held)

        async def bind_session(session):
            self.events.append(("bind", session, len(self.credentials.placed)))
            return bound

        async def still_held():
            self.events.append(("held", len(self.credentials.placed)))
            return answers.pop(0) if answers else True

        self.bind_session = bind_session
        self.still_held = still_held

    async def test_the_session_is_bound_before_its_login_is_placed(self):
        self.bind()
        outcome = await self.run_node(self.SUBMIT)
        self.assertEqual(outcome.result.summary, "s")
        (bind, *checks) = self.events
        self.assertEqual(bind[0], "bind")
        self.assertIsInstance(bind[1], uuid.UUID)
        self.assertEqual(bind[2], 0)  # nothing placed yet
        # Before the unit starts and before the write-back.
        self.assertEqual(checks, [("held", 1), ("held", 1)])
        self.assertEqual(len(self.credentials.collected), 1)
        self.assertEqual(list(self.sessions.iterdir()), [])

    async def test_a_session_whose_slot_cannot_be_bound_places_nothing(self):
        self.bind(bound=False)
        outcome = await self.run_node(self.SUBMIT)
        self.assertIsNone(outcome.result)
        self.assertEqual(self.connections.raw.failure, FailureCode.UNAVAILABLE)
        self.assertEqual(self.credentials.placed, [])
        self.assertFalse(self.report.exists())  # app-server never started
        self.assertEqual(self.connections.cli_running, [])
        self.assertEqual(list(self.sessions.iterdir()), [])

    async def test_a_slot_lost_before_the_start_starts_nothing(self):
        self.bind(held=[False])
        with self.assertLogs("paw_backend.agents.codex_cli", "ERROR") as logs:
            outcome = await self.run_node(self.SUBMIT)
        self.assertIsNone(outcome.result)
        self.assertEqual(self.connections.raw.failure, FailureCode.UNAVAILABLE)
        self.assertEqual(len(self.credentials.placed), 1)
        self.assertFalse(self.report.exists())  # app-server never started
        # Not collected here, not deleted: the collection loop's now.
        self.assertEqual(self.credentials.collected, [])
        (kept,) = list(self.sessions.iterdir())
        self.assertTrue((kept / "config" / "auth.json").exists())
        self.assertIn("left to the collection", "\n".join(logs.output))

    async def test_a_slot_lost_before_the_write_back_leaves_the_login(self):
        self.bind(held=[True, False])
        await self.run_node(self.SUBMIT)
        self.assertEqual(self.read_report()["args"][0], "app-server")
        self.assertEqual(self.credentials.collected, [])
        (kept,) = list(self.sessions.iterdir())
        self.assertTrue((kept / "config" / "auth.json").exists())


class BoundaryTest(CodexCase):
    """An exception of the session's setup or launcher is classified at the
    runtime's boundary (``ErrorClassifier.from_error``), as Claude's and the
    local runtime's are: it does not escape as its own class."""

    def missing_adapter(self):
        missing = str(self.base / "no-such-codex")
        return CodexSessionAdapter(
            InteractiveSubprocessLauncher(),
            credentials=self.credentials,
            executable=(missing,),
            verified_versions=frozenset({VERSION}),
        )

    async def test_a_missing_executable_is_an_adapter_error(self):
        outcome = await self.run_node([], adapter=self.missing_adapter())
        self.assertEqual(outcome.error_class, "AdapterError")
        self.assertEqual(outcome.message, "adapter_error")
        self.assertEqual(list(self.sessions.iterdir()), [])

    async def test_a_session_directory_that_cannot_be_made_is_an_adapter_error(self):
        self.sessions.rmdir()  # create_session's base is gone
        outcome = await self.run_node([])
        self.assertEqual(outcome.error_class, "AdapterError")

    async def test_the_fallback_path_is_classified_too(self):
        self.sessions.rmdir()
        outcome = await self.run_node([], fallback=True)
        self.assertEqual(outcome.error_class, "AdapterError")

    async def test_a_cancellation_is_not_classified(self):
        started = asyncio.Event()

        class Hanging(InteractiveSubprocessLauncher):
            async def start(self, launch):
                started.set()
                await asyncio.sleep(60)

        running = asyncio.create_task(
            self.run_node([], adapter=self.stuck_adapter_for(Hanging()))
        )
        await asyncio.wait_for(started.wait(), 20)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

    def stuck_adapter_for(self, launcher):
        return CodexSessionAdapter(
            launcher,
            credentials=self.credentials,
            executable=("codex",),
            verified_versions=frozenset({VERSION}),
        )


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
