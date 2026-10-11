"""The sandbox's ``git`` shim and its socket, and the file helper (Decision 0083, 2).

The real programs of ``deploy/sandbox`` run here as the test process (pointed
at a temporary directory instead of ``/run/paw-shim``): the shim talks to the
real :class:`ShimServer`; the file helper writes in a temporary directory.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paw_backend.sandbox.shim import MAX_REQUEST_BYTES, ShimServer

from .sandbox_support import FILE_HELPER, _shim_program


class ShimTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="paw-shim-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.directory = os.path.join(self.base, "s")
        self.program = os.path.join(self.base, "git")
        with open(self.program, "w") as handle:
            handle.write(_shim_program(self.directory))

    async def asyncSetUp(self):
        self.server = ShimServer(self.directory)
        await self.server.open()

    async def asyncTearDown(self):
        await self.server.close()

    async def git(self, *argv, cwd="/workspace"):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            self.program,
            *argv,
            env={"PAW_TEST_SANDBOX_CWD": cwd},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        return process.returncode, out.decode(), err.decode()

    async def test_a_command_is_answered_while_active(self):
        asked = []

        async def handler(cwd, argv):
            asked.append((cwd, argv))
            return {"exit": 1, "output": "M  a.py\n"}

        self.server.activate(handler)
        code, out, err = await self.git("status", "--short", cwd="/workspace/src")
        self.assertEqual((code, out, err), (1, "M  a.py\n", ""))
        self.assertEqual(asked, [("/workspace/src", ["status", "--short"])])

    async def test_a_refusal_is_printed_as_gits_fatal(self):
        async def handler(cwd, argv):
            return {"error": "subcommand_not_allowed", "message": "read-only"}

        self.server.activate(handler)
        code, out, err = await self.git("commit", "-m", "x")
        self.assertEqual(code, 128)
        self.assertEqual(err, "git: read-only (subcommand_not_allowed)\n")

    async def test_nothing_is_answered_outside_a_command(self):
        code, _, err = await self.git("status")
        self.assertEqual(code, 128)
        self.assertIn("inactive", err)
        self.server.activate(lambda cwd, argv: None)
        self.server.deactivate()
        code, _, err = await self.git("status")
        self.assertIn("inactive", err)

    async def test_a_handler_failure_is_a_fixed_answer(self):
        async def handler(cwd, argv):
            raise RuntimeError("/secret/path in a message")

        self.server.activate(handler)
        with self.assertLogs("paw_backend.sandbox.shim", "ERROR") as logs:
            code, _, err = await self.git("status")
        self.assertEqual(code, 128)
        self.assertNotIn("/secret", err + "".join(logs.output))

    def request(self, payload: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(os.path.join(self.directory, "git.sock"))
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            data = b""
            while chunk := connection.recv(65536):
                data += chunk
        return json.loads(data)

    async def test_the_token_is_required(self):
        async def handler(cwd, argv):
            return {"exit": 0, "output": "ran"}

        self.server.activate(handler)
        token = await asyncio.to_thread(Path(self.directory, "token").read_text)
        self.assertEqual(len(token), 64)
        for document in (
            {"token": "0" * 64, "cwd": "/workspace", "argv": []},
            {"cwd": "/workspace", "argv": []},
            {"token": token[:-1], "cwd": "/workspace", "argv": []},
        ):
            answer = await asyncio.to_thread(
                self.request, json.dumps(document).encode() + b"\n"
            )
            self.assertEqual(answer["error"], "bad_token")
        good = {"token": token, "cwd": "/workspace", "argv": ["status"]}
        answer = await asyncio.to_thread(
            self.request, json.dumps(good).encode() + b"\n"
        )
        self.assertEqual(answer, {"exit": 0, "output": "ran"})
        for payload in (
            b"not json\n",
            b"[]\n",
            json.dumps({"token": token, "cwd": 1, "argv": []}).encode() + b"\n",
            json.dumps({"token": token, "cwd": "/w", "argv": [1]}).encode() + b"\n",
            b"x" * (MAX_REQUEST_BYTES + 10) + b"\n",
            b"no newline",
        ):
            answer = await asyncio.to_thread(self.request, payload)
            self.assertEqual(answer["error"], "bad_request", payload[:20])

    async def test_the_files_are_this_users_only(self):
        mode = lambda name: os.stat(os.path.join(self.directory, name)).st_mode & 0o777  # noqa: E731
        self.assertEqual(mode(""), 0o700)
        self.assertEqual(mode("token"), 0o600)
        self.assertEqual(mode("git.sock"), 0o600)
        await self.server.close()
        self.assertFalse(await asyncio.to_thread(os.path.exists, self.directory))
        self.server = ShimServer(self.directory)
        await self.server.open()

    async def test_the_shim_without_a_socket(self):
        await self.server.close()
        code, _, err = await self.git("status")
        self.assertEqual(code, 128)
        self.assertIn("not available", err)
        self.server = ShimServer(self.directory)
        await self.server.open()


class FileHelperTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="paw-files-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.root])

    def helper(self, operation, path, document):
        result = subprocess.run(
            [sys.executable, "-I", str(FILE_HELPER), operation, path],
            input=json.dumps(document).encode(),
            capture_output=True,
            check=False,
        )
        return result.returncode, json.loads(result.stdout)

    def test_write_makes_the_parents_and_keeps_the_bytes(self):
        path = os.path.join(self.root, "a", "b", "c.txt")
        code, answer = self.helper("write", path, {"content": "x\r\ny\n"})
        self.assertEqual((code, answer), (0, {"ok": True, "chars": 5}))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"x\r\ny\n")
        code, answer = self.helper("write", self.root, {"content": ""})
        self.assertEqual((code, answer), (3, {"error": "not_a_file"}))

    def test_str_replace_needs_exactly_one_occurrence(self):
        path = os.path.join(self.root, "f.py")
        with open(path, "wb") as handle:
            handle.write(b"a = 1\r\nb = 1\r\n")
        self.assertEqual(
            self.helper("str-replace", path, {"old": "= 1", "new": "= 2"}),
            (3, {"error": "occurrences", "count": 2}),
        )
        self.assertEqual(
            self.helper("str-replace", path, {"old": "c = 1", "new": "x"}),
            (3, {"error": "occurrences", "count": 0}),
        )
        self.assertEqual(
            self.helper("str-replace", path, {"old": "", "new": "x"})[0], 3
        )
        self.assertEqual(
            self.helper("str-replace", path, {"old": "a = 1", "new": "a = 3"}),
            (0, {"ok": True, "chars": 5}),
        )
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"a = 3\r\nb = 1\r\n")
        self.assertEqual(
            self.helper(
                "str-replace", os.path.join(self.root, "none"), {"old": "a", "new": "b"}
            ),
            (3, {"error": "not_found"}),
        )

    def test_bad_input_and_usage(self):
        path = os.path.join(self.root, "x")
        self.assertEqual(
            self.helper("write", path, {"content": 1}), (3, {"error": "bad_input"})
        )
        self.assertEqual(self.helper("delete", path, {})[0], 2)
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe")
        self.assertEqual(
            self.helper("str-replace", path, {"old": "a", "new": "b"}),
            (3, {"error": "not_utf8"}),
        )


if __name__ == "__main__":
    unittest.main()
