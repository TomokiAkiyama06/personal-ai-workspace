"""The production path of a CLI session (Decision 0083, 6): the template
unit's program and the backend's ``SystemdLauncher``. A fake ``podman`` and a
fake ``systemctl`` on ``PATH``; no container, no systemd, no other account.

* the program refuses a session that is not a UUID, an image without its
  digest, an environment name it does not know and a credential file of any
  other form, before starting anything;
* it runs Podman with no network, a read-only root, no capabilities, the
  session's directory at its own path, the egress socket and the credential
  as an env file, the prompt as stdin and the stream socket as stdout;
* the launcher writes the session's files (the credential in its own file,
  mode 0640, never in an argument), starts and stops exactly
  ``paw-cli-session@<uuid>.service``, and reads the CLI's stream.
"""

import asyncio
import json
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import uuid
from pathlib import Path

from paw_backend.agents.cli_session import (
    CliLaunch,
    SystemdLauncher,
    UnitNotStoppedError,
    create_session,
    stop_through_cancellation,
)
from paw_backend.connections import Secret

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "cli-session"
RUNNER = DEPLOY / "paw_cli_session_run.py"
TOKEN = "paw-claude-canary-" + "5e1d" + "-token"
DIGEST = "localhost/paw-claude-cli@sha256:" + "ab" * 32


def fake_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o755)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = self.base / "run"
        self.root.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.record = self.base / "podman.json"
        fake_tool(
            self.bin,
            "podman",
            f"""
            import json, sys
            data = sys.stdin.read()
            record = open({str(self.record)!r}, "w")
            json.dump({{"argv": sys.argv[1:], "stdin": data}}, record)
            sys.stdout.write('{{"type": "result"}}\\n')
            """,
        )
        self.config = self.base / "cli-session.toml"
        self.write_config(DIGEST)
        self.session = str(uuid.uuid4())
        self.directory = self.root / self.session
        self.directory.mkdir()
        (self.directory / "launch.json").write_text(
            json.dumps(
                {
                    "argv": ["/opt/paw/start-claude", "-p", "--tools", ""],
                    "env": {"HTTPS_PROXY": "http://127.0.0.1:3128"},
                }
            )
        )
        (self.directory / "credential.env").write_text(
            f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n"
        )
        (self.directory / "prompt").write_text("the task")

    def write_config(self, image):
        self.config.write_text(
            f'root = "{self.root}"\nimage = "{image}"\n'
            'egress_socket = "/run/paw-egress/egress.sock"\n'
        )

    def run_runner(self, session=None):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = self.directory / "stream.sock"
        server.bind(str(path))
        server.listen(1)
        received = []

        def accept():
            server.settimeout(10)
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                while data := connection.recv(65536):
                    received.append(data)

        thread = threading.Thread(target=accept)
        thread.start()
        env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(RUNNER),
                session or self.session,
                "--config",
                str(self.config),
            ],
            env=env,
            capture_output=True,
            timeout=30,
        )
        # Wake the accept of a runner that never connected (it refused).
        try:
            server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        server.close()
        thread.join(15)
        return result, b"".join(received)

    def test_the_container_is_started_as_decided(self):
        result, stream = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stream, b'{"type": "result"}\n')
        record = json.loads(self.record.read_text())
        argv = record["argv"]
        self.assertEqual(record["stdin"], "the task")
        self.assertEqual(argv[:2], ["run", "--rm"])
        for pair in (
            ["--network", "none"],
            ["--cap-drop", "all"],
            ["--security-opt", "no-new-privileges"],
            ["--userns", "keep-id"],
            ["--env-file", str(self.directory / "credential.env")],
            ["--env", "HTTPS_PROXY=http://127.0.0.1:3128"],
            ["--env", f"PAW_SESSION_ID={self.session}"],
            ["--volume", f"{self.directory}:{self.directory}:rw"],
            ["--volume", "/run/paw-egress/egress.sock:/run/paw-egress.sock"],
        ):
            with self.subTest(pair=pair):
                index = argv.index(
                    pair[0], *([argv.index(pair[1]) - 1] if pair[1] in argv else [])
                )
                self.assertEqual(argv[index : index + 2], pair)
        self.assertIn("--read-only", argv)
        image = argv.index(DIGEST)
        self.assertEqual(
            argv[image + 1 :], ["/opt/paw/start-claude", "-p", "--tools", ""]
        )
        self.assertNotIn(TOKEN, json.dumps(argv))

    def test_what_it_refuses_starts_nothing(self):
        cases = {
            "not a uuid": lambda: None,
            "an image without its digest": lambda: self.write_config(
                "localhost/paw-claude-cli:latest"
            ),
            "an unknown environment name": lambda: (
                self.directory / "launch.json"
            ).write_text(json.dumps({"argv": ["x"], "env": {"LD_PRELOAD": "x"}})),
            "a second credential line": lambda: (
                self.directory / "credential.env"
            ).write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\nOTHER=1\n"),
            "another credential name": lambda: (
                self.directory / "credential.env"
            ).write_text("ANTHROPIC_API_KEY=x\n"),
        }
        for name, prepare in cases.items():
            with self.subTest(name):
                self.setUp()
                prepare()
                session = "../etc" if name == "not a uuid" else None
                result, _ = self.run_runner(session)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.record.exists())
                self.assertNotIn(TOKEN.encode(), result.stderr)


class LauncherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.sessions = self.base / "run"
        self.sessions.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.calls = self.base / "calls.txt"
        # A fake systemctl: "start" runs a fake unit program in the
        # background (it connects to the session's stream socket, writes two
        # lines and ends); "show" answers from a state file; "stop" kills it.
        fake_tool(
            self.bin,
            "systemctl",
            f"""
            import os, subprocess, sys
            root, calls = {str(self.sessions)!r}, {str(self.calls)!r}
            args = sys.argv[1:]
            open(calls, "a").write(" ".join(args) + "\\n")
            unit = args[-1]
            session = unit.split("@", 1)[1].rsplit(".", 1)[0]
            state = os.path.join(root, session + ".state")
            if args[0] == "start":
                import time
                delay = os.path.join(root, "start_delay")
                if os.path.exists(delay):
                    time.sleep(float(open(delay).read()))
                open(calls, "a").write("queued " + unit + "\\n")
                program = (
                    "import socket, sys, time, os;"
                    "s = socket.socket(socket.AF_UNIX);"
                    f"s.connect({{os.path.join(root, session, 'stream.sock')!r}});"
                    "s.sendall(b'line one\\\\nline two\\\\n');"
                    "time.sleep(float(open(sys.argv[1]).read() or 0));"
                    f"open({{state!r}}, 'w').write('inactive 0')"
                )
                hold = os.path.join(root, "hold")
                child = subprocess.Popen(
                    [sys.executable, "-c", program, hold],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                open(state, "w").write(f"active {{child.pid}}")
            elif args[0] == "show":
                text = ["inactive", "0"]
                if os.path.exists(state):
                    text = open(state).read().split()
                if "ActiveState" in args:
                    print(text[0])
                else:
                    print(text[1] if text[0] == "inactive" else "")
            elif args[0] == "stop":
                if os.path.exists(os.path.join(root, "stop_fails")):
                    sys.exit(1)
                # (a unit that never ran is stopped already)
                text = ["inactive", "0"]
                if os.path.exists(state):
                    text = open(state).read().split()
                if text[0] == "active":
                    import signal
                    os.kill(int(text[1]), signal.SIGTERM)
                open(state, "w").write("inactive 143")
            """,
        )
        (self.sessions / "hold").write_text("0")

    def launcher(self):
        return SystemdLauncher(
            systemctl=(str(self.bin / "systemctl"),), settle_seconds=5
        )

    async def launch(self):
        spec = create_session(self.sessions, ("python3", "relay.py"))
        launch = CliLaunch(
            ("/opt/paw/start-claude", "-p"),
            {"HTTPS_PROXY": "http://127.0.0.1:3128"},
            Secret(TOKEN),
            "CLAUDE_CODE_OAUTH_TOKEN",
            b"the task",
            spec,
        )
        return spec, await self.launcher().start(launch)

    async def test_the_session_files_and_the_stream(self):
        spec, process = await self.launch()
        lines = [await process.stdout.readline(), await process.stdout.readline()]
        self.assertEqual(lines, [b"line one\n", b"line two\n"])
        self.assertEqual(await process.stdout.readline(), b"")
        self.assertEqual(await process.wait(), 0)
        credential = spec.directory / "credential.env"
        self.assertEqual(credential.read_text(), f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o640)
        self.assertEqual((spec.directory / "prompt").read_bytes(), b"the task")
        launch = json.loads((spec.directory / "launch.json").read_text())
        self.assertEqual(launch["argv"], ["/opt/paw/start-claude", "-p"])
        self.assertNotIn(TOKEN, json.dumps(launch))
        calls = self.calls.read_text()
        self.assertIn(
            f"start --no-block paw-cli-session@{spec.session_id}.service", calls
        )
        self.assertNotIn(TOKEN, calls)

    async def test_stop_stops_exactly_the_sessions_unit(self):
        (self.sessions / "hold").write_text("30")
        spec, process = await self.launch()
        await process.stdout.readline()
        await process.stop()
        self.assertEqual(await process.wait(), 143)
        self.assertIn(
            f"stop paw-cli-session@{spec.session_id}.service", self.calls.read_text()
        )

    async def test_a_refused_stop_is_raised(self):
        (self.sessions / "hold").write_text("30")
        spec, process = await self.launch()
        await process.stdout.readline()
        (self.sessions / "stop_fails").write_text("")
        with self.assertRaises(OSError):
            await process.stop()
        (self.sessions / "stop_fails").unlink()
        await process.stop()

    async def test_a_start_cancelled_during_systemctl_stops_the_unit(self):
        (self.sessions / "start_delay").write_text("1")
        spec = create_session(self.sessions, ("python3", "relay.py"))
        launch = CliLaunch(
            ("x",), {}, Secret(TOKEN), "CLAUDE_CODE_OAUTH_TOKEN", b"", spec
        )
        starting = asyncio.create_task(self.launcher().start(launch))
        await asyncio.sleep(0.3)  # systemctl start is running
        starting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await starting
        calls = self.calls.read_text()
        unit = f"paw-cli-session@{spec.session_id}.service"
        self.assertIn(f"stop {unit}", calls)
        # The start that was running finished first: the stop came after it.
        self.assertLess(calls.index(f"queued {unit}"), calls.index(f"stop {unit}"))

    async def test_a_hung_systemctl_does_not_hold_a_cancellation(self):
        # systemctl start hangs (D-Bus or polkit not answering): the
        # cancellation of the session's deadline is not held past the bound;
        # the hung systemctl is killed and the unit is stopped before the
        # cancellation goes on.
        (self.sessions / "start_delay").write_text("60")
        spec = create_session(self.sessions, ("python3", "relay.py"))
        launch = CliLaunch(
            ("x",), {}, Secret(TOKEN), "CLAUDE_CODE_OAUTH_TOKEN", b"", spec
        )
        launcher = SystemdLauncher(
            systemctl=(str(self.bin / "systemctl"),),
            settle_seconds=5,
            systemctl_seconds=1,
        )
        starting = asyncio.create_task(launcher.start(launch))
        await asyncio.sleep(0.3)  # systemctl start is running (and hangs)
        starting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(starting), 10)
        unit = f"paw-cli-session@{spec.session_id}.service"
        self.assertIn(f"stop {unit}", self.calls.read_text())

    async def test_a_hung_systemctl_is_a_failed_call(self):
        (self.sessions / "start_delay").write_text("60")
        launcher = SystemdLauncher(
            systemctl=(str(self.bin / "systemctl"),), systemctl_seconds=1
        )
        code, _ = await asyncio.wait_for(
            launcher.systemctl("start", "--no-block", "paw-cli-session@x.service"),
            10,
        )
        self.assertNotEqual(code, 0)

    async def test_a_failed_stop_after_a_failed_start_is_raised(self):
        (self.sessions / "start_delay").write_text("1")
        (self.sessions / "stop_fails").write_text("")
        spec = create_session(self.sessions, ("python3", "relay.py"))
        launch = CliLaunch(
            ("x",), {}, Secret(TOKEN), "CLAUDE_CODE_OAUTH_TOKEN", b"", spec
        )
        starting = asyncio.create_task(self.launcher().start(launch))
        await asyncio.sleep(0.3)
        starting.cancel()
        with self.assertRaises(OSError) as caught:
            await starting
        self.assertIsInstance(caught.exception, UnitNotStoppedError)
        self.assertIsInstance(caught.exception.__cause__, asyncio.CancelledError)
        (self.sessions / "stop_fails").unlink()
        await self.launcher().stop_unit(f"paw-cli-session@{spec.session_id}.service")

    def test_the_session_directory_keeps_the_parents_setgid(self):
        shared = self.base / "shared"
        shared.mkdir()
        shared.chmod(0o2750)
        spec = create_session(shared, ("python3", "relay.py"))
        mode = spec.directory.stat().st_mode
        self.assertTrue(mode & stat.S_ISGID)
        self.assertEqual(stat.S_IMODE(mode) & 0o777, 0o750)
        plain = create_session(self.sessions, ("python3", "relay.py"))
        self.assertFalse(plain.directory.stat().st_mode & stat.S_ISGID)

    async def test_a_credential_that_is_not_one_line_is_refused(self):
        spec = create_session(self.sessions, ("python3", "relay.py"))
        launch = CliLaunch(
            ("x",),
            {},
            Secret("two\nlines"),
            "CLAUDE_CODE_OAUTH_TOKEN",
            b"",
            spec,
        )
        with self.assertRaises(ValueError):
            await self.launcher().start(launch)
        self.assertFalse(self.calls.exists())
        self.assertFalse((spec.directory / "credential.env").exists())


