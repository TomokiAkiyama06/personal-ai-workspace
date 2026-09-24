import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from paw_backend.app import create_app
from paw_backend.server import (
    WEBSOCKET_MAX_MESSAGE_BYTES,
    ServerConfigurationError,
    build_server_config,
    main,
)

from .support import make_settings, paw_environment


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
