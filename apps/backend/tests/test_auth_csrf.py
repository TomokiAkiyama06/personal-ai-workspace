"""The Origin check for state-changing requests (CSRF layer of PAW-022)."""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from paw_backend.app import create_app
from paw_backend.auth.csrf import UNSAFE_METHODS, OriginCheckMiddleware

from .support import FakeDatabase, make_client, make_settings

ORIGIN_ERROR = {"code": "forbidden_origin", "message": "Cross-origin request refused"}


def tiny_app(allowed_origins=()) -> FastAPI:
    app = FastAPI()
    calls: list[str] = []

    @app.api_route(
        "/x", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def x() -> dict:
        calls.append("x")
        return {"ok": True}

    app.add_middleware(OriginCheckMiddleware, allowed_origins=list(allowed_origins))
    app.state.calls = calls
    return app


def error_of(response) -> dict:
    body = dict(response.json()["error"])
    body.pop("request_id", None)
    return body


class MiddlewareTest(unittest.TestCase):
    def client(self, allowed=()):
        app = tiny_app(allowed)
        return app, TestClient(app, base_url="http://localhost")

    def test_the_unsafe_methods_are_exactly_the_four_that_change_things(self):
        self.assertEqual(UNSAFE_METHODS, {"POST", "PUT", "PATCH", "DELETE"})

    def test_a_foreign_origin_is_refused_for_every_unsafe_method(self):
        app, client = self.client()
        for method in sorted(UNSAFE_METHODS):
            with self.subTest(method=method):
                response = client.request(
                    method, "/x", headers={"Origin": "https://evil.example"}
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(error_of(response), ORIGIN_ERROR)
        self.assertEqual(app.state.calls, [])

    def test_a_safe_method_is_never_refused_by_origin(self):
        app, client = self.client()
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                response = client.request(
                    method, "/x", headers={"Origin": "https://evil.example"}
                )
                self.assertEqual(response.status_code, 200)

    def test_the_servers_own_origin_passes(self):
        app, client = self.client()
        for origin in ("http://localhost", "http://localhost:80", "http://LOCALHOST"):
            with self.subTest(origin=origin):
                response = client.post("/x", headers={"Origin": origin})
                self.assertEqual(response.status_code, 200)

    def test_an_origin_with_another_port_or_host_is_not_the_servers_own(self):
        app, client = self.client()
        for origin in (
            "http://localhost:8080",
            "http://127.0.0.1",
            "http://localhost.evil.example",
            "http://evil.example/localhost",
        ):
            with self.subTest(origin=origin):
                response = client.post("/x", headers={"Origin": origin})
                self.assertEqual(response.status_code, 403)

    def test_null_and_malformed_origins_are_refused(self):
        app, client = self.client()
        for origin in (
            "null",
            "",
            "not a url",
            "ftp://localhost",
            "//localhost",
            "http://",
        ):
            with self.subTest(origin=origin):
                response = client.post("/x", headers={"Origin": origin})
                self.assertEqual(response.status_code, 403)

    def test_a_listed_origin_passes(self):
        app, client = self.client(["https://paw.example.org"])
        self.assertEqual(
            client.post(
                "/x", headers={"Origin": "https://paw.example.org"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.post(
                "/x", headers={"Origin": "https://paw.example.org:443"}
            ).status_code,
            200,
        )
        self.assertEqual(
            client.post(
                "/x", headers={"Origin": "https://other.example.org"}
            ).status_code,
            403,
        )
        # A different scheme is a different origin.
        self.assertEqual(
            client.post("/x", headers={"Origin": "http://paw.example.org"}).status_code,
            403,
        )

    def test_without_origin_fetch_metadata_decides(self):
        app, client = self.client()
        for value, status in (
            ("same-origin", 200),
            ("none", 200),
            ("SAME-ORIGIN", 200),
            ("same-site", 403),
            ("cross-site", 403),
            ("", 403),
            ("anything", 403),
        ):
            with self.subTest(value=value):
                response = client.post("/x", headers={"Sec-Fetch-Site": value})
                self.assertEqual(response.status_code, status)

    def test_origin_wins_over_fetch_metadata(self):
        app, client = self.client()
        response = client.post(
            "/x",
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(response.status_code, 403)

    def test_a_client_that_sends_neither_is_not_a_browser_and_passes(self):
        app, client = self.client()
        self.assertEqual(client.post("/x").status_code, 200)

    def test_the_refusal_carries_the_request_id_when_there_is_one(self):
        app = create_app(make_settings(), database=FakeDatabase())
        client = make_client(app)
        response = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://evil.example", "X-Request-ID": "req-csrf-1"},
            json={"login_name": "alice", "password": "x"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["request_id"], "req-csrf-1")
        self.assertEqual(response.headers["X-Request-ID"], "req-csrf-1")
        # ... and the standard security headers of every response.
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")


class ApplicationTest(unittest.TestCase):
    """The middleware is part of the application, for every route under it."""

    def test_every_state_changing_route_of_the_application_is_covered(self):
        client = make_client(create_app(make_settings(), database=FakeDatabase()))
        for method, path in (
            ("POST", "/api/v1/auth/login"),
            ("POST", "/api/v1/auth/logout"),
            ("POST", "/api/v1/auth/password/change"),
            ("POST", "/api/v1/auth/step-up"),
            ("POST", "/api/v1/auth/token/redeem"),
            ("POST", "/api/v1/auth/sessions/revoke-others"),
            ("DELETE", "/api/v1/auth/sessions/00000000-0000-4000-8000-000000000000"),
            ("PUT", "/api/v1/auth/policy"),
            ("POST", "/api/v1/auth/users/00000000-0000-4000-8000-000000000000/unlock"),
            # A path that does not exist is refused on the Origin first, too.
            ("POST", "/api/v1/nothing-here"),
        ):
            with self.subTest(method=method, path=path):
                response = client.request(
                    method, path, headers={"Origin": "https://evil.example"}
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "forbidden_origin")

    def test_the_configured_allowed_origins_are_honoured(self):
        app = create_app(
            make_settings(allowed_origins="https://paw.example.org"),
            database=FakeDatabase(),
        )
        client = make_client(app)
        response = client.post(
            "/api/v1/auth/logout", headers={"Origin": "https://paw.example.org"}
        )
        # Past the Origin check: refused for the missing session instead.
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