class StopThroughCancellationTest(unittest.IsolatedAsyncioTestCase):
    """A cancellation of the caller is never lost, also when it comes in the
    same turn of the event loop as the end of the stop."""

    class Process:
        def __init__(self):
            self.ended = asyncio.get_running_loop().create_future()

        async def stop(self):
            await self.ended

    async def stopped_then_cancelled(self, end):
        process = self.Process()
        stopping = asyncio.create_task(stop_through_cancellation(process))
        await asyncio.sleep(0)  # the stop is running
        end(process.ended)
        stopping.cancel()  # in the same turn as the stop's end
        return await stopping

    async def test_a_cancellation_with_a_stop_that_ended_is_kept(self):
        failed, cancelled = await self.stopped_then_cancelled(
            lambda ended: ended.set_result(None)
        )
        self.assertIsNone(failed)
        self.assertTrue(cancelled)

    async def test_a_cancellation_with_a_stop_that_failed_is_kept(self):
        failed, cancelled = await self.stopped_then_cancelled(
            lambda ended: ended.set_exception(UnitNotStoppedError("not stopped"))
        )
        self.assertIsInstance(failed, UnitNotStoppedError)
        self.assertTrue(cancelled)

    async def test_a_cancelled_stop_is_a_failed_stop(self):
        failed, cancelled = await self.stopped_then_cancelled(
            lambda ended: ended.cancel()
        )
        self.assertIsInstance(failed, OSError)
        self.assertTrue(cancelled)


class StopCancelledInsideTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_stop_cancelled_inside_is_not_a_cancellation_of_the_caller(
        self,
    ):
        process = StopThroughCancellationTest.Process()
        stopping = asyncio.create_task(stop_through_cancellation(process))
        await asyncio.sleep(0)
        process.ended.cancel()  # the stop itself ends cancelled
        failed, cancelled = await stopping
        self.assertIsInstance(failed, OSError)
        self.assertFalse(cancelled)


class DeployFilesTest(unittest.TestCase):
    def test_the_template_unit_and_the_polkit_rule(self):
        unit = (DEPLOY / "paw-cli-session@.service").read_text()
        for line in (
            "User=paw-agent-cli",
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "RestrictSUIDSGID=yes",
            "TimeoutStopSec=5s",
            "RuntimeDirectory=paw-cli-session/%i",
            "Delegate=yes",
            "Environment=XDG_RUNTIME_DIR=/run/paw-cli-session/%i",
        ):
            with self.subTest(line=line):
                self.assertIn(f"\n{line}\n", unit)
        # ProtectHome=yes hides /run/user: Podman's runtime directory is the
        # unit's own, and nothing under /run/user is asked for.
        settings = [line for line in unit.splitlines() if not line.startswith("#")]
        self.assertFalse([line for line in settings if "/run/user" in line])
        rule = (DEPLOY / "50-paw-cli-session.rules").read_text()
        self.assertIn('subject.user !== "paw"', rule)
        self.assertIn('verb === "start" || verb === "stop"', rule)
        self.assertIn(
            "^paw-cli-session@[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.service$",
            rule,
        )


if __name__ == "__main__":
    unittest.main()
