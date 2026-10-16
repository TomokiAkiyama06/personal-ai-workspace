#!/usr/bin/env python3
"""``paw-egress``: the only way out of a CLI session's container (Decision 0087, 3).

A CLI (Codex / Claude Code) runs in a container without a network
(``--network none``, Decision 0083, 6). Its only path to its provider is this
proxy, on the host, under its own account ``paw-egress``, one instance per
session (Decision 0091):

* each instance listens on its session's Unix socket only (group
  ``paw-cli-bridge``, mode 0660), which systemd makes and passes
  (``paw-egress-session@<uuid>.socket``, started with the session's unit and
  stopped with it): the proxy's own process is not in that group, which also
  reads the sessions' credential files (Decision 0088). Only that session's
  container has the socket mounted, so the session is the socket's: the
  instance is told its UUID on its command line (``serve --session``) and no
  header of the container is read. Inside the container a small forwarder
  (:func:`forward`, the same file) maps ``127.0.0.1:<port>`` to the mounted
  socket, and the CLI is given ``HTTPS_PROXY=http://127.0.0.1:<port>``;
* it accepts HTTP ``CONNECT host:443`` only, for a host that is on the
  allowlist exactly (no wildcard, no other port, no plain HTTP request);
* it resolves the host itself and refuses every address that is not a global
  one (private, loopback, link-local, CGNAT, multicast, reserved...): a DNS
  answer cannot take a session into the LAN or the host;
* TLS passes through untouched: the proxy neither reads nor changes it, and
  adds no credential;
* every connection is logged with the session, its number in the instance
  (``conn=``), the host, the outcome and the byte counts: a tunnel when it
  opens (``outcome=open``), every 60 seconds while it is open
  (``outcome=progress``) and when it ends; the counts are the bytes since the
  connection's previous line, so the lines of a session add up to its traffic
  (Decision 0091, 3: the session's volume is counted from these lines). A
  refusal is one line. When the instance stops it writes a summary line (an
  approximation: an instance that was killed writes none, and the next
  connection starts a new one). Never a path, a header value, or any content.

Limits: a CONNECT request of at most 8 KiB within 10 seconds; per session at
most 16 connections at once and 20 new ones a second (the excess is closed
without a line of its own and counted on one line every 10 seconds, which keeps
the session's lines far below journald's rate limit); on the whole host at most
64 live connections, each holding a ``flock`` on one of the slot files of
``slots`` (``/run/paw-egress/slots``, made by ``tmpfiles.d``; the kernel lets a
killed instance's locks go); a connection idle for 15 minutes in both
directions is closed.

The account's own outbound traffic is narrowed by nftables as well (the
example in ``README.md``: DNS to the configured resolver and TCP 443 to global
addresses only); the hostname decision is this file's.

Standard library only (it does not run in the backend's virtual environment).

    paw_egress.py serve --config /etc/paw/egress.toml --session <uuid>
    paw_egress.py forward --listen-port 3128 --socket /run/paw-egress.sock

Without ``--session`` (development and tests only) ``serve`` makes the socket
of the configuration's ``socket`` itself and logs no session.
"""

import argparse
import asyncio
import collections
import concurrent.futures
import contextlib
import fcntl
import ipaddress
import logging
import os
import random
import re
import signal
import socket
import stat
import sys
import tomllib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger("paw_egress")

MAX_REQUEST_BYTES = 8 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 15 * 60.0
#: Decision 0091, 3 (provisional): one session's connections at once, and its
#: new connections a second (the excess is closed and counted every
#: ``RATE_REPORT_SECONDS``).
MAX_SESSION_CONNECTIONS = 16
MAX_NEW_PER_SECOND = 20
RATE_REPORT_SECONDS = 10.0
#: A line for every open tunnel this often (its bytes since its last line).
PROGRESS_SECONDS = 60.0
#: Live connections on the whole host: the slot files ``tmpfiles.d`` makes.
HOST_CONNECTIONS = 64
#: The threads of the name lookups (``getaddrinfo``): the unit's ``TasksMax=``
#: counts them with the main thread.
RESOLVER_THREADS = 4
PORT = 443
_CHUNK = 64 * 1024

