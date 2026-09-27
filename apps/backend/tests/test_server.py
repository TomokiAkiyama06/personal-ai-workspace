import contextlib
import io
import logging
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn

from paw_backend.app import create_app
from paw_backend.server import (
    WEBSOCKET_MAX_MESSAGE_BYTES,
    ServerConfigurationError,
    build_server_config,
    main,
)

from .support import FakeDatabase, make_settings, paw_environment


class BuildServerConfigTest(unittest.TestCase):
    def build(self, **overrides):
        settings = make_settings(**overrides)
        return build_server_config(settings, create_app(settings))

    def test_loopback_plain_http_is_allowed_for_a_tls_proxy(self):
        config = self.build(port=8123)
        self.assertEqual((config.host, config.port), ("127.0.0.1", 8123))
        self.assertIsNone(config.ssl_certfile)

    def test_non_loopback_plain_http_is_refused(self):
        with self.assertRaises(ServerConfigurationError) as caught:
            self.build(host="0.0.0.0")
        self.assertIn("PAW_TLS_CERTFILE", str(caught.exception))

    def test_non_loopback_plain_http_needs_an_explicit_opt_in(self):
        config = self.build(host="0.0.0.0", allow_plaintext_http=True)
        self.assertEqual(config.host, "0.0.0.0")

    def test_uvicorn_terminates_tls_when_a_certificate_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory, "tls.crt")
            key = Path(directory, "tls.key")
            cert.write_text("placeholder")
            key.write_text("placeholder")
            config = self.build(host="0.0.0.0", tls_certfile=cert, tls_keyfile=key)
        self.assertEqual(config.ssl_certfile, cert)
        self.assertEqual(config.ssl_keyfile, key)

    def test_missing_tls_file_is_reported(self):
        with self.assertRaises(ServerConfigurationError) as caught:
            self.build(
                tls_certfile="/nonexistent/tls.crt", tls_keyfile="/nonexistent/tls.key"
            )
        self.assertIn("PAW_TLS_CERTFILE", str(caught.exception))

    def test_server_hardening_options(self):
        config = self.build()
        self.assertFalse(config.server_header)
        self.assertEqual(config.ws_max_size, WEBSOCKET_MAX_MESSAGE_BYTES)

    def test_proxy_headers_are_honoured_only_from_the_trusted_proxies(self):
        # The Origin check of PAW-022 compares the request's external scheme
        # (scope["scheme"]) and the login backoff counts the client address; both
        # are what Uvicorn makes of X-Forwarded-Proto / -For from a proxy in
        # FORWARDED_ALLOW_IPS (default: loopback), never from anybody else.
        config = self.build()
        self.assertTrue(config.proxy_headers)
        self.assertEqual(config.forwarded_allow_ips, "127.0.0.1,::1")
        with paw_environment(FORWARDED_ALLOW_IPS="10.0.0.5"):
            self.assertEqual(self.build().forwarded_allow_ips, "10.0.0.5")

    def test_graceful_shutdown_is_bounded(self):
        self.assertEqual(self.build().timeout_graceful_shutdown, 5)
        config = self.build(shutdown_timeout_seconds=2)
        self.assertEqual(config.timeout_graceful_shutdown, 2)


class ShutdownWithOpenStreamTest(unittest.TestCase):
    """A real Uvicorn on loopback: an open SSE stream must not block shutdown."""

    def test_shutdown_completes_while_an_event_stream_is_open(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        database = FakeDatabase()
        settings = make_settings(
            port=port,
            shutdown_timeout_seconds=1,
            event_heartbeat_seconds=0.05,
            log_level="warning",
        )
        config = build_server_config(settings, create_app(settings, database=database))
        # Uvicorn logs the streams it has to cancel at the deadline as errors.
        # That is the expected outcome here, so keep it out of the test output.
        config.log_config = None
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(self.stop, server, thread)
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.started, "the server did not start")

        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(
                b"GET /api/v1/events/stream HTTP/1.1\r\n"
                b"Host: localhost\r\nAccept: text/event-stream\r\n\r\n"
            )
            received = b""
            while b"system.heartbeat" not in received:
                received += client.recv(4096)

            # The client stays connected; only the server is asked to stop.
            server.should_exit = True
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive(), "shutdown hung on the open stream")
        self.assertTrue(database.disposed, "lifespan cleanup did not run")

    @staticmethod
    def stop(server, thread):
        server.should_exit = True
        server.force_exit = True
        thread.join(timeout=5)


class MainTest(unittest.TestCase):
    def test_invalid_configuration_exits_2_without_echoing_secrets(self):
        stderr = io.StringIO()
        with (
            paw_environment(PAW_DATABASE_URL="mysql://paw:s3cr3t-pw@db.internal/paw"),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("Invalid configuration", stderr.getvalue())
        self.assertNotIn("s3cr3t-pw", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
