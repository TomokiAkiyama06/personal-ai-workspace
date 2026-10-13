"""``paw-egress`` (Decision 0087, 3): the CONNECT-only proxy of the CLI
sessions. Real sockets in a temporary directory and on the loopback; the DNS
answer and the connection to the "provider" are the test seams (the proxy
itself refuses every non-global address, the loopback included).

* an allowed host on port 443 is tunnelled byte for byte, and logged with the
  session and the counts (never the content);
* any other host, port or method, a malformed or oversized request, and a DNS
  answer with any non-global address are refused before anything is opened;
* the connection limit and the idle limit hold;
* the allowlist accepts plain lower-case DNS names only;
* the forwarder of the container passes TCP to the Unix socket.
"""

import asyncio
import contextlib
import importlib.util
import shutil
import socket
import sys
import tempfile
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

    async def start(self, **options):
        proxy = egress.EgressProxy(
            egress.allowed_hosts([HOST]),
            resolver=self.resolver,
            connector=self.connector,
            **options,
        )
        server = await proxy.serve_unix(self.socket_path)
        self.addAsyncCleanup(self._close_server, server)
        return proxy

    async def request(self, head: bytes):
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        writer.write(head)
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return status.split(b"\r\n")[0], reader, writer


class TunnelTest(ProxyCase):
    async def test_an_allowed_host_is_tunnelled_and_logged(self):
        await self.start()
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
        (line,) = logs.output
        self.assertIn(f"session={SESSION}", line)
        self.assertIn(f"host={HOST}", line)
        self.assertIn(f"sent={len(SECRET_BYTES)}", line)
        self.assertNotIn(SECRET_BYTES.decode(), line)
        self.assertEqual(self.connected, [PUBLIC])


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
        await self.start()
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


class LimitTest(ProxyCase):
    async def test_the_connection_limit_holds(self):
        await self.start(max_connections=1)
        _, _, first = await self.request(
            f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode()
        )
        status, _, second = await self.request(
            f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode()
        )
        self.assertEqual(status.split(b" ")[1], b"503")
        first.close()
        second.close()

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
            'socket = "/run/paw-egress/egress.sock"\n'
            'allowed_hosts = ["api.anthropic.com"]\n'
        )
        config = egress.Config.load(str(path))
        self.assertEqual(config.allowed_hosts, frozenset({"api.anthropic.com"}))
        path.write_text('socket = "relative.sock"\nallowed_hosts = []\n')
        with self.assertRaises(ValueError):
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
        self.assertTrue(egress.is_public("2606:4700::1"))
        self.assertFalse(
            any(ipaddress.ip_address("2606:4700::1") in n for n in networks)
        )


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


if __name__ == "__main__":
    unittest.main()
