#!/usr/bin/env python3
"""``paw-egress``: the only way out of a CLI session's container (Decision 0087, 3).

A CLI (Codex / Claude Code) runs in a container without a network
(``--network none``, Decision 0083, 6). Its only path to its provider is this
proxy, on the host, under its own account ``paw-egress``:

* it listens on a Unix socket only (group ``paw-cli-bridge``, mode 0660),
  which systemd makes and passes (``paw-egress.socket``): the proxy's own
  process is not in that group, which also reads the sessions' credential
  files (Decision 0088); inside the container a small forwarder
  (:func:`forward`, the same file) maps ``127.0.0.1:<port>`` to the mounted
  socket, and the CLI is given ``HTTPS_PROXY=http://127.0.0.1:<port>``;
* it accepts HTTP ``CONNECT host:443`` only, for a host that is on the
  allowlist exactly (no wildcard, no other port, no plain HTTP request);
* it resolves the host itself and refuses every address that is not a global
  one (private, loopback, link-local, CGNAT, multicast, reserved...): a DNS
  answer cannot take a session into the LAN or the host;
* TLS passes through untouched: the proxy neither reads nor changes it, and
  adds no credential;
* every connection is logged on one line: the session (the optional
  ``X-PAW-Session: <uuid>`` header of the CONNECT), the host, the outcome and
  the byte counts. Never a path, a header value other than the session, or
  any content.

Limits: a CONNECT request of at most 8 KiB within 10 seconds, at most 64
connections at once, and a connection idle for 15 minutes in both directions
is closed.

The account's own outbound traffic is narrowed by nftables as well (the
example in ``README.md``: DNS to the configured resolver and TCP 443 to global
addresses only); the hostname decision is this file's.

Standard library only (it does not run in the backend's virtual environment).

    paw_egress.py serve --config /etc/paw/egress.toml
    paw_egress.py forward --listen-port 3128 --socket /run/paw-egress/egress.sock \\
        --session <uuid>
"""

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import sys
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger("paw_egress")

MAX_REQUEST_BYTES = 8 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 15 * 60.0
MAX_CONNECTIONS = 64
PORT = 443
_CHUNK = 64 * 1024

_HOST = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_SESSION_HEADER = "x-paw-session"
_GLOBAL_UNICAST6 = ipaddress.ip_network("2000::/3")

