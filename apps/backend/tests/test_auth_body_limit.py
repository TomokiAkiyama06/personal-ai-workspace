"""The body cap of the authentication routes (no database needed)."""

import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from paw_backend.app import create_app
from paw_backend.auth.body_limit import AuthBodyLimitMiddleware
from paw_backend.auth.limits import AUTH_BODY_MAX_BYTES

from .support import FakeDatabase, make_client, make_settings

LIMIT = 1_000


def tiny_app() -> FastAPI:
    app = FastAPI()
    seen: list[int] = []

    @app.post("/api/v1/auth/echo")
    async def echo(request: Request) -> dict:
        body = await request.body()
        seen.append(len(body))
        return {"length": len(body), "first": body[:1].decode() if body else ""}

    @app.post("/api/v1/other/echo")
    async def other(request: Request) -> dict:
        return {"length": len(await request.body())}

    app.add_middleware(AuthBodyLimitMiddleware, max_bytes=LIMIT)
    app.state.seen = seen
    return app


class MiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.app = tiny_app()
        self.client = TestClient(self.app, base_url="http://localhost")

    def test_a_body_up_to_the_limit_reaches_the_application_intact(self):
        for size in (0, 1, LIMIT - 1, LIMIT):
            with self.subTest(size=size):
                response = self.client.post("/api/v1/auth/echo", content=b"x" * size)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["length"], size)

    def test_a_body_over_the_limit_is_413_and_never_reaches_the_application(self):
        for size in (LIMIT + 1, LIMIT * 10):
            with self.subTest(size=size):
                response = self.client.post("/api/v1/auth/echo", content=b"x" * size)
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json()["error"]["code"], "payload_too_large")
        self.assertEqual(self.app.state.seen, [])

    def test_a_body_without_a_length_is_counted_as_it_arrives(self):
        def chunks(count):
            for _ in range(count):
                yield b"y" * 300

        ok = self.client.post("/api/v1/auth/echo", content=chunks(3))
        self.assertEqual((ok.status_code, ok.json()["length"]), (200, 900))
        refused = self.client.post("/api/v1/auth/echo", content=chunks(5))
        self.assertEqual(refused.status_code, 413)
        self.assertEqual(self.app.state.seen, [900])

    def test_a_declared_length_over_the_limit_is_refused_without_reading(self):
        response = self.client.post(
            "/api/v1/auth/echo",
            content=b"x",
            headers={"Content-Length": str(LIMIT + 1)},
        )
        self.assertEqual(response.status_code, 413)

    def test_other_paths_and_methods_are_not_limited(self):
        big = b"z" * (LIMIT * 5)
        self.assertEqual(
            self.client.post("/api/v1/other/echo", content=big).json()["length"],
            len(big),
        )
        self.assertEqual(self.client.get("/api/v1/auth/echo").status_code, 405)

    def test_the_settings_of_the_middleware_are_checked(self):
        for bad, error in (
            (True, TypeError),
            ("16", TypeError),
            (0, ValueError),
            (-1, ValueError),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(error):
                    AuthBodyLimitMiddleware(FastAPI(), max_bytes=bad)


class ApplicationTest(unittest.TestCase):
    def client(self):
        return make_client(create_app(make_settings(), database=FakeDatabase()))

    def test_the_limit_is_16_kib(self):
        self.assertEqual(AUTH_BODY_MAX_BYTES, 16 * 1024)

    def test_the_public_routes_refuse_a_huge_body(self):
        client = self.client()
        big = (
            b'{"login_name": "alice", "password": "'
            + b"p" * AUTH_BODY_MAX_BYTES
            + b'"}'
        )
        for path in ("/api/v1/auth/login", "/api/v1/auth/token/redeem"):
            with self.subTest(path=path):
                response = client.post(
                    path,
                    content=big,
                    headers={
                        "Content-Type": "application/json",
                        "X-Request-ID": "req-big-1",
                    },
                )
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json()["error"]["request_id"], "req-big-1")

    def test_a_login_body_with_every_field_at_its_limit_is_far_below_the_cap(self):
        body = (
            b'{"login_name": "'
            + b"a" * 128
            + b'", "password": "'
            + b"p" * 1024
            + b'", "remember_me": true, "device_name": "'
            + b"d" * 64
            + b'"}'
        )
        self.assertLess(len(body), AUTH_BODY_MAX_BYTES // 4)
        response = self.client().post(
            "/api/v1/auth/login",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        # Past the cap and the schema; a password of 1,024 characters cannot be
        # anybody's (the limit is 256), so it is simply the wrong password.
        self.assertEqual(response.status_code, 401)

    def test_a_normal_login_reaches_the_database_layer(self):
        response = self.client().post(
            "/api/v1/auth/login",
            json={"login_name": "alice", "password": "a normal passphrase"},
        )
        # The database is not configured in this test: 503, not a wrong 401.
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