_HOST = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
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

    def __init__(self, status: int, reason: str, host: str = "-") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        # What the log line names (a host only when it is a DNS name).
        self.host = host


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
    # The socket ``serve`` makes itself without ``--session`` (development).
    socket_path: str | None
    allowed_hosts: frozenset[str]
    # The directory of the host's connection slots (``serve --session``).
    slots: str | None = None

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        socket_path = data.get("socket")
        slots = data.get("slots")
        hosts = data.get("allowed_hosts")
        for name, value in (("socket", socket_path), ("slots", slots)):
            if value is not None and (
                not isinstance(value, str) or not os.path.isabs(value)
            ):
                raise ValueError(f"{name} must be an absolute path")
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ValueError("allowed_hosts must be a list of host names")
        return cls(socket_path, allowed_hosts(hosts), slots)


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


class HostSlots:
    """The host's live connections (Decision 0091, 3): every regular file of
    ``directory`` is one slot, held by a ``flock`` on a read-only descriptor
    for as long as a connection lives. Every instance opens the same files,
    so the limit holds across the sessions; the kernel releases a killed
    process's locks."""

    def __init__(self, directory: str) -> None:
        paths = []
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if stat.S_ISREG(os.lstat(path).st_mode):
                paths.append(path)
        self._paths = tuple(paths)

    def __len__(self) -> int:
        return len(self._paths)

    def acquire(self) -> int | None:
        """A held slot's descriptor, or ``None`` when every slot is taken."""
        count = len(self._paths)
        start = random.randrange(count) if count else 0
        for index in range(count):
            path = self._paths[(start + index) % count]
            try:
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            return fd
        return None

    @staticmethod
    def release(fd: int) -> None:
        os.close(fd)  # (closing the last descriptor releases the lock)


class _Tally:
    """One tunnel's bytes, and how much of them its lines have carried."""

    __slots__ = ("sent", "received", "_sent_logged", "_received_logged")

    def __init__(self) -> None:
        self.sent = self.received = 0
        self._sent_logged = self._received_logged = 0

    def delta(self) -> tuple[int, int]:
        sent = self.sent - self._sent_logged
        received = self.received - self._received_logged
        self._sent_logged, self._received_logged = self.sent, self.received
        return sent, received


