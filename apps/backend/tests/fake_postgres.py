"""A minimal PostgreSQL wire-protocol server for stalled and healthy databases.

It authenticates every client (``trust``). By default it then answers nothing,
so a query never completes and psycopg's query cancellation is never
confirmed: the situation in which a driver-level cancel wait can outlast the
readiness timeout. With ``login=False`` it does not even answer the startup
message; with ``answer_queries=True`` it answers ``SELECT 1`` like a healthy
server. ``logins`` counts the connections that sent a startup message.
No PostgreSQL installation is needed.
"""

import asyncio
import struct

_SSL_REQUEST = 80877103
_CANCEL_REQUEST = 80877102


def _message(kind: bytes, body: bytes = b"") -> bytes:
    return kind + struct.pack("!I", 4 + len(body)) + body


_ROW_DESCRIPTION = _message(  # one int4 column
    b"T",
    struct.pack("!H", 1) + b"?column?\x00" + struct.pack("!IHIhih", 0, 0, 23, 4, -1, 0),
)
# The reply to `SELECT 1` as psycopg sends it: one simple-protocol Query.
_SELECT_1_REPLY = b"".join(
    (
        _ROW_DESCRIPTION,
        _message(b"D", struct.pack("!HI", 1, 1) + b"1"),  # DataRow
        _message(b"C", b"SELECT 1\x00"),  # CommandComplete
        _message(b"Z", b"I"),  # ReadyForQuery
    )
)


async def _answer_queries(reader: asyncio.StreamReader, writer) -> None:
    """Answer every simple ``Query`` until the client terminates."""
    while True:
        kind, length = struct.unpack("!cI", await reader.readexactly(5))
        await reader.readexactly(length - 4)
        if kind == b"X":  # Terminate
            return
        if kind == b"Q":
            writer.write(_SELECT_1_REPLY)
            await writer.drain()


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    server: "HangingPostgres",
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
        server.logins += 1
        if not server._login:
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
        if server._answer_queries:
            await _answer_queries(reader, writer)
        else:
            await reader.read()  # ignore every query
    except (asyncio.IncompleteReadError, ConnectionError, struct.error):
        pass
    finally:
        writer.close()


class HangingPostgres:
    """``async with HangingPostgres() as server:`` then use ``server.port``."""

    def __init__(self, login: bool = True, answer_queries: bool = False) -> None:
        self._login = login
        self._answer_queries = answer_queries
        self.logins = 0  # connections that sent a startup message

    async def __aenter__(self) -> "HangingPostgres":
        self._writers: list[asyncio.StreamWriter] = []

        async def handle(reader, writer):
            self._writers.append(writer)
            await _handle(reader, writer, self)

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port: int = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        for writer in self._writers:
            writer.close()  # ends the handlers, which wait for the peer
        await self._server.wait_closed()


class FreezableProxy:
    """A TCP proxy in front of a REAL PostgreSQL that can go silent mid-session.

    ``async with FreezableProxy(host, port) as proxy:`` then connect to
    ``proxy.port``. Until ``freeze()`` it forwards every byte both ways. From
    then on it forwards nothing in either direction (an established connection
    stalls, and so does a new one, a query cancellation included) but keeps
    every socket open: the server "accepted the connection but does not answer",
    at a moment the test chooses, after the statements before it succeeded.
    """

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._forwarding = asyncio.Event()
        self._forwarding.set()
        self._tasks: set[asyncio.Task] = set()
        self._writers: list[asyncio.StreamWriter] = []

    def freeze(self) -> None:
        self._forwarding.clear()

    async def __aenter__(self) -> "FreezableProxy":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port: int = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        for task in self._tasks:
            task.cancel()
        for writer in self._writers:
            writer.close()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.append(writer)
        upstream_reader, upstream_writer = await asyncio.open_connection(
            *self._upstream
        )
        self._writers.append(upstream_writer)
        for source, target in (
            (reader, upstream_writer),
            (upstream_reader, writer),
        ):
            task = asyncio.create_task(self._pump(source, target))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _pump(
        self, source: asyncio.StreamReader, target: asyncio.StreamWriter
    ) -> None:
        try:
            while data := await source.read(65536):
                await self._forwarding.wait()  # frozen: hold the bytes back
                target.write(data)
                await target.drain()
        except ConnectionError:
            pass
        finally:
            target.close()
