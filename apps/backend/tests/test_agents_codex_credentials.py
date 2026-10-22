"""Codex's ChatGPT login in a session (``CodexAuthFile``; Decision 0083, 6;
issue #239): the real runtime, adapter, bridge and relay, the fake ``codex``
(which saves a refreshed login in place, as ``rust-v0.162.1`` does) and the
fake connection service of ``test_agents_codex_cli``.

* ``place`` writes ``auth.json`` as a new file with mode 0660, whatever the
  umask, and refuses a credential that is not a JSON object;
* an unchanged login is not written back; one the CLI refreshed is, once,
  after the CLI stopped (also when the turn failed);
* a cancellation of the attempt still writes the refreshed login back before
  it passes;
* what the write-back came to is logged, the login itself never.
"""

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from paw_backend.agents.codex_credentials import AUTH_NAME, CodexAuthFile
from paw_backend.connections import Secret, WriteBack

from .test_agents_codex_cli import TOKEN, CodexCase

REFRESHED = json.dumps({"tokens": {"access_token": "paw-codex-ne" + "wtok"}})


class Recorder:
    """The service's write-back, recorded."""

    def __init__(self, answer=WriteBack.WRITTEN):
        self.values = []
        self.answer = answer

    async def __call__(self, value):
        self.values.append(value)
        return self.answer


class AuthFileTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, True))

    async def test_place_writes_a_new_file_0660_whatever_the_umask(self):
        previous = os.umask(0o077)
        try:
            await CodexAuthFile().place(Secret(TOKEN), self.home)
        finally:
            os.umask(previous)
        path = self.home / AUTH_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)
        self.assertEqual(path.read_text(), TOKEN)
        with self.assertRaises(FileExistsError):
            await CodexAuthFile().place(Secret(TOKEN), self.home)

    async def test_place_refuses_what_is_not_a_login(self):
        for value in ("not json", "[1, 2]", '"text"'):
            with self.subTest(value), self.assertRaises(ValueError):
                await CodexAuthFile().place(Secret(value), self.home)
        self.assertFalse((self.home / AUTH_NAME).exists())

    async def test_an_unchanged_login_is_not_written_back(self):
        await CodexAuthFile().place(Secret(TOKEN), self.home)
        # The same value, pretty-printed as Codex saves it: not a change.
        (self.home / AUTH_NAME).write_text(json.dumps(json.loads(TOKEN), indent=2))
        recorder = Recorder()
        result = await CodexAuthFile().collect(Secret(TOKEN), self.home, recorder)
        self.assertIsNone(result)
        self.assertEqual(recorder.values, [])

    async def test_a_changed_login_is_written_back(self):
        await CodexAuthFile().place(Secret(TOKEN), self.home)
        (self.home / AUTH_NAME).write_text(REFRESHED)
        recorder = Recorder()
        with self.assertLogs("paw_backend.agents.codex_credentials") as logs:
            result = await CodexAuthFile().collect(Secret(TOKEN), self.home, recorder)
        self.assertIs(result, WriteBack.WRITTEN)
        self.assertEqual(recorder.values, [REFRESHED])
        self.assertNotIn("newtok", "\n".join(logs.output))

    async def test_a_stale_write_back_is_reported(self):
        await CodexAuthFile().place(Secret(TOKEN), self.home)
        (self.home / AUTH_NAME).write_text(REFRESHED)
        with self.assertLogs("paw_backend.agents.codex_credentials", "ERROR"):
            result = await CodexAuthFile().collect(
                Secret(TOKEN), self.home, Recorder(WriteBack.STALE)
            )
        self.assertIs(result, WriteBack.STALE)

    async def test_what_cannot_be_a_login_is_not_written_back(self):
        await CodexAuthFile().place(Secret(TOKEN), self.home)
        recorder = Recorder()
        path = self.home / AUTH_NAME
        for name, prepare in {
            "not json": lambda: path.write_text("garbage"),
            "not text": lambda: path.write_bytes(b"\xff\xfe"),
            "too big": lambda: path.write_text(" " * (4 * 16_384 + 1)),
            "a link": lambda: (path.unlink(), path.symlink_to(self.home / "x")),
            "gone": lambda: path.unlink(missing_ok=True),
        }.items():
            with self.subTest(name):
                path.unlink(missing_ok=True)
                path.write_text(TOKEN)
                prepare()
                result = await CodexAuthFile().collect(
                    Secret(TOKEN), self.home, recorder
                )
                self.assertIsNone(result)
        self.assertEqual(recorder.values, [])

    async def test_without_a_write_back_a_refresh_is_logged(self):
        await CodexAuthFile().place(Secret(TOKEN), self.home)
        (self.home / AUTH_NAME).write_text(REFRESHED)
        with self.assertLogs("paw_backend.agents.codex_credentials", "ERROR"):
            self.assertIsNone(
                await CodexAuthFile().collect(Secret(TOKEN), self.home, None)
            )


class SessionTest(CodexCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.credentials = CodexAuthFile()
        self.write_back = Recorder()

    async def test_an_unchanged_login_is_not_written_back(self):
        await self.run_node(
            [{"call": "submit", "arguments": {"summary": "s"}}, {"sleep": 30}]
        )
        self.assertEqual(self.read_report()["auth_length"], len(TOKEN))
        self.assertEqual(self.write_back.values, [])

    async def test_a_refreshed_login_is_written_back_once(self):
        outcome = await self.run_node(
            [
                {"refresh": REFRESHED},
                {"call": "submit", "arguments": {"summary": "s"}},
                {"sleep": 30},
            ]
        )
        self.assertEqual(outcome.result.summary, "s")
        self.assertEqual(self.write_back.values, [REFRESHED])
        self.assertEqual(list(self.sessions.iterdir()), [])  # cleaned up

    async def test_a_refresh_before_a_failed_turn_is_written_back(self):
        outcome = await self.run_node(
            [{"refresh": REFRESHED}, {"error": "unauthorized"}, {"complete": "failed"}]
        )
        self.assertEqual(outcome.error_class, "ConnectionError")
        self.assertEqual(self.write_back.values, [REFRESHED])

    async def test_a_cancelled_attempt_writes_the_refresh_back_first(self):
        running = asyncio.ensure_future(
            self.run_node([{"refresh": REFRESHED}, {"sleep": 30}])
        )
        # Wait until the CLI refreshed its login (in the session's directory).
        for _ in range(400):
            found = [
                p.read_text()
                for p in self.sessions.glob(f"*/config/{AUTH_NAME}")
                if p.exists()
            ]
            if REFRESHED in found:
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("the fake CLI did not refresh")
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(self.write_back.values, [REFRESHED])
        self.assertEqual(list(self.sessions.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