class EgressProxy:
    """See the module: one instance serves one session (``session``, its UUID;
    ``-`` in development). ``resolver`` / ``connector`` are the seams of the
    tests (the defaults use the system's DNS and TCP); ``slots`` the host's
    connection slots (``None``: no host-wide limit, development)."""

    def __init__(
        self,
        hosts: frozenset[str],
        *,
        session: str = "-",
        resolver: Resolver = system_resolver,
        connector: Connector = system_connector,
        max_connections: int = MAX_SESSION_CONNECTIONS,
        max_new_per_second: int = MAX_NEW_PER_SECOND,
        slots: HostSlots | None = None,
        progress_seconds: float = PROGRESS_SECONDS,
        rate_report_seconds: float = RATE_REPORT_SECONDS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._hosts = hosts
        self._session = session
        self._resolver = resolver
        self._connector = connector
        self._max = max_connections
        self._max_new = max_new_per_second
        self._slots = slots
        self._progress = progress_seconds
        self._rate_report = rate_report_seconds
        self._idle = idle_timeout
        self._active = 0
        self._recent: collections.deque[float] = collections.deque()
        self._dropped = 0  # since the last ``outcome=rate`` line
        self._dropped_total = 0
        self._numbers = 0
        self._sent = self._received = 0
        self._refused: collections.Counter[str] = collections.Counter()
        self._handlers: set[asyncio.Task] = set()
        self._servers: list[asyncio.AbstractServer] = []
        self._reporter: asyncio.Task | None = None

    async def serve_socket(self, sock: socket.socket) -> asyncio.AbstractServer:
        """Serve a listening Unix socket that systemd made and passed
        (``paw-egress-session@<uuid>.socket``: group ``paw-cli-bridge``,
        mode 0660)."""
        server = await asyncio.start_unix_server(
            self.handle, sock=sock, limit=MAX_REQUEST_BYTES
        )
        return self._served(server)

    async def serve_unix(self, path: str) -> asyncio.AbstractServer:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        server = await asyncio.start_unix_server(
            self.handle, path=path, limit=MAX_REQUEST_BYTES
        )
        os.chmod(path, 0o660)
        return self._served(server)

    def _served(self, server: asyncio.AbstractServer) -> asyncio.AbstractServer:
        self._servers.append(server)
        if self._reporter is None:
            self._reporter = asyncio.create_task(self._report_rate_every())
        return server

    async def _report_rate_every(self) -> None:
        while True:
            await asyncio.sleep(self._rate_report)
            self._report_rate()

    def _report_rate(self) -> None:
        if self._dropped:
            logger.info(
                "session=%s outcome=rate count=%d", self._session, self._dropped
            )
            self._dropped = 0

    async def shutdown(self) -> None:
        """The instance stops (its session ended): no new connection, every
        open one is closed and logged, then the summary line."""
        for server in self._servers:
            server.close()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        if self._reporter is not None:
            self._reporter.cancel()
            await asyncio.gather(self._reporter, return_exceptions=True)
            self._reporter = None
        self._report_rate()
        refused = ",".join(
            f"{reason}:{count}" for reason, count in sorted(self._refused.items())
        )
        logger.info(
            "session=%s outcome=summary connections=%d refused=%s rate=%d"
            " sent=%d received=%d",
            self._session,
            self._numbers,
            refused or "-",
            self._dropped_total,
            self._sent,
            self._received,
        )

    def _too_fast(self) -> bool:
        """Whether this new connection is beyond ``max_new_per_second``
        (counted over the last second; one closed for it is not counted)."""
        now = asyncio.get_running_loop().time()
        while self._recent and now - self._recent[0] >= 1.0:
            self._recent.popleft()
        if len(self._recent) >= self._max_new:
            return True
        self._recent.append(now)
        return False

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            await self._handle(reader, writer)
        finally:
            self._handlers.discard(task)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._too_fast():
            # No line of its own (journald's rate limit): counted, and
            # reported every ``rate_report_seconds``.
            self._dropped += 1
            self._dropped_total += 1
            writer.transport.abort()
            return
        self._numbers += 1
        number = self._numbers
        if self._active >= self._max:
            await self._refuse(writer, number, Refused(503, "busy"))
            return
        slot = None
        if self._slots is not None:
            slot = self._slots.acquire()
            if slot is None:
                await self._refuse(writer, number, Refused(503, "busy"))
                return
        self._active += 1
        try:
            await self._serve(reader, writer, number)
        finally:
            self._active -= 1
            if slot is not None:
                HostSlots.release(slot)

    async def _refuse(
        self, writer: asyncio.StreamWriter, number: int, refused: Refused
    ) -> None:
        await _answer(writer, refused.status)
        self._refused[refused.reason] += 1
        self._log(number, refused.host, refused.reason, 0, 0)
        await _close(writer)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, number: int
    ) -> None:
        try:
            host = await self._read_request(reader)
            upstream_reader, upstream_writer = await self._open(host)
        except Refused as refused:
            await self._refuse(writer, number, refused)
            return
        tally = _Tally()
        outcome = "ok"
        progress = None
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            self._log(number, host, "open", 0, 0)
            progress = asyncio.create_task(self._report_progress(number, host, tally))
            if await self._splice(
                reader, writer, upstream_reader, upstream_writer, tally
            ):
                outcome = "error"
        except asyncio.CancelledError:
            outcome = "stopped"  # the instance stopped (its session ended)
            raise
        except Exception:
            outcome = "error"
        finally:
            if progress is not None:
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)
            await _close(upstream_writer)
            await _close(writer)
            self._sent += tally.sent
            self._received += tally.received
            self._log(number, host, outcome, *tally.delta())

    async def _report_progress(self, number: int, host: str, tally: _Tally) -> None:
        while True:
            await asyncio.sleep(self._progress)
            self._log(number, host, "progress", *tally.delta())

    def _log(
        self, number: int, host: str, outcome: str, sent: int, received: int
    ) -> None:
        logger.info(
            "session=%s conn=%d host=%s outcome=%s sent=%d received=%d",
            self._session,
            number,
            host,
            outcome,
            sent,
            received,
        )

    async def _read_request(self, reader: asyncio.StreamReader) -> str:
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
        parts = text.split("\r\n")[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise Refused(400, "malformed")
        # (No header is read: the session is the socket's, Decision 0091.)
        method, target, _ = parts
        host, separator, port = target.rpartition(":")
        # The refusal's log line names the host the CLI asked for (what the
        # allowlist may need), but only when it is a plain DNS name.
        shown = host if _HOST.fullmatch(host) else "-"
        if method != "CONNECT":
            raise Refused(405, "not_connect")
        if not separator or port != str(PORT):
            raise Refused(403, "port", shown)
        if host not in self._hosts:
            raise Refused(403, "host", shown)
        return host

    async def _open(
        self, host: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                addresses = await self._resolver(host)
        except TimeoutError:
            raise Refused(504, "dns_timeout", host) from None
        except OSError:
            raise Refused(502, "dns", host) from None
        public = [address for address in addresses if is_public(address[1])]
        if not public or len(public) != len(addresses):
            # One non-global answer refuses the host: a name that also points
            # into the LAN is not a provider's.
            raise Refused(403, "address", host)
        for address in public:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                    return await self._connector(address)
            except (OSError, TimeoutError):
                continue
        raise Refused(502, "connect", host)

    async def _splice(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        tally: _Tally,
    ) -> bool:
        """Copy both ways until both ends are done, a side fails, or both are
        idle, counting into ``tally``. Returns whether a side failed."""
        failed = False
        activity = asyncio.Event()

        async def pump(
            source: asyncio.StreamReader, sink: asyncio.StreamWriter, sent: bool
        ) -> None:
            while data := await source.read(_CHUNK):
                if sent:
                    tally.sent += len(data)
                else:
                    tally.received += len(data)
                activity.set()
                sink.write(data)
                await sink.drain()
            with contextlib.suppress(OSError):
                if sink.can_write_eof():
                    sink.write_eof()

        tasks = [
            asyncio.create_task(pump(client_reader, upstream_writer, True)),
            asyncio.create_task(pump(upstream_reader, client_writer, False)),
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
        return failed


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


async def forward(listen_port: int, socket_path: str) -> asyncio.AbstractServer:
    """Inside the container: ``127.0.0.1:<listen_port>`` to the proxy's
    mounted Unix socket (the session's own). Everything passes byte for byte:
    the session is the socket's, not anything the forwarder adds."""

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            upstream_reader, upstream_writer = await asyncio.open_unix_connection(
                socket_path
            )
        except OSError:
            await _close(writer)
            return

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


class StartRefused(Exception):
    """An instance that does not start (exit code 2, a fixed text)."""


def _session_proxy(
    config: Config, session: str, environ: Mapping[str, str]
) -> tuple[EgressProxy, socket.socket]:
    """``serve --session``: the session's instance, on the socket systemd
    passed (never one it makes), with the host's connection slots."""
    if not _UUID.fullmatch(session):
        raise StartRefused("the session must be a UUID")
    if config.slots is None:
        raise StartRefused("the host's connection slots (slots) are not configured")
    try:
        slots = HostSlots(config.slots)
    except OSError:
        raise StartRefused(
            "the host's connection slots (slots) cannot be read"
        ) from None
    if not len(slots):
        raise StartRefused("the host's connection slots (slots) are missing")
    try:
        inherited = inherited_socket(environ)
    except ValueError as error:
        raise StartRefused(str(error)) from None
    if inherited is None:
        raise StartRefused("no socket was passed (socket activation only)")
    proxy = EgressProxy(config.allowed_hosts, session=session, slots=slots)
    return proxy, inherited


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paw_egress.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--config", required=True)
    serve.add_argument("--session")
    relay = commands.add_parser("forward")
    relay.add_argument("--listen-port", type=int, required=True)
    relay.add_argument("--socket", required=True)
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    if arguments.command == "forward":

        async def relay_forever() -> None:
            server = await forward(arguments.listen_port, arguments.socket)
            async with server:
                await server.serve_forever()

        asyncio.run(relay_forever())
        return 0

    try:
        config = Config.load(arguments.config)
        if arguments.session is not None:
            proxy, inherited = _session_proxy(config, arguments.session, os.environ)
        elif config.socket_path is None:
            raise StartRefused("socket must be configured without --session")
        else:  # (development: the proxy makes the socket itself)
            proxy, inherited = EgressProxy(config.allowed_hosts), None
    except (StartRefused, ValueError, OSError) as error:
        reason = (
            str(error)
            if isinstance(error, StartRefused | ValueError)
            else (type(error).__name__)
        )
        print(f"paw-egress: refused ({reason})", file=sys.stderr)
        return 2

    async def run() -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                RESOLVER_THREADS, thread_name_prefix="paw-egress-dns"
            )
        )
        stopping = asyncio.Event()
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(number, stopping.set)
        if inherited is not None:
            await proxy.serve_socket(inherited)
        else:
            assert config.socket_path is not None
            await proxy.serve_unix(config.socket_path)
        await stopping.wait()
        # The session's unit stopped (systemd's SIGTERM): every open tunnel
        # is closed and logged, then the summary.
        await proxy.shutdown()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