#: An address the proxy connects to: (family, address string).
Address = tuple[int, str]
Resolver = Callable[[str], Awaitable[Sequence[Address]]]
Connector = Callable[
    [Address], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


class Refused(Exception):
    """A request the proxy does not serve. ``status`` is the HTTP answer,
    ``reason`` the fixed word of the log line."""

    def __init__(
        self, status: int, reason: str, host: str = "-", session: str = "-"
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        # What the log line names (a host only when it is a DNS name).
        self.host = host
        self.session = session


_STATUS_TEXT = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


@dataclass(frozen=True, slots=True)
class Config:
    socket_path: str
    allowed_hosts: frozenset[str]

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        socket_path = data.get("socket")
        hosts = data.get("allowed_hosts")
        if not isinstance(socket_path, str) or not os.path.isabs(socket_path):
            raise ValueError("socket must be an absolute path")
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ValueError("allowed_hosts must be a list of host names")
        return cls(socket_path, allowed_hosts(hosts))


def allowed_hosts(hosts: Iterable[str]) -> frozenset[str]:
    """The allowlist, each entry a plain lower-case DNS name (no wildcard, no
    port, no address)."""
    result = set()
    for host in hosts:
        if host != host.lower() or not _HOST.fullmatch(host):
            raise ValueError("an allowed host must be a lower-case DNS name")
        result.add(host)
    return frozenset(result)


def is_public(address: str) -> bool:
    """Whether ``address`` is a global unicast address (not private,
    loopback, link-local, CGNAT, multicast, reserved or unspecified)."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip not in _GLOBAL_UNICAST6 or ip.is_site_local:
            # Only the global unicast block 2000::/3 (``is_global`` also holds
            # for reserved blocks such as 4000::/2, and for fec0::/10).
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


async def system_resolver(host: str) -> Sequence[Address]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, PORT, type=socket.SOCK_STREAM
    )
    return [(family, sockaddr[0]) for family, _, _, _, sockaddr in infos]


async def system_connector(
    address: Address,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address[1], PORT)


class EgressProxy:
    """See the module. ``resolver`` / ``connector`` are the seams of the
    tests (the defaults use the system's DNS and TCP)."""

    def __init__(
        self,
        hosts: frozenset[str],
        *,
        resolver: Resolver = system_resolver,
        connector: Connector = system_connector,
        max_connections: int = MAX_CONNECTIONS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._hosts = hosts
        self._resolver = resolver
        self._connector = connector
        self._slots = asyncio.Semaphore(max_connections)
        self._idle = idle_timeout

    async def serve_socket(self, sock: socket.socket) -> asyncio.AbstractServer:
        """Serve a listening Unix socket that systemd made and passed
        (``paw-egress.socket``: group ``paw-cli-bridge``, mode 0660)."""
        return await asyncio.start_unix_server(
            self.handle, sock=sock, limit=MAX_REQUEST_BYTES
        )

    async def serve_unix(self, path: str) -> asyncio.AbstractServer:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        server = await asyncio.start_unix_server(
            self.handle, path=path, limit=MAX_REQUEST_BYTES
        )
        os.chmod(path, 0o660)
        return server

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = "-"
        host = "-"
        sent = received = 0
        outcome = "ok"
        if self._slots.locked():
            await _answer(writer, 503)
            _log(session, host, "busy", 0, 0)
            await _close(writer)
            return
        async with self._slots:
            try:
                host, session = await self._read_request(reader)
                upstream_reader, upstream_writer = await self._open(host, session)
            except Refused as refused:
                await _answer(writer, refused.status)
                _log(refused.session, refused.host, refused.reason, 0, 0)
                await _close(writer)
                return
            try:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                sent, received, failed = await self._splice(
                    reader, writer, upstream_reader, upstream_writer
                )
                if failed:
                    outcome = "error"
            except Exception:
                outcome = "error"
            finally:
                await _close(upstream_writer)
                await _close(writer)
            _log(session, host, outcome, sent, received)

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str]:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                head = await reader.readuntil(b"\r\n\r\n")
        except TimeoutError:
            raise Refused(408, "timeout") from None
        except asyncio.LimitOverrunError:
            raise Refused(400, "too_long") from None
        except asyncio.IncompleteReadError:
            raise Refused(400, "incomplete") from None
        if len(head) > MAX_REQUEST_BYTES:
            raise Refused(400, "too_long")
        try:
            text = head.decode("ascii")
        except UnicodeDecodeError:
            raise Refused(400, "not_ascii") from None
        lines = text.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise Refused(400, "malformed")
        method, target, _ = parts
        session = "-"
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon and name.strip().lower() == _SESSION_HEADER:
                with contextlib.suppress(ValueError):
                    session = str(uuid.UUID(value.strip()))
        host, separator, port = target.rpartition(":")
        # The refusal's log line names the host the CLI asked for (what the
        # allowlist may need), but only when it is a plain DNS name.
        shown = host if _HOST.fullmatch(host) else "-"
        if method != "CONNECT":
            raise Refused(405, "not_connect", session=session)
        if not separator or port != str(PORT):
            raise Refused(403, "port", shown, session)
        if host not in self._hosts:
            raise Refused(403, "host", shown, session)
        return host, session

    async def _open(
        self, host: str, session: str = "-"
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                addresses = await self._resolver(host)
        except TimeoutError:
            raise Refused(504, "dns_timeout", host, session) from None
        except OSError:
            raise Refused(502, "dns", host, session) from None
        public = [address for address in addresses if is_public(address[1])]
        if not public or len(public) != len(addresses):
            # One non-global answer refuses the host: a name that also points
            # into the LAN is not a provider's.
            raise Refused(403, "address", host, session)
        for address in public:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                    return await self._connector(address)
            except (OSError, TimeoutError):
                continue
        raise Refused(502, "connect", host, session)

    async def _splice(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> tuple[int, int, bool]:
        """Copy both ways until both ends are done, a side fails, or both are
        idle. Returns the bytes sent and received, and whether a side failed."""
        counts = [0, 0]
        failed = False
        activity = asyncio.Event()

        async def pump(
            source: asyncio.StreamReader, sink: asyncio.StreamWriter, index: int
        ) -> None:
            while data := await source.read(_CHUNK):
                counts[index] += len(data)
                activity.set()
                sink.write(data)
                await sink.drain()
            with contextlib.suppress(OSError):
                if sink.can_write_eof():
                    sink.write_eof()

        tasks = [
            asyncio.create_task(pump(client_reader, upstream_writer, 0)),
            asyncio.create_task(pump(upstream_reader, client_writer, 1)),
        ]
        try:
            while not all(task.done() for task in tasks):
                activity.clear()
                waiter = asyncio.create_task(activity.wait())
                done, _ = await asyncio.wait(
                    [waiter, *[t for t in tasks if not t.done()]],
                    timeout=self._idle,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                waiter.cancel()
                if not done:
                    break  # idle in both directions
                if any(t.done() and not t.cancelled() and t.exception() for t in tasks):
                    failed = True
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return counts[0], counts[1], failed


async def _answer(writer: asyncio.StreamWriter, status: int) -> None:
    with contextlib.suppress(OSError):
        writer.write(
            f"HTTP/1.1 {status} {_STATUS_TEXT[status]}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


def _log(session: str, host: str, outcome: str, sent: int, received: int) -> None:
    logger.info(
        "session=%s host=%s outcome=%s sent=%d received=%d",
        session,
        host,
        outcome,
        sent,
        received,
    )


async def forward(
    listen_port: int, socket_path: str, session: str | None = None
) -> asyncio.AbstractServer:
    """Inside the container: ``127.0.0.1:<listen_port>`` to the proxy's
    mounted Unix socket. With ``session`` (the session's UUID, given by the
    backend through the container's command, not by the CLI), the CONNECT
    request's ``X-PAW-Session`` header is set to it: any such header the CLI
    sent is dropped. Everything after the request passes byte for byte."""
    if session is not None:
        session = str(uuid.UUID(session))

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            upstream_reader, upstream_writer = await asyncio.open_unix_connection(
                socket_path
            )
        except OSError:
            await _close(writer)
            return
        if session is not None:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    head = await reader.readuntil(b"\r\n\r\n")
            except (
                TimeoutError,
                asyncio.LimitOverrunError,
                asyncio.IncompleteReadError,
            ):
                await _close(upstream_writer)
                await _close(writer)
                return
            lines = head[:-4].split(b"\r\n")
            kept = [
                line
                for line in lines[1:]
                if line.split(b":", 1)[0].strip().lower() != _SESSION_HEADER.encode()
            ]
            upstream_writer.write(
                b"\r\n".join([lines[0], *kept, f"X-PAW-Session: {session}".encode()])
                + b"\r\n\r\n"
            )

        async def copy(source, sink):
            # The end of one direction (a half-close) is passed on as one
            # (``write_eof``): the other direction stays open until it ends
            # too. A failure ends both.
            try:
                while data := await source.read(_CHUNK):
                    sink.write(data)
                    await sink.drain()
                if sink.can_write_eof():
                    sink.write_eof()
                else:
                    await _close(sink)
            except Exception:
                await _close(upstream_writer)
                await _close(writer)

        try:
            await asyncio.gather(
                copy(reader, upstream_writer), copy(upstream_reader, writer)
            )
        finally:
            await _close(upstream_writer)
            await _close(writer)

    return await asyncio.start_server(pipe, host="127.0.0.1", port=listen_port)


def inherited_socket(
    environ: Mapping[str, str], *, fd: int = 3
) -> socket.socket | None:
    """The listening socket systemd passed to this process (socket
    activation: ``LISTEN_PID`` is this process, ``LISTEN_FDS`` is 1, the
    socket is ``fd`` 3), or ``None`` when none was passed. The process then
    needs no access to the socket's group (``paw-cli-bridge``, which also
    reads the sessions' credential files: Decision 0088)."""
    if environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    if environ.get("LISTEN_FDS") != "1":
        raise ValueError("exactly one socket must be passed")
    sock = socket.socket(fileno=fd)
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        sock.detach()
        raise ValueError("the passed socket must be a Unix stream socket")
    sock.setblocking(False)
    return sock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paw_egress.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True)
    relay = commands.add_parser("forward")
    relay.add_argument("--listen-port", type=int, required=True)
    relay.add_argument("--socket", required=True)
    relay.add_argument("--session")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    async def run() -> None:
        if arguments.command == "serve":
            config = Config.load(arguments.config)
            proxy = EgressProxy(config.allowed_hosts)
            inherited = inherited_socket(os.environ)
            if inherited is not None:
                server = await proxy.serve_socket(inherited)
            else:  # (development: the proxy makes the socket itself)
                server = await proxy.serve_unix(config.socket_path)
        else:
            server = await forward(
                arguments.listen_port, arguments.socket, arguments.session
            )
        async with server:
            await server.serve_forever()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
