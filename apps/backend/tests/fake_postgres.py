"""A minimal PostgreSQL wire-protocol server that accepts logins and then hangs.

It authenticates every client (``trust``) and answers nothing after that, so a
query never completes and psycopg's query cancellation is never confirmed.
This is the situation in which a driver-level cancel wait can outlast the
readiness timeout. With ``login=False`` it does not even answer the startup
message. No PostgreSQL installation is needed.
"""

import asyncio
import struct

_SSL_REQUEST = 80877103
_CANCEL_REQUEST = 80877102


async def _handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, login: bool
) -> None:
    """Serve one connection until the peer (or the test) closes it."""
    try:
        length, code = struct.unpack("!II", await reader.readexactly(8))
        if code == _SSL_REQUEST:
            writer.write(b"N")  # no TLS; the client then sends its startup message
            await writer.drain()
            length, code = struct.unpack("!II", await reader.readexactly(8))
        if code == _CANCEL_REQUEST:
            await reader.read()  # never confirm a cancellation
            return
        await reader.readexactly(length - 8)  # startup parameters
        if not login:
            await reader.read()  # never answer the startup message
            return
        version = b"server_version\x0017.0\x00"
        writer.write(
            b"R"
            + struct.pack("!II", 8, 0)  # AuthenticationOk
            + b"S"
            + struct.pack("!I", 4 + len(version))
            + version
            + b"K"
            + struct.pack("!III", 12, 1, 2)  # BackendKeyData
            + b"Z"
            + struct.pack("!I", 5)
            + b"I"  # ReadyForQuery
        )
        await writer.drain()
        await reader.read()  # ignore every query
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


class HangingPostgres:
    """``async with HangingPostgres() as server:`` then use ``server.port``."""

    def __init__(self, login: bool = True) -> None:
        self._login = login

    async def __aenter__(self) -> "HangingPostgres":
        self._writers: list[asyncio.StreamWriter] = []

        async def handle(reader, writer):
            self._writers.append(writer)
            await _handle(reader, writer, self._login)

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port: int = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        for writer in self._writers:
            writer.close()  # ends the handlers, which wait for the peer
        await self._server.wait_closed()
