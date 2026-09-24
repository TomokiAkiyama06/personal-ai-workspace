import re
import unittest

from fastapi.testclient import TestClient

from paw_backend.app import create_app

from .support import make_settings


class RequestIdTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(make_settings()))

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
        client = TestClient(create_app(make_settings()))
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
                self.assertEqual(
                    headers["strict-transport-security"], "max-age=31536000"
                )

    def test_hsts_max_age_is_configurable_and_can_be_turned_off(self):
        custom = TestClient(create_app(make_settings(hsts_max_age_seconds=600)))
        self.assertEqual(
            custom.get("/api/v1/health").headers["strict-transport-security"],
            "max-age=600",
        )
        off = TestClient(create_app(make_settings(hsts_max_age_seconds=0)))
        self.assertNotIn("strict-transport-security", off.get("/api/v1/health").headers)

    def test_server_version_is_not_advertised_by_the_application(self):
        response = TestClient(create_app(make_settings())).get("/api/v1/health")
        self.assertIsNone(re.search(r"uvicorn|fastapi", str(response.headers), re.I))


if __name__ == "__main__":
    unittest.main()
