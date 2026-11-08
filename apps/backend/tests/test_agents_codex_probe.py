"""The Codex connection's health probe (Decision 0100, 10 and 11; Decision
0102, 1; Decision 0101; Decision 0096): the real adapter, launcher and login
file with a fake ``codex`` (``fake_codex_cli.py``) and the service's slot hooks
as doubles (``ProbeSession``). The service's side (the slot, the write-back's
compare-and-swap) is ``test_connections_codex_probe``.

* outside the connection's slot nothing starts;
* an unverified version starts nothing with the login;
* the session is bound before the login is placed; the config has no MCP
  server and every tool feature off; the fixed prompts (Claude's, Decision
  0102, 1) are the system prompt and the task;
* the verdicts: an answer with ``PAW_PROBE_OK`` after one model call is
  ``connected``; ``unauthorized`` is ``expired``; a usage limit, an answer
  without the token, a second model call, a tool call, a request from the
  server, a failed turn and a CLI that ended without its turn's end are
  ``unavailable``;
* a refreshed login is written back after the CLI was stopped, also past the
  deadline; the directory goes once the slot was given back; a slot that was
  lost or a CLI that could not be stopped keeps the status and the files;
* the credential is in no argument, environment, config or log.
"""

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import tomllib
import unittest
import uuid
from pathlib import Path

from paw_backend.agents.claude_probe import PROBE_PROMPT, PROBE_SYSTEM_PROMPT
from paw_backend.agents.cli_session import UnitNotStoppedError
from paw_backend.agents.codex_app_server import CodexStream, StreamRefused
from paw_backend.agents.codex_cli import (
    DISABLED_FEATURES,
    InteractiveSubprocessLauncher,
    probe_config,
)
from paw_backend.agents.codex_credentials import CodexAuthFile
from paw_backend.agents.codex_probe import CodexProbeAdapter
from paw_backend.connections import (
    AdapterFailure,
    AdapterRegistry,
    AdapterRequest,
    ConnectionKind,
    FailureCode,
    ProbeOutcome,
    ProbeSession,
    Secret,
    WriteBack,
)

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_codex_cli.py"
VERSION = "0.162.1"
MODEL = "gpt-test-codex"
# A login built from pieces (no literal a scanner would flag).
LOGIN = '{"tokens": {"access_token": "paw-codex-probe-' + "c4na" + 'ry"}}'
REFRESHED = '{"tokens": {"access_token": "paw-codex-probe-' + "r3fr" + 'esh"}}'
GOOD = [
    {"answer": "PAW_PROBE_OK."},
    {"usage": [120, 4]},
    {"complete": "completed"},
]


class Hooks:
    """The service's side of a probe in the connection's slot."""

    def __init__(self, *, bound=True, held=True):
        self.calls = []
        self.bound = bound
        self.held = held  # True, False, or a list of answers in turn
        self.written = []
        self.kept = False

    async def bind(self, session):
        self.calls.append(("bind", session))
        return self.bound

    async def still_held(self):
        if isinstance(self.held, list):
            answer = self.held.pop(0) if self.held else True
        else:
            answer = self.held
        self.calls.append(("held", answer))
        return answer

    async def write_back(self, value):
        self.written.append(value)
        return WriteBack.WRITTEN

    def cli_running(self):
        self.kept = True

    def session(self):
        return ProbeSession(
            bind_session=self.bind,
            still_held=self.still_held,
            write_back=self.write_back,
            cli_running=self.cli_running,
            connection_id=uuid.uuid4(),
        )


class StuckLauncher(InteractiveSubprocessLauncher):
    """``app-server``'s unit cannot be stopped (the version check's can)."""

    async def start(self, launch):
        process = await super().start(launch)
        if "app-server" in launch.argv:
            real_stop = process.stop

            async def stop():
                await real_stop()
                raise UnitNotStoppedError("the session's unit could not be stopped")

            process.stop = stop
        return process


class ProbeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.report = self.base / "report.json"
        self.scenario = self.base / "scenario.json"
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()

    def adapter(self, *, verified=frozenset({VERSION}), launcher=None):
        return CodexProbeAdapter(
            launcher or InteractiveSubprocessLauncher(),
            model=MODEL,
            session_base=self.sessions,
            credentials=CodexAuthFile(),
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

    async def probe(self, actions, *, hooks=None, adapter=None, release=True, **extra):
        self.scenario.write_text(json.dumps({"actions": actions, **extra}))
        self.hooks = hooks or Hooks()
        session = self.hooks.session()
        try:
            return await (adapter or self.adapter()).check_health(
                Secret(LOGIN), session
            )
        finally:
            if release is not None:
                session.released(release)

    def sent(self):
        return json.loads(self.report.read_text())

    def directories(self):
        return list(self.sessions.iterdir())

    def version_checked(self):
        return Path(str(self.report) + ".probes").exists()


class LaunchTest(ProbeCase):
    async def test_the_cli_runs_without_any_tool_and_the_fixed_prompts(self):
        report = await self.probe(GOOD)
        self.assertEqual(report.outcome, ProbeOutcome.CONNECTED)
        sent = self.sent()
        self.assertEqual(sent["args"], ["app-server", "--listen", "stdio://"])
        self.assertNotIn("mcp_servers", sent["config"])
        self.assertEqual(sent["listed"], [])
        self.assertEqual(
            sent["config"]["features"], {name: False for name in DISABLED_FEATURES}
        )
        self.assertEqual(sent["config"]["forced_login_method"], "chatgpt")
        self.assertEqual(sent["config"]["web_search"], "disabled")
        start = sent["requests"]["thread/start"]
        self.assertEqual(start["baseInstructions"], PROBE_SYSTEM_PROMPT)
        self.assertEqual(start["model"], MODEL)
        self.assertEqual(start["environments"], [])
        self.assertEqual(
            (start["approvalPolicy"], start["sandbox"], start["ephemeral"]),
            ("never", "read-only", True),
        )
        turn = sent["requests"]["turn/start"]
        self.assertEqual(turn["input"], [{"type": "text", "text": PROBE_PROMPT}])
        self.assertEqual(sent["auth_length"], len(LOGIN))
        self.assertEqual(
            [name for name in sent["env"] if name.startswith(("CODEX", "HOME"))],
            ["CODEX_HOME", "HOME"],
        )
        self.assertNotIn("paw-codex-probe", json.dumps(sent["args"]))

    def test_the_probe_config_is_a_session_config_without_a_server(self):
        config = tomllib.loads(probe_config())
        self.assertNotIn("mcp_servers", config)
        self.assertEqual(
            config["features"], {name: False for name in DISABLED_FEATURES}
        )
        self.assertEqual(config["orchestrator"]["skills"], {"enabled": False})
        self.assertEqual(config["tools"]["update_plan"], {"enabled": False})

    async def test_the_session_is_bound_before_the_login_is_placed(self):
        await self.probe(GOOD)
        kinds = [call[0] for call in self.hooks.calls]
        self.assertEqual(kinds[0], "bind")
        # The checks before the config's login, before the unit, and before
        # the write-back's read.
        self.assertEqual(kinds[1:], ["held", "held", "held"])
        self.assertIsInstance(self.hooks.calls[0][1], uuid.UUID)

    async def test_the_directory_goes_once_the_slot_was_given_back(self):
        self.scenario.write_text(json.dumps({"actions": GOOD}))
        hooks = Hooks()
        session = hooks.session()
        await self.adapter().check_health(Secret(LOGIN), session)
        self.assertEqual(len(self.directories()), 1)  # until the release
        session.released(True)
        self.assertEqual(self.directories(), [])

    async def test_a_slot_not_given_back_leaves_the_files_to_the_collection(self):
        with self.assertLogs("paw_backend.agents.codex_probe", logging.ERROR):
            await self.probe(GOOD, release=False)
        self.assertEqual(len(self.directories()), 1)

    async def test_outside_the_slot_nothing_starts(self):
        self.scenario.write_text(json.dumps({"actions": GOOD}))
        report = await self.adapter().check_health(Secret(LOGIN))
        self.assertEqual(
            (report.outcome, report.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE),
        )
        self.assertFalse(report.keeps_status)
        self.assertEqual(self.directories(), [])
        self.assertFalse(self.version_checked())

    async def test_an_unverified_version_places_no_login(self):
        report = await self.probe(GOOD, adapter=self.adapter(verified=frozenset()))
        self.assertEqual(
            (report.outcome, report.failure),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE),
        )
        self.assertFalse(self.report.exists())  # app-server never started
        self.assertEqual(self.hooks.calls, [])  # not even bound
        self.assertEqual(self.directories(), [])

    async def test_run_starts_nothing(self):
        with self.assertRaises(AdapterFailure) as raised:
            await self.adapter().run(Secret(LOGIN), AdapterRequest(MODEL, "x", 5))
        self.assertIs(raised.exception.code, FailureCode.INTERNAL_ERROR)
        self.assertEqual(self.directories(), [])

    def test_it_registers_as_the_codex_connections_exclusive_adapter(self):
        registry = AdapterRegistry()
        self.assertIs(registry.register(self.adapter()), ConnectionKind.CODEX)
        self.assertIs(CodexProbeAdapter.exclusive_sessions, True)
        self.assertNotIn("paw-codex", repr(self.adapter()))


