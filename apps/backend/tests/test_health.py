import logging
import unittest

from fastapi.testclient import TestClient

from paw_backend.app import create_app
from paw_backend.db import Database, DatabaseStatus

from .support import FakeDatabase, make_settings

PASSWORD = "s3cr3t-pw"


class LivenessTest(unittest.TestCase):
    def test_liveness_does_not_depend_on_the_database(self):
        for status in DatabaseStatus:
            with self.subTest(status=status):
                app = create_app(make_settings(), database=FakeDatabase(status))
                response = TestClient(app).get("/api/v1/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok"})

    def test_app_starts_and_serves_liveness_with_an_unreachable_database(self):
        settings = make_settings(
            database_url=f"postgresql://paw:{PASSWORD}@127.0.0.1:1/paw"
        )
        # Entering the client context runs the lifespan (application startup).
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)


class ReadinessTest(unittest.TestCase):
    def get_ready(self, database: Database):
        app = create_app(make_settings(), database=database)
        return TestClient(app).get("/api/v1/health/ready")

    def test_ready_when_the_database_answers(self):
        response = self.get_ready(FakeDatabase(DatabaseStatus.OK))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"status": "ok", "checks": {"database": "ok"}}
        )

    def test_unavailable_database_gives_503_with_a_stable_body(self):
        response = self.get_ready(FakeDatabase(DatabaseStatus.UNAVAILABLE))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "checks": {"database": "unavailable"}},
        )

    def test_unconfigured_database_gives_503_with_a_distinct_check(self):
        response = self.get_ready(FakeDatabase(DatabaseStatus.NOT_CONFIGURED))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "checks": {"database": "not_configured"}},
        )

    def test_default_database_without_url_reports_not_configured(self):
        app = create_app(make_settings())
        response = TestClient(app).get("/api/v1/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"], {"database": "not_configured"})

    def test_real_connection_failure_never_leaks_connection_details(self):
        # Port 1 refuses the connection immediately; no server is involved.
        settings = make_settings(
            database_url=f"postgresql://paw-user:{PASSWORD}@127.0.0.1:1/paw-db",
            database_timeout_seconds=2,
        )
        app = create_app(settings)
        with self.assertLogs("paw_backend.db", level=logging.WARNING) as logs:
            response = TestClient(app).get("/api/v1/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "unavailable", "checks": {"database": "unavailable"}},
        )
        output = response.text + "\n".join(logs.output) + repr(response.headers)
        for secret in (PASSWORD, "paw-user", "paw-db", "127.0.0.1"):
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
