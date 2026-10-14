"""The PAW tools of a CLI session as an MCP server (Decision 0083, 6): a real
Unix socket in a temporary directory, a fake toolkit (and the real one for a
reader's ``submit``), the real relay program for the end-to-end case.

* nothing is served before the token's handshake; one connection at a time;
* ``initialize`` / ``ping`` / ``tools/list`` / ``tools/call`` and the JSON-RPC
  errors; notifications get no answer;
* a refused call is ``isError``; ``submit`` ends the attempt with its outcome,
  a failure of the attempt is kept, and nothing runs after either;
* a ``ping`` is answered while a long call runs;
* the relay passes the CLI's stdin and stdout through.
"""

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from paw_backend.agents.bridge import ENDED_TEXT, McpBridge
from paw_backend.agents.failures import Failure, RuntimeFailure
from paw_backend.agents.toolkit import BASH_TOOL, NodeToolkit, ToolError
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome

TOKEN = "t" * 16 + "0123456789abcdef0123"
RELAY = (
    Path(__file__).resolve().parents[1] / "deploy" / "cli-session" / "paw_mcp_relay.py"
)
DONE = NodeOutcome.succeeded(NodeResult(summary="done"))


class FakeToolkit:
    def __init__(self):
        self.tools = [BASH_TOOL]
        self.calls = []
        self.answers = {}
        self.gate = None

    async def run(self, name, arguments):
        self.calls.append((name, arguments))
        if self.gate is not None:
            await self.gate.wait()
        answer = self.answers.get(name, ("ok", None))
        if isinstance(answer, BaseException):
            raise answer
        return answer


class BridgeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.path = str(self.directory / "bridge.sock")
        self.toolkit = FakeToolkit()
        self.bridge = McpBridge(self.toolkit, token=TOKEN)
        self.server = await self.bridge.serve(self.path)
        self.addAsyncCleanup(self._stop)
        self.next_id = 0

    async def _stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, token=TOKEN):
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write(json.dumps({"paw_bridge": 1, "token": token}).encode() + b"\n")
        await writer.drain()
        self.addCleanup(writer.close)
        return reader, writer

    async def send(self, writer, method, params=None, *, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        return message.get("id")

    async def assert_closed(self, reader):
        """The bridge closed the connection (without answering)."""
        try:
            data = await asyncio.wait_for(reader.read(), 5)
        except ConnectionResetError:
            data = b""
        self.assertEqual(data, b"")

    async def receive(self, reader):
        line = await asyncio.wait_for(reader.readline(), 5)
        return json.loads(line)

    async def call(self, reader, writer, method, params=None):
        request_id = await self.send(writer, method, params)
        answer = await self.receive(reader)
        self.assertEqual(answer["id"], request_id)
        return answer


class HandshakeTest(BridgeCase):
    async def test_a_wrong_or_missing_token_gets_nothing(self):
        for token in ("x" * 36, TOKEN[:-1], ""):
            with self.subTest(token=token):
                reader, writer = await self.connect(token)
                await self.send(writer, "tools/list")
                await self.assert_closed(reader)
        reader, writer = await asyncio.open_unix_connection(self.path)
        writer.write(b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n')
        await writer.drain()
        await self.assert_closed(reader)
        writer.close()
        self.assertEqual(self.toolkit.calls, [])

    async def test_one_connection_at_a_time(self):
        reader, writer = await self.connect()
        await self.call(reader, writer, "ping")
        other_reader, other_writer = await self.connect()
        await self.assert_closed(other_reader)
        await self.call(reader, writer, "ping")  # the first still works

    async def test_two_handshakes_at_once_get_one_connection(self):
        first_reader, first_writer = await asyncio.open_unix_connection(self.path)
        second_reader, second_writer = await asyncio.open_unix_connection(self.path)
        self.addCleanup(first_writer.close)
        self.addCleanup(second_writer.close)
        hello = json.dumps({"paw_bridge": 1, "token": TOKEN}).encode() + b"\n"
        ping = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
        for writer in (first_writer, second_writer):
            writer.write(hello + ping)
        await asyncio.gather(first_writer.drain(), second_writer.drain())
        answers = []
        for reader in (first_reader, second_reader):
            try:
                line = await asyncio.wait_for(reader.readline(), 5)
            except ConnectionResetError:
                line = b""
            answers.append(line)
        self.assertEqual(sum(1 for line in answers if line), 1)

    async def test_a_connection_without_the_token_holds_no_slot(self):
        silent_reader, silent_writer = await asyncio.open_unix_connection(self.path)
        self.addCleanup(silent_writer.close)
        await asyncio.sleep(0.05)  # it says nothing, and keeps the line open
        reader, writer = await self.connect()
        await self.call(reader, writer, "ping")  # the relay still gets in

    def test_a_short_token_is_refused(self):
        with self.assertRaises(ValueError):
            McpBridge(FakeToolkit(), token="short")


class ProtocolTest(BridgeCase):
    async def test_initialize_and_the_tool_list(self):
        reader, writer = await self.connect()
        answer = await self.call(
            reader, writer, "initialize", {"protocolVersion": "2025-03-26"}
        )
        self.assertEqual(answer["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(answer["result"]["serverInfo"]["name"], "paw")
        self.assertIn("tools", answer["result"]["capabilities"])
        answer = await self.call(
            reader, writer, "initialize", {"protocolVersion": "1999-01-01"}
        )
        self.assertEqual(answer["result"]["protocolVersion"], "2025-06-18")
        await self.send(writer, "notifications/initialized", notify=True)
        (tool,) = (await self.call(reader, writer, "tools/list"))["result"]["tools"]
        self.assertEqual(tool["name"], "bash")
        self.assertEqual(tool["inputSchema"]["required"], ["command"])

    async def test_json_rpc_errors(self):
        reader, writer = await self.connect()
        self.assertEqual(
            (await self.call(reader, writer, "resources/list"))["error"]["code"],
            -32601,
        )
        answer = await self.call(reader, writer, "tools/call", {"name": 3})
        self.assertEqual(answer["error"]["code"], -32602)
        writer.write(b"{not json\n")
        await writer.drain()
        self.assertEqual((await self.receive(reader))["error"]["code"], -32700)
        writer.write(b'{"id": 9, "method": "ping"}\n')  # no "jsonrpc"
        await writer.drain()
        self.assertEqual((await self.receive(reader))["error"]["code"], -32600)
        self.assertEqual(self.toolkit.calls, [])


class CallTest(BridgeCase):
    async def tool(self, reader, writer, name, arguments=None):
        answer = await self.call(
            reader, writer, "tools/call", {"name": name, "arguments": arguments or {}}
        )
        result = answer["result"]
        return result["content"][0]["text"], result["isError"]

    async def test_answers_refusals_and_the_submit(self):
        reader, writer = await self.connect()
        self.toolkit.answers["bash"] = ("exit code: 0\nhello", None)
        self.toolkit.answers["str_replace"] = ToolError("ValueError: nope")
        self.toolkit.answers["submit"] = ("Submitted.", DONE)
        self.assertEqual(
            await self.tool(reader, writer, "bash", {"command": "echo hello"}),
            ("exit code: 0\nhello", False),
        )
        self.assertEqual(
            await self.tool(reader, writer, "str_replace"),
            ("Tool error: ValueError: nope", True),
        )
        self.assertFalse(self.bridge.ended.is_set())
        self.assertEqual(
            await self.tool(reader, writer, "submit", {"summary": "s"}),
            ("Submitted.", False),
        )
        self.assertTrue(self.bridge.ended.is_set())
        self.assertIs(self.bridge.outcome, DONE)
        self.assertEqual(await self.tool(reader, writer, "bash"), (ENDED_TEXT, True))
        self.assertEqual(len(self.toolkit.calls), 3)

    async def test_a_failure_of_the_attempt_is_kept(self):
        for error in (
            RuntimeFailure(Failure.SANDBOX_UNAVAILABLE),
            NodeStopped(StopReason.LEASE_LOST),
        ):
            with self.subTest(error=type(error).__name__):
                bridge = McpBridge(FakeToolkit(), token=TOKEN)
                bridge._toolkit.answers["bash"] = error
                result = await bridge._call("bash", {})
                self.assertTrue(result["isError"])
                self.assertIs(bridge.failure, error)
                self.assertTrue(bridge.ended.is_set())

    async def test_a_ping_is_answered_while_a_call_runs(self):
        reader, writer = await self.connect()
        self.toolkit.gate = asyncio.Event()
        call_id = await self.send(
            writer, "tools/call", {"name": "bash", "arguments": {"command": "x"}}
        )
        ping_id = await self.send(writer, "ping")
        first = await self.receive(reader)
        self.assertEqual(first["id"], ping_id)
        self.toolkit.gate.set()
        second = await self.receive(reader)
        self.assertEqual(second["id"], call_id)

    async def test_the_real_toolkit_submits_a_readers_findings(self):
        assignment = NodeAssignment(
            task_id=uuid.uuid4(),
            node_key="r",
            role=NodeRole.RESEARCHER,
            title="Look",
            goal="Find it.",
            input={},
            upstream={},
            agent="claude",
            attempt=1,
            approach=0,
            tools=None,
            budget=None,
            worktrees={},
            roots={},
        )
        bridge = McpBridge(NodeToolkit(assignment), token=TOKEN)
        self.assertEqual([t["name"] for t in bridge._tool_list()], ["submit"])
        result = await bridge._call("submit", {"summary": "It is in parse.py"})
        self.assertFalse(result["isError"])
        self.assertEqual(bridge.outcome.result.summary, "It is in parse.py")


class RelayTest(BridgeCase):
    async def test_the_relay_passes_the_cli_through(self):
        token_file = self.directory / "token"
        token_file.write_text(TOKEN + "\n")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(RELAY),
            "--socket",
            self.path,
            "--token-file",
            str(token_file),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        process.stdin.write(json.dumps(request).encode() + b"\n")
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        self.assertEqual(json.loads(line)["result"]["tools"][0]["name"], "bash")
        process.stdin.close()
        self.assertEqual(await asyncio.wait_for(process.wait(), 10), 0)


if __name__ == "__main__":
    unittest.main()