class VerdictTest(ProbeCase):
    async def test_an_answer_with_the_token_is_connected_with_its_tokens(self):
        report = await self.probe(GOOD)
        self.assertEqual(
            (report.outcome, report.failure, report.input_tokens, report.output_tokens),
            (ProbeOutcome.CONNECTED, None, 120, 4),
        )
        self.assertEqual(report.model, MODEL)
        self.assertFalse(report.keeps_status)

    async def test_an_authentication_failure_is_expired(self):
        for actions in (
            [{"error": "unauthorized"}, {"sleep": 30}],
            [{"complete": "failed", "turn_error": "unauthorized"}],
        ):
            with self.subTest(actions=actions):
                report = await self.probe(actions)
                self.assertEqual(
                    (report.outcome, report.failure),
                    (ProbeOutcome.EXPIRED, FailureCode.EXPIRED),
                )

    async def test_the_other_endings_are_unavailable(self):
        resource_call = {
            "item": {
                "type": "mcpToolCall",
                "id": "r",
                "server": "paw",
                "tool": "list_mcp_resources",
            }
        }
        cases = {
            "rate": (
                [{"complete": "failed", "turn_error": "usageLimitExceeded"}],
                FailureCode.RATE_LIMITED,
            ),
            "retried error then rate": (
                [
                    {"error": "unauthorized", "will_retry": True},
                    {"complete": "failed", "turn_error": "rateLimitExceeded"},
                ],
                FailureCode.RATE_LIMITED,
            ),
            "no token": (
                [{"answer": "OK"}, {"usage": [1, 1]}, {"complete": "completed"}],
                FailureCode.INVALID_RESPONSE,
            ),
            "no answer": (
                [{"usage": [1, 1]}, {"complete": "completed"}],
                FailureCode.INVALID_RESPONSE,
            ),
            "two calls": (
                [{"usage": [1, 1]}, {"usage": [1, 1]}, *GOOD],
                FailureCode.INVALID_RESPONSE,
            ),
            "no call reported": (
                [{"answer": "PAW_PROBE_OK"}, {"complete": "completed"}],
                FailureCode.INVALID_RESPONSE,
            ),
            "a resource call": ([resource_call, *GOOD], FailureCode.INVALID_RESPONSE),
            "a command": (
                [{"item": {"type": "commandExecution", "id": "c"}}, *GOOD],
                FailureCode.INVALID_RESPONSE,
            ),
            "a request": (
                [{"request": "item/tool/call"}],
                FailureCode.INVALID_RESPONSE,
            ),
            "failed turn": (
                [{"complete": "failed", "turn_error": "internalServerError"}],
                FailureCode.INTERNAL_ERROR,
            ),
            "ended early": (
                [{"usage": [1, 1]}, {"exit": 1}],
                FailureCode.INTERNAL_ERROR,
            ),
        }
        for name, (actions, failure) in cases.items():
            with self.subTest(name):
                report = await self.probe(actions)
                self.assertEqual(
                    (report.outcome, report.failure),
                    (ProbeOutcome.UNAVAILABLE, failure),
                )
                self.assertFalse(report.keeps_status)

    async def test_a_reroute_is_logged_and_the_verdict_is_unchanged(self):
        with self.assertLogs("paw_backend.agents.codex_probe", logging.WARNING) as logs:
            report = await self.probe([{"reroute": [MODEL, "gpt-older"]}, *GOOD])
        self.assertEqual(report.outcome, ProbeOutcome.CONNECTED)
        (line,) = [r.getMessage() for r in logs.records if "reroute" in r.getMessage()]
        self.assertIn(f"configured={MODEL} from={MODEL} to=gpt-older", line)


