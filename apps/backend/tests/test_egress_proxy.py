"""``paw-egress`` (Decision 0087, 3; Decision 0091): the CONNECT-only proxy of
the CLI sessions, one instance per session. Real sockets in a temporary
directory and on the loopback; the DNS answer and the connection to the
"provider" are the test seams (the proxy itself refuses every non-global
address, the loopback included). No systemd: the unit files are read.

* an allowed host on port 443 is tunnelled byte for byte, and logged with the
  session the instance was started for (never a header's) and the counts
  (never the content);
* any other host, port or method, a malformed or oversized request, and a DNS
  answer with any non-global address are refused before anything is opened;
* the session's limits (16 at once, 20 new a second, overflow summarized) and
  the host's (64 live connections through ``flock`` slot files, also across
  instances and processes), and the idle limit hold;
* every connection is logged when it opens, every 60 seconds while open and
  when it ends, with byte deltas that add up; a stop writes a summary;
* ``serve --session`` takes only the socket systemd passed;
* the allowlist accepts plain lower-case DNS names only;
* the forwarder of the container passes TCP to the Unix socket.
"""

import asyncio
import contextlib
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "deploy" / "egress" / "paw_egress.py"


def _load():
    spec = importlib.util.spec_from_file_location("paw_egress", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


egress = _load()

HOST = "api.anthropic.com"
PUBLIC = (socket.AF_INET, "160.79.104.10")
SESSION = "6f1c2d3e-4a5b-4c6d-8e7f-001122334455"
SECRET_BYTES = b"paw-egress-canary-" + b"7a1c" + b"-tls-bytes"


class ProxyCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.socket_path = str(self.directory / "egress.sock")
        # The "provider": an echo server on the loopback.
        self.upstream = await asyncio.start_server(self._echo, "127.0.0.1", 0)
        self.addAsyncCleanup(self._close_server, self.upstream)
        self.upstream_port = self.upstream.sockets[0].getsockname()[1]
        self.answers = {HOST: [PUBLIC]}
        self.connected = []

    async def _close_server(self, server):
        server.close()
        await server.wait_closed()

    async def _echo(self, reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    async def resolver(self, host):
        return self.answers[host]

    async def connector(self, address):
        self.connected.append(address)
        return await asyncio.open_connection("127.0.0.1", self.upstream_port)

    async def start(self, path=None, **options):
        proxy = egress.EgressProxy(
            egress.allowed_hosts([HOST]),
            resolver=self.resolver,
            connector=self.connector,
            **options,
        )
        server = await proxy.serve_unix(path or self.socket_path)
        self.addAsyncCleanup(self._close_server, server)
        return proxy

    async def request(self, head: bytes, path=None):
        reader, writer = await asyncio.open_unix_connection(path or self.socket_path)
        writer.write(head)
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return status.split(b"\r\n")[0], reader, writer


class TunnelTest(ProxyCase):
    async def test_an_allowed_host_is_tunnelled_and_logged(self):
        await self.start(session=SESSION)
        with self.assertLogs("paw_egress", "INFO") as logs:
            status, reader, writer = await self.request(
                f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n"
                f"X-PAW-Session: {SESSION}\r\n\r\n".encode()
            )
            self.assertEqual(status, b"HTTP/1.1 200 Connection Established")
            writer.write(SECRET_BYTES)
            await writer.drain()
            self.assertEqual(
                await asyncio.wait_for(reader.readexactly(len(SECRET_BYTES)), 5),
                SECRET_BYTES,
            )
            writer.write_eof()
            await asyncio.wait_for(reader.read(), 5)
            writer.close()
            await asyncio.sleep(0.1)
        opened, line = logs.output
        self.assertIn("outcome=open", opened)
        self.assertIn(f"session={SESSION}", line)
        self.assertIn(f"host={HOST}", line)
        self.assertIn("outcome=ok", line)
        self.assertIn(f"sent={len(SECRET_BYTES)}", line)
        self.assertNotIn(SECRET_BYTES.decode(), line)
        self.assertEqual(self.connected, [PUBLIC])

    async def test_a_header_does_not_change_the_session(self):
        # The session is the socket's (the instance's argument): a header the
        # container sends, forged or not, is not read (Decision 0091, 1).
        await self.start(session=SESSION)
        forged = "00000000-0000-4000-8000-000000000000"
        with self.assertLogs("paw_egress", "INFO") as logs:
            _, _, writer = await self.request(
                f"CONNECT {HOST}:443 HTTP/1.1\r\n".encode()
                + f"X-PAW-Session: {forged}\r\n\r\n".encode()
            )
            writer.close()
            _, _, writer = await self.request(
                b"CONNECT statsig.anthropic.com:443 HTTP/1.1\r\n"
                + f"X-PAW-Session: {forged}\r\n\r\n".encode()
            )
            writer.close()
            await asyncio.sleep(0.2)
        self.assertTrue(logs.output)
        for line in logs.output:
            with self.subTest(line=line):
                self.assertIn(f"session={SESSION} ", line)
                self.assertNotIn(forged, line)

    async def test_without_a_session_the_log_says_none(self):
        # Development (no socket activation): no session, never a header's.
        await self.start()
        with self.assertLogs("paw_egress", "INFO") as logs:
            _, _, writer = await self.request(
                f"CONNECT {HOST}:443 HTTP/1.1\r\n".encode()
                + f"X-PAW-Session: {SESSION}\r\n\r\n".encode()
            )
            writer.close()
            await asyncio.sleep(0.2)
        for line in logs.output:
            self.assertIn("session=- ", line)


class RefusalTest(ProxyCase):
    async def refused(self, head: bytes, status: bytes):
        got, _, writer = await self.request(head)
        writer.close()
        self.assertEqual(got.split(b" ")[1], status)
        self.assertEqual(self.connected, [])

    async def test_other_hosts_ports_and_methods_are_refused(self):
        await self.start()
        for head, status in (
            (b"CONNECT evil.example.com:443 HTTP/1.1\r\n\r\n", b"403"),
            (b"CONNECT API.ANTHROPIC.COM:443 HTTP/1.1\r\n\r\n", b"403"),
            (f"CONNECT {HOST}:80 HTTP/1.1\r\n\r\n".encode(), b"403"),
            (f"CONNECT {HOST} HTTP/1.1\r\n\r\n".encode(), b"403"),
            (f"GET http://{HOST}/ HTTP/1.1\r\n\r\n".encode(), b"405"),
            (b"CONNECT 160.79.104.10:443 HTTP/1.1\r\n\r\n", b"403"),
            (b"nonsense\r\n\r\n", b"400"),
        ):
            with self.subTest(head=head):
                await self.refused(head, status)

    async def test_an_oversized_request_is_refused(self):
        await self.start()
        head = (
            f"CONNECT {HOST}:443 HTTP/1.1\r\nX-Pad: ".encode()
            + b"a" * 20_000
            + b"\r\n\r\n"
        )
        await self.refused(head, b"400")

    async def test_a_dns_answer_with_any_non_global_address_is_refused(self):
        await self.start()
        for answer in (
            [(socket.AF_INET, "10.0.0.5")],
            [(socket.AF_INET, "127.0.0.1")],
            [(socket.AF_INET, "100.64.0.1")],
            [(socket.AF_INET, "169.254.169.254")],
            [(socket.AF_INET6, "::1")],
            [(socket.AF_INET6, "::ffff:192.168.1.1")],
            [(socket.AF_INET6, "fd00::1")],
            [(socket.AF_INET6, "fec0::1")],
            [(socket.AF_INET6, "4000::1")],
            [(socket.AF_INET, "240.0.0.1")],
            [PUBLIC, (socket.AF_INET, "192.168.0.10")],
            [],
        ):
            with self.subTest(answer=answer):
                self.answers[HOST] = answer
                await self.refused(
                    f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode(), b"403"
                )


class RefusalLogTest(ProxyCase):
    async def test_a_refusal_names_the_host_and_the_session(self):
        # What the operator reads to decide on the allowlist (README).
        await self.start(session=SESSION)
        with self.assertLogs("paw_egress", "INFO") as logs:
            _, _, writer = await self.request(
                b"CONNECT statsig.anthropic.com:443 HTTP/1.1\r\n"
                + f"X-PAW-Session: {SESSION}\r\n\r\n".encode()
            )
            writer.close()
            _, _, writer = await self.request(
                b"CONNECT bad host\x7f:443 HTTP/1.1\r\n\r\n"
            )
            writer.close()
            await asyncio.sleep(0.05)
        first, second = logs.output
        self.assertIn("host=statsig.anthropic.com", first)
        self.assertIn(f"session={SESSION}", first)
        self.assertIn("outcome=host", first)
        self.assertIn("host=-", second)  # not a DNS name: not repeated
        # Each connection has its own number in the instance.
        numbers = [re.search(r" conn=(\d+) ", line).group(1) for line in logs.output]
        self.assertEqual(len(set(numbers)), 2)


class FailureLogTest(ProxyCase):
    async def test_a_failed_tunnel_is_logged_as_an_error(self):
        async def resetting(reader, writer):
            await reader.read(1)
            writer.transport.abort()  # the provider resets the connection

        self.upstream.close()
        await self.upstream.wait_closed()
        self.upstream = await asyncio.start_server(resetting, "127.0.0.1", 0)
        self.upstream_port = self.upstream.sockets[0].getsockname()[1]
        await self.start()
        with self.assertLogs("paw_egress", "INFO") as logs:
            _, reader, writer = await self.request(
                f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode()
            )
            with contextlib.suppress(ConnectionError):
                for _ in range(20):
                    writer.write(SECRET_BYTES)
                    await writer.drain()
                    await asyncio.sleep(0.01)
            writer.close()
            await asyncio.sleep(0.2)
        self.assertIn("outcome=error", logs.output[-1])


CONNECT = f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode()


class LimitTest(ProxyCase):
    async def test_the_sessions_limit_is_16_at_once(self):
        self.assertEqual(egress.MAX_SESSION_CONNECTIONS, 16)
        await self.start(session=SESSION)
        writers = []
        self.addCleanup(lambda: [w.close() for w in writers])
        for _ in range(16):
            status, _, writer = await self.request(CONNECT)
            writers.append(writer)
            self.assertEqual(status, b"HTTP/1.1 200 Connection Established")
        with self.assertLogs("paw_egress", "INFO") as logs:
            status, _, writer = await self.request(CONNECT)
            writers.append(writer)
            await asyncio.sleep(0.05)
        self.assertEqual(status.split(b" ")[1], b"503")
        (line,) = logs.output
        self.assertIn("outcome=busy", line)
        self.assertIn(f"session={SESSION} ", line)

    async def test_more_than_20_new_connections_a_second_are_closed_and_counted(self):
        self.assertEqual(egress.MAX_NEW_PER_SECOND, 20)
        proxy = await self.start(session=SESSION, rate_report_seconds=0.3)
        refused = b"CONNECT other.example.com:443 HTTP/1.1\r\n\r\n"
        with self.assertLogs("paw_egress", "INFO") as logs:
            for _ in range(20):
                status, _, writer = await self.request(refused)
                writer.close()
                self.assertEqual(status.split(b" ")[1], b"403")
            # The 21st within the second: closed, no line of its own.
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
            writer.write(CONNECT)
            try:
                answer = await asyncio.wait_for(reader.read(), 5)
            except ConnectionError:
                answer = b""
            writer.close()
            self.assertNotIn(b"200", answer)
            await asyncio.sleep(0.6)  # the 10-second summary (here 0.3 s)
        lines = [line for line in logs.output if "outcome=host" in line]
        self.assertEqual(len(lines), 20)
        (rate,) = [line for line in logs.output if "outcome=rate" in line]
        self.assertIn(f"session={SESSION} ", rate)
        self.assertIn("count=1", rate)
        self.assertEqual(self.connected, [])
        # A second later new connections are taken again.
        await asyncio.sleep(0.5)
        status, _, writer = await self.request(CONNECT)
        writer.close()
        self.assertEqual(status, b"HTTP/1.1 200 Connection Established")
        del proxy

    async def test_an_idle_tunnel_is_closed(self):
        await self.start(idle_timeout=0.2)
        _, reader, writer = await self.request(
            f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode()
        )
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        writer.close()


class AllowlistTest(unittest.TestCase):
    def test_only_plain_lower_case_names_are_accepted(self):
        egress.allowed_hosts(["api.anthropic.com", "platform.claude.com"])
        for bad in (
            "*.anthropic.com",
            "API.anthropic.com",
            "api.anthropic.com:443",
            "160.79.104.10",
            "localhost",
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                egress.allowed_hosts([bad])

    def test_the_configuration_file(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory / "egress.toml"
        path.write_text(
            'slots = "/run/paw-egress/slots"\nallowed_hosts = ["api.anthropic.com"]\n'
        )
        config = egress.Config.load(str(path))
        self.assertEqual(config.allowed_hosts, frozenset({"api.anthropic.com"}))
        self.assertEqual(config.slots, "/run/paw-egress/slots")
        self.assertIsNone(config.socket_path)  # development only
        for bad in (
            'socket = "relative.sock"\nallowed_hosts = []\n',
            'slots = "slots"\nallowed_hosts = []\n',
        ):
            path.write_text(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                egress.Config.load(str(path))


class NftablesTest(unittest.TestCase):
    """The nftables example (the second line) refuses at least what the
    proxy refuses inside the global unicast block 2000::/3."""

    def test_the_special_ranges_inside_2000_3_are_dropped(self):
        import ipaddress
        import re

        text = (PATH.parent / "paw-egress.nft").read_text()
        block = text[text.index("set not_global6") :]
        block = block[: block.index("}", block.index("elements"))]
        networks = [
            ipaddress.ip_network(item) for item in re.findall(r"[0-9a-f:]+/\d+", block)
        ]
        # Every special-purpose block inside 2000::/3 that Python's ``ipaddress``
        # knows (the IANA registry it follows), and a few addresses in them.
        special = [
            network
            for network in ipaddress._IPv6Constants._private_networks
            if network.subnet_of(ipaddress.ip_network("2000::/3"))
        ]
        self.assertTrue(special)
        for block in special:
            with self.subTest(block=str(block)):
                self.assertTrue(any(block.subnet_of(n) for n in networks))
        for address in (
            "2001:2::1",
            "2001::1",
            "2001:10::1",
            "2002::1",
            "2001:db8::1",
            "3fff::1",
        ):
            with self.subTest(address=address):
                self.assertFalse(egress.is_public(address))
                ip = ipaddress.ip_address(address)
                self.assertTrue(any(ip in network for network in networks))
        # On purpose: the globally reachable exceptions inside 2001::/23 are
        # dropped here too (the example's comment says so); the proxy alone
        # would let them through.
        for block in ipaddress._IPv6Constants._private_networks_exceptions:
            with self.subTest(exception=str(block)):
                self.assertTrue(any(block.subnet_of(n) for n in networks))
        self.assertIn("2001::/23 is dropped whole on purpose", text)
        self.assertTrue(egress.is_public("2606:4700::1"))
        self.assertFalse(
            any(ipaddress.ip_address("2606:4700::1") in n for n in networks)
        )


class NftablesRulesTest(unittest.TestCase):
    def test_no_shortcut_for_established_flows(self):
        text = (PATH.parent / "paw-egress.nft").read_text()
        rules = [line for line in text.splitlines() if not line.strip().startswith("#")]
        self.assertFalse([line for line in rules if "ct state" in line])
        # Every accept (but the first, for the other accounts) is for flows
        # the account started: replies to inbound connections never pass.
        accepts = [
            line.strip()
            for line in rules
            if line.strip().endswith("accept") and "skuid" not in line
        ]
        self.assertTrue(accepts)
        for line in accepts:
            with self.subTest(line=line):
                self.assertIn("ct direction original", line)

    def test_the_install_steps_add_the_cli_account_to_the_group(self):
        unit = (PATH.parent / "paw-egress-session@.service").read_text()
        self.assertIn("usermod -aG paw-cli-bridge paw-agent-cli", unit)


class InheritedSocketTest(ProxyCase):
    def listening(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self.socket_path)
        sock.listen()
        return sock

    def test_only_a_socket_passed_to_this_process_is_taken(self):
        sock = self.listening()
        self.addCleanup(sock.close)
        fd = sock.fileno()
        self.assertIsNone(egress.inherited_socket({}, fd=fd))
        other = {"LISTEN_PID": str(os.getpid() + 1), "LISTEN_FDS": "1"}
        self.assertIsNone(egress.inherited_socket(other, fd=fd))
        two = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "2"}
        with self.assertRaises(ValueError):
            egress.inherited_socket(two, fd=fd)

    async def test_the_proxy_serves_the_inherited_socket(self):
        sock = self.listening()
        env = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"}
        inherited = egress.inherited_socket(env, fd=os.dup(sock.fileno()))
        sock.close()
        proxy = egress.EgressProxy(
            egress.allowed_hosts([HOST]),
            resolver=self.resolver,
            connector=self.connector,
        )
        server = await proxy.serve_socket(inherited)
        self.addAsyncCleanup(self._close_server, server)
        status, reader, writer = await self.request(
            f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode()
        )
        self.assertEqual(status, b"HTTP/1.1 200 Connection Established")
        writer.close()


class ForwardTest(ProxyCase):
    async def test_the_forwarder_passes_tcp_to_the_socket(self):
        await self.start()
        server = await egress.forward(0, self.socket_path)
        self.addAsyncCleanup(self._close_server, server)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        self.assertTrue(status.startswith(b"HTTP/1.1 200"))
        writer.write(SECRET_BYTES)
        await writer.drain()
        self.assertEqual(
            await asyncio.wait_for(reader.readexactly(len(SECRET_BYTES)), 5),
            SECRET_BYTES,
        )
        writer.close()


class ForwardHalfCloseTest(unittest.IsolatedAsyncioTestCase):
    """A half-close (``write_eof``) on one side is passed on as one: the
    other direction stays open until it ends too."""

    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.socket_path = str(self.directory / "upstream.sock")
        self.received = asyncio.get_running_loop().create_future()

    async def serve(self, handler):
        upstream = await asyncio.start_unix_server(handler, path=self.socket_path)
        self.addAsyncCleanup(_close, upstream)
        server = await egress.forward(0, self.socket_path)
        self.addAsyncCleanup(_close, server)
        return server.sockets[0].getsockname()[1]

    async def test_the_answer_after_the_clients_eof_still_arrives(self):
        async def answer_after_eof(reader, writer):
            request = await reader.read()  # up to the client's EOF
            await asyncio.sleep(0.1)
            writer.write(b"answer to " + request)
            await writer.drain()
            writer.close()

        port = await self.serve(answer_after_eof)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"question")
        await writer.drain()
        writer.write_eof()
        self.assertEqual(
            await asyncio.wait_for(reader.read(), 5), b"answer to question"
        )
        writer.close()

    async def test_the_client_can_still_send_after_the_upstreams_eof(self):
        async def eof_first(reader, writer):
            writer.write(b"hello")
            await writer.drain()
            writer.write_eof()
            self.received.set_result(await reader.read())
            writer.close()

        port = await self.serve(eof_first)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"hello")
        writer.write(b"late bytes")
        await writer.drain()
        writer.write_eof()
        self.assertEqual(await asyncio.wait_for(self.received, 5), b"late bytes")
        writer.close()


def _fields(line):
    message = line.split(":", 2)[2]  # (assertLogs: "INFO:paw_egress:...")
    return dict(item.split("=", 1) for item in message.split() if "=" in item)


class ConnectionLogTest(ProxyCase):
    """Decision 0091, 3: a line when a tunnel opens, every 60 seconds while it
    is open and when it ends, all with ``conn=``; the byte counts are deltas
    from the previous line, so the lines add up to the connection's total."""

    async def test_open_progress_and_end_lines_add_up(self):
        await self.start(session=SESSION, progress_seconds=0.2)
        with self.assertLogs("paw_egress", "INFO") as logs:
            status, reader, writer = await self.request(CONNECT)
            self.assertEqual(status, b"HTTP/1.1 200 Connection Established")
            total = 0
            for size in (1000, 2500, 7):
                writer.write(b"x" * size)
                await writer.drain()
                await asyncio.wait_for(reader.readexactly(size), 5)
                total += size
                await asyncio.sleep(0.45)
            writer.write_eof()
            await asyncio.wait_for(reader.read(), 5)
            writer.close()
            await asyncio.sleep(0.1)
        lines = [_fields(line) for line in logs.output]
        self.assertTrue(all(f["session"] == SESSION for f in lines))
        self.assertEqual(len({f["conn"] for f in lines}), 1)
        outcomes = [f["outcome"] for f in lines]
        self.assertEqual(outcomes[0], "open")
        self.assertEqual(outcomes[-1], "ok")
        self.assertGreaterEqual(outcomes.count("progress"), 2)
        self.assertEqual(sum(int(f["sent"]) for f in lines), total)
        self.assertEqual(sum(int(f["received"]) for f in lines), total)
        # Deltas, not running totals: no line carries all of it but when it
        # is the only line with bytes.
        with_bytes = [f for f in lines if int(f["sent"])]
        self.assertGreaterEqual(len(with_bytes), 2)

    async def test_the_stop_writes_a_summary(self):
        proxy = await self.start(session=SESSION)
        status, reader, writer = await self.request(CONNECT)
        writer.write(SECRET_BYTES)
        await writer.drain()
        await asyncio.wait_for(reader.readexactly(len(SECRET_BYTES)), 5)
        _, _, refused = await self.request(
            b"CONNECT statsig.anthropic.com:443 HTTP/1.1\r\n\r\n"
        )
        refused.close()
        await asyncio.sleep(0.1)
        with self.assertLogs("paw_egress", "INFO") as logs:
            # The instance stops (the session ended) with a tunnel still open:
            # it is closed and logged, then the summary.
            await proxy.shutdown()
        writer.close()
        ended, summary = logs.output
        self.assertIn("outcome=", ended)
        self.assertIn(f"sent={len(SECRET_BYTES)}", ended)
        fields = _fields(summary)
        self.assertEqual(fields["outcome"], "summary")
        self.assertEqual(fields["session"], SESSION)
        self.assertEqual(fields["connections"], "2")
        self.assertEqual(fields["sent"], str(len(SECRET_BYTES)))
        self.assertEqual(fields["received"], str(len(SECRET_BYTES)))
        self.assertEqual(fields["refused"], "host:1")
        self.assertNotIn(SECRET_BYTES.decode(), summary)


class HostSlotsTest(ProxyCase):
    """Decision 0091, 3: 64 live connections on the whole host, counted with
    ``flock`` on slot files every instance opens read-only."""

    def slot_directory(self, count):
        directory = self.directory / "slots"
        directory.mkdir()
        for index in range(count):
            path = directory / f"{index:02d}"
            path.touch()
            path.chmod(0o400)  # read-only is enough for flock
        return directory

    async def test_the_65th_connection_is_busy_from_another_instance(self):
        self.assertEqual(egress.HOST_CONNECTIONS, 64)
        slots = self.slot_directory(64)
        paths = []
        for index in range(5):
            path = str(self.directory / f"session-{index}.sock")
            await self.start(path, session=SESSION, slots=egress.HostSlots(str(slots)))
            paths.append(path)
        writers = []
        self.addCleanup(lambda: [w.close() for w in writers])
        for index in range(64):
            status, _, writer = await self.request(CONNECT, paths[index // 16])
            writers.append(writer)
            self.assertEqual(status, b"HTTP/1.1 200 Connection Established")
        with self.assertLogs("paw_egress", "INFO") as logs:
            status, _, writer = await self.request(CONNECT, paths[4])
            writers.append(writer)
            await asyncio.sleep(0.05)
        self.assertEqual(status.split(b" ")[1], b"503")
        self.assertIn("outcome=busy", logs.output[-1])
        # A closed tunnel gives its slot back.
        writers[0].close()
        await asyncio.sleep(0.2)
        status, _, writer = await self.request(CONNECT, paths[4])
        writers.append(writer)
        self.assertEqual(status, b"HTTP/1.1 200 Connection Established")

    async def test_a_killed_process_gives_its_slot_back(self):
        slots = self.slot_directory(1)
        script = textwrap.dedent(
            f"""
            import importlib.util, sys, time
            spec = importlib.util.spec_from_file_location("e", {str(PATH)!r})
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            slot = module.HostSlots({str(slots)!r}).acquire()
            print("held" if slot is not None else "none", flush=True)
            time.sleep(60)
            """
        )
        holder = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE
        )
        self.addAsyncCleanup(holder.wait)
        self.addCleanup(lambda: holder.returncode is None and holder.kill())
        line = await asyncio.wait_for(holder.stdout.readline(), 30)
        self.assertEqual(line.strip(), b"held")
        await self.start(slots=egress.HostSlots(str(slots)))
        status, _, writer = await self.request(CONNECT)
        writer.close()
        self.assertEqual(status.split(b" ")[1], b"503")
        holder.kill()  # (an OOM kill: no clean-up of its own)
        await holder.wait()
        status, _, writer = await self.request(CONNECT)
        writer.close()
        self.assertEqual(status, b"HTTP/1.1 200 Connection Established")

    def test_only_regular_files_are_slots(self):
        slots = self.slot_directory(2)
        (slots / "link").symlink_to(slots / "00")
        (slots / "sub").mkdir()
        self.assertEqual(len(egress.HostSlots(str(slots))), 2)


class ServeCommandTest(unittest.TestCase):
    """``serve --session``: the session's socket is the one systemd passed;
    without it the instance does not start (it never makes one itself)."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        (self.directory / "slots").mkdir()
        (self.directory / "slots" / "00").touch()
        self.config = self.directory / "egress.toml"
        self.config.write_text(
            f'socket = "{self.directory / "dev.sock"}"\n'
            f'slots = "{self.directory / "slots"}"\n'
            'allowed_hosts = ["api.anthropic.com"]\n'
        )

    def run_serve(self, *extra, env=None):
        environment = {
            k: v for k, v in os.environ.items() if not k.startswith("LISTEN_")
        }
        environment.update(env or {})
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(PATH),
                "serve",
                "--config",
                str(self.config),
                *extra,
            ],
            env=environment,
            capture_output=True,
            timeout=30,
        )

    def test_a_session_without_a_passed_socket_does_not_start(self):
        result = self.run_serve("--session", SESSION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"socket", result.stderr)
        self.assertFalse((self.directory / "dev.sock").exists())

    def test_the_session_must_be_a_uuid(self):
        result = self.run_serve("--session", "../x", env={"LISTEN_FDS": "1"})
        self.assertNotEqual(result.returncode, 0)

    def test_a_session_without_the_host_slots_does_not_start(self):
        self.config.write_text('allowed_hosts = ["api.anthropic.com"]\n')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.directory / "passed.sock"))
        sock.listen()
        self.addCleanup(sock.close)
        # (LISTEN_PID cannot name the child before it exists: the check of
        # the slots comes first, from the configuration alone.)
        result = self.run_serve("--session", SESSION)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"slots", result.stderr)

    def test_the_forwarder_takes_no_session(self):
        with self.assertRaises(SystemExit):
            egress.main(
                [
                    "forward",
                    "--listen-port",
                    "0",
                    "--socket",
                    "/x",
                    "--session",
                    SESSION,
                ]
            )
        self.assertFalse(hasattr(egress, "_SESSION_HEADER"))


class SessionUnitTest(unittest.TestCase):
    """The per-session units (Decision 0091, 1 and 3), read as files."""

    def directives(self, name):
        text = (PATH.parent / name).read_text()
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def value(self, directives, key):
        (found,) = [d.split("=", 1)[1] for d in directives if d.startswith(key + "=")]
        return found

    def test_the_shared_socket_is_gone(self):
        self.assertFalse((PATH.parent / "paw-egress.socket").exists())
        self.assertFalse((PATH.parent / "paw-egress.service").exists())
        example = (PATH.parent / "egress.example.toml").read_text()
        self.assertNotIn("/run/paw-egress/egress.sock", example)

    def test_the_session_socket(self):
        unit = self.directives("paw-egress-session@.socket")
        self.assertIn("ListenStream=/run/paw-egress/session/%i.sock", unit)
        self.assertIn("SocketUser=paw-egress", unit)
        self.assertIn("SocketGroup=paw-cli-bridge", unit)
        self.assertIn("SocketMode=0660", unit)
        self.assertIn("DirectoryMode=0755", unit)
        self.assertIn("RemoveOnStop=yes", unit)
        self.assertIn("BindsTo=paw-cli-session@%i.service", unit)
        self.assertFalse(
            [d for d in unit if d.startswith("Accept=") and d != "Accept=no"]
        )
        self.assertFalse([d for d in unit if d.startswith("WantedBy")])

    def test_the_session_proxy_is_in_the_slice_with_its_own_limits(self):
        unit = self.directives("paw-egress-session@.service")
        self.assertIn("User=paw-egress", unit)
        self.assertIn("Group=paw-egress", unit)
        self.assertFalse([d for d in unit if "paw-cli-bridge" in d])
        self.assertIn("Slice=paw-egress.slice", unit)
        self.assertIn("Requires=paw-egress-session@%i.socket", unit)
        self.assertIn("BindsTo=paw-egress-session@%i.socket", unit)
        self.assertIn("paw-egress-session@%i.socket", self.value(unit, "After"))
        self.assertIn("--session %i", self.value(unit, "ExecStart"))
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("CapabilityBoundingSet=", unit)
        self.assertFalse([d for d in unit if d.startswith("RuntimeDirectory")])
        memory = _bytes(self.value(unit, "MemoryMax"))
        tasks = int(self.value(unit, "TasksMax"))
        self.assertGreater(tasks, egress.RESOLVER_THREADS + 1)
        slice_unit = self.directives("paw-egress.slice")
        # The slice's limits: about 32 proxies' threads; its memory above the
        # proxies' own limits together (a last guard that does not choose).
        self.assertEqual(int(self.value(slice_unit, "TasksMax")), 32 * tasks)
        self.assertGreater(_bytes(self.value(slice_unit, "MemoryMax")), 32 * memory)

    def test_the_slot_files_are_made_by_tmpfiles(self):
        lines = [
            line.split()
            for line in (PATH.parent / "paw-egress.tmpfiles.conf")
            .read_text()
            .splitlines()
            if line.strip() and not line.startswith("#")
        ]
        directories = [line for line in lines if line[0] == "d"]
        self.assertIn(
            ["d", "/run/paw-egress/slots", "0700", "paw-egress", "paw-egress", "-"],
            directories,
        )
        files = [line for line in lines if line[0] == "f"]
        self.assertEqual(len(files), egress.HOST_CONNECTIONS)
        self.assertEqual(len({line[1] for line in files}), egress.HOST_CONNECTIONS)
        for line in files:
            self.assertTrue(line[1].startswith("/run/paw-egress/slots/"))
            self.assertEqual(line[2:5], ["0600", "paw-egress", "paw-egress"])
        example = (PATH.parent / "egress.example.toml").read_text()
        self.assertIn('slots = "/run/paw-egress/slots"', example)


def _bytes(value):
    units = {"K": 1024, "M": 1024**2, "G": 1024**3}
    return int(value[:-1]) * units[value[-1]] if value[-1] in units else int(value)


async def _close(server):
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
