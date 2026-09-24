import unittest

from starlette.testclient import WebSocketDenialResponse

from paw_backend.app import create_app

from .support import make_client, make_settings


class RequestIdTest(unittest.TestCase):
    def setUp(self):
        self.client = make_client(create_app(make_settings()))

    def test_generates_an_id_when_none_is_sent(self):
        first = self.client.get("/api/v1/health").headers["x-request-id"]
        second = self.client.get("/api/v1/health").headers["x-request-id"]
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first, second)

    def test_echoes_a_well_formed_inbound_id(self):
        response = self.client.get(
            "/api/v1/health", headers={"X-Request-ID": "trace-42.a_b"}
        )
        self.assertEqual(response.headers["x-request-id"], "trace-42.a_b")

    def test_replaces_a_malformed_inbound_id(self):
        for value in ("has space", "semi;colon", "x" * 65, "<script>"):
            with self.subTest(value=value):
                response = self.client.get(
                    "/api/v1/health", headers={"X-Request-ID": value}
                )
                self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")

    def test_error_body_carries_the_same_id(self):
        response = self.client.get("/nope", headers={"X-Request-ID": "abc-123"})
        self.assertEqual(response.headers["x-request-id"], "abc-123")
        self.assertEqual(response.json()["error"]["request_id"], "abc-123")


class SecurityHeadersTest(unittest.TestCase):
    def test_headers_are_on_success_and_error_responses(self):
        client = make_client(create_app(make_settings()))
        for path in ("/api/v1/health", "/api/v1/missing"):
            with self.subTest(path=path):
                headers = client.get(path).headers
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["x-frame-options"], "DENY")
                self.assertEqual(headers["referrer-policy"], "no-referrer")
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertIn(
                    "frame-ancestors 'none'", headers["content-security-policy"]
                )

    def test_hsts_is_sent_for_https_requests_only(self):
        app = create_app(make_settings())
        for path in ("/api/v1/health", "/api/v1/missing"):
            with self.subTest(path=path):
                https = make_client(app, base_url="https://localhost").get(path)
                self.assertEqual(
                    https.headers["strict-transport-security"], "max-age=31536000"
                )
                http = make_client(app).get(path)
                self.assertNotIn("strict-transport-security", http.headers)

    def test_hsts_is_sent_when_uvicorn_terminates_tls(self):
        settings = make_settings(
            tls_certfile="/etc/paw/tls.crt", tls_keyfile="/etc/paw/tls.key"
        )
        response = make_client(create_app(settings)).get("/api/v1/health")
        self.assertEqual(
            response.headers["strict-transport-security"], "max-age=31536000"
        )

    def test_hsts_max_age_is_configurable_and_can_be_turned_off(self):
        custom = make_client(
            create_app(make_settings(hsts_max_age_seconds=600)),
            base_url="https://localhost",
        )
        self.assertEqual(
            custom.get("/api/v1/health").headers["strict-transport-security"],
            "max-age=600",
        )
        off = make_client(
            create_app(make_settings(hsts_max_age_seconds=0)),
            base_url="https://localhost",
        )
        self.assertNotIn("strict-transport-security", off.get("/api/v1/health").headers)


class HostValidationTest(unittest.TestCase):
    def get(self, host: str, **settings):
        client = make_client(create_app(make_settings(**settings)))
        return client.get("/api/v1/health", headers={"Host": host})

    def test_default_allow_list_accepts_loopback_names_with_or_without_port(self):
        for host in ("localhost", "localhost:8000", "127.0.0.1:8443", "[::1]:8000"):
            with self.subTest(host=host):
                self.assertEqual(self.get(host).status_code, 200)

    def test_other_hosts_are_rejected_in_the_error_format(self):
        for host in ("evil.example", "evil.example:8000", "localhost.evil.example"):
            with self.subTest(host=host):
                response = self.get(host)
                self.assertEqual(response.status_code, 400)
                error = response.json()["error"]
                self.assertEqual(error["code"], "invalid_host")
                self.assertEqual(error["request_id"], response.headers["x-request-id"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_a_reverse_proxy_deployment_lists_its_public_host_name(self):
        settings = {"allowed_hosts": "Workspace.Example.org, localhost"}
        self.assertEqual(self.get("workspace.example.org", **settings).status_code, 200)
        self.assertEqual(
            self.get("WORKSPACE.example.org:443", **settings).status_code, 200
        )
        self.assertEqual(self.get("127.0.0.1", **settings).status_code, 400)

    def test_websocket_handshake_with_an_unknown_host_is_refused(self):
        client = make_client(create_app(make_settings()))
        with self.assertRaises(WebSocketDenialResponse) as caught:
            with client.websocket_connect("ws://evil.example/api/v1/events/ws"):
                pass
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
