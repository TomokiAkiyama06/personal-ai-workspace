#!/usr/bin/env python3
"""The MCP server command of a CLI session (Decision 0083, 6; #208).

Inside the CLI's container, the CLI starts this as its only MCP server
(``--mcp-config``: ``{"mcpServers": {"paw": {"command": "python3", "args":
["-I", "/opt/paw/paw_mcp_relay.py", "--socket", ..., "--token-file", ...]}}}``).
It connects to the backend's bridge socket (mounted from the session's
directory), sends one handshake line with the session's token (read from a
file, never from an argument or the environment), and then copies the CLI's
stdin to the socket and the socket to the CLI's stdout, line for line, until
either side ends. Standard library only.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

MAX_LINE_BYTES = 8 * 1024 * 1024


async def relay(socket_path: str, token: str) -> int:
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin.buffer
    )
    reader, writer = await asyncio.open_unix_connection(
        socket_path, limit=MAX_LINE_BYTES
    )
    writer.write(json.dumps({"paw_bridge": 1, "token": token}).encode() + b"\n")
    await writer.drain()

    async def upstream() -> None:
        while line := await stdin.readline():
            writer.write(line)
            await writer.drain()
        writer.write_eof()

    async def downstream() -> None:
        out = sys.stdout.buffer
        while line := await reader.readline():
            out.write(line)
            out.flush()

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    writer.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paw_mcp_relay.py", allow_abbrev=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--token-file", required=True)
    arguments = parser.parse_args(argv)
    with open(arguments.token_file, encoding="ascii") as handle:
        token = handle.read().strip()
    return asyncio.run(relay(arguments.socket, token))


if __name__ == "__main__":
    sys.exit(main())