class LoginTest(ProbeCase):
    async def test_a_refreshed_login_is_written_back(self):
        await self.probe([{"refresh": REFRESHED}, *GOOD])
        self.assertEqual(self.hooks.written, [REFRESHED])

    async def test_an_unchanged_login_is_not_written_back(self):
        await self.probe(GOOD)
        self.assertEqual(self.hooks.written, [])

    async def test_a_probe_past_its_deadline_stops_the_cli_and_writes_back(self):
        self.scenario.write_text(
            json.dumps({"actions": [{"refresh": REFRESHED}, {"sleep": 30}]})
        )
        hooks = Hooks()
        session = hooks.session()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(3):
                await self.adapter().check_health(Secret(LOGIN), session)
        self.assertLess(loop.time() - started, 15)
        self.assertEqual(hooks.written, [REFRESHED])
        session.released(True)
        self.assertEqual(self.directories(), [])

    async def test_a_slot_that_could_not_be_bound_places_nothing(self):
        report = await self.probe(GOOD, hooks=Hooks(bound=False))
        self.assertEqual(
            (report.outcome, report.failure, report.keeps_status),
            (ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE, True),
        )
        self.assertFalse(self.report.exists())
        self.assertEqual(self.directories(), [])  # not bound: nobody collects

    async def test_a_slot_lost_before_the_unit_leaves_the_files(self):
        with self.assertLogs("paw_backend.agents.codex_probe", logging.ERROR):
            report = await self.probe(GOOD, hooks=Hooks(held=[True, False]))
        self.assertEqual(
            (report.outcome, report.failure, report.keeps_status),
            (ProbeOutcome.UNAVAILABLE, FailureCode.UNAVAILABLE, True),
        )
        self.assertFalse(self.report.exists())  # the unit was not started
        self.assertEqual(self.hooks.written, [])  # not collected either
        self.assertEqual(len(self.directories()), 1)  # the collection's

    async def test_a_cli_that_could_not_be_stopped_keeps_the_slot_and_files(self):
        with self.assertLogs("paw_backend.agents.codex_probe", logging.ERROR):
            report = await self.probe(
                [{"refresh": REFRESHED}, *GOOD],
                adapter=self.adapter(launcher=StuckLauncher()),
            )
        self.assertEqual(
            (report.outcome, report.failure, report.keeps_status),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INTERNAL_ERROR, True),
        )
        self.assertTrue(self.hooks.kept)
        self.assertEqual(self.hooks.written, [REFRESHED])  # read back at once
        self.assertEqual(len(self.directories()), 1)

    async def test_a_deadline_during_a_stop_that_fails_marks_the_session(self):
        # Codex review of #336: the cancellation passes, the service still
        # learns the CLI could not be stopped.
        class SlowStuckLauncher(InteractiveSubprocessLauncher):
            async def start(self, launch):
                process = await super().start(launch)
                if "app-server" in launch.argv:
                    real_stop = process.stop

                    async def stop():
                        await real_stop()
                        await asyncio.sleep(3)
                        raise UnitNotStoppedError("the unit could not be stopped")

                    process.stop = stop
                return process

        self.scenario.write_text(json.dumps({"actions": GOOD}))
        hooks = Hooks()
        session = hooks.session()
        with self.assertLogs("paw_backend", logging.ERROR):
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(2):
                    await self.adapter(launcher=SlowStuckLauncher()).check_health(
                        Secret(LOGIN), session
                    )
            session.released(True)
        self.assertTrue(session.unstopped)
        self.assertTrue(hooks.kept)
        self.assertEqual(len(self.directories()), 1)

    async def test_the_login_is_in_no_log(self):
        with self.assertLogs("paw_backend", logging.DEBUG) as logs:
            await self.probe([{"refresh": REFRESHED}, *GOOD])
        text = "\n".join(r.getMessage() for r in logs.records)
        self.assertIn("cli=codex-probe", text)
        self.assertNotIn("paw-codex-probe", text)


class StreamTest(unittest.TestCase):
    def feed(self, stream, *messages):
        for message in messages:
            stream.feed(json.dumps(message))

    def started(self, probe=True):
        stream = CodexStream((), frozenset({VERSION}), probe=probe)
        self.feed(
            stream,
            {"id": 1, "result": {"userAgent": f"x/{VERSION} (linux)"}},
            {"id": 2, "result": {"thread": {"id": "t"}}},
        )
        return stream

    def test_a_probe_stream_refuses_every_tool_call(self):
        for tool in ("list_mcp_resources", "submit"):
            with self.subTest(tool), self.assertRaises(StreamRefused) as refused:
                self.feed(
                    self.started(),
                    {
                        "method": "item/started",
                        "params": {
                            "item": {
                                "type": "mcpToolCall",
                                "id": "i",
                                "server": "paw",
                                "tool": tool,
                            }
                        },
                    },
                )
            self.assertEqual(refused.exception.reason, "item")
        with self.assertRaises(ValueError):
            CodexStream({"submit"}, probe=True)

    def test_only_a_probe_stream_keeps_the_answer(self):
        completed = {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": "a", "text": "hi"}},
        }
        probe, node = self.started(), self.started(probe=False)
        self.feed(probe, completed)
        self.feed(node, completed)
        self.assertEqual((probe.answer, node.answer), ("hi", None))

    def test_a_reroute_keeps_model_names_only(self):
        stream = self.started()
        self.feed(
            stream,
            {
                "method": "model/rerouted",
                "params": {"fromModel": "gpt-a", "toModel": "not a model name"},
            },
        )
        self.assertEqual(stream.reroutes, [("gpt-a", "?")])

    def test_the_last_unretried_error_is_known(self):
        stream = self.started()
        self.feed(
            stream,
            {
                "method": "error",
                "params": {"willRetry": True, "error": {"codexErrorInfo": "x"}},
            },
        )
        self.assertIsNone(stream.error)
        self.feed(
            stream,
            {
                "method": "error",
                "params": {"error": {"codexErrorInfo": "unauthorized"}},
            },
        )
        self.assertEqual(stream.error, "unauthorized")


if __name__ == "__main__":
    unittest.main()
