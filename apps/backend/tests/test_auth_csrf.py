"""The Origin check for state-changing requests (CSRF layer of PAW-022)."""

import asyncio
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from paw_backend.app import create_app
from paw_backend.auth.csrf import UNSAFE_METHODS, OriginCheckMiddleware
from paw_backend.security import origin_allowed

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


def csrf_status(
    scheme,
    host,
    origin=None,
    *,
    fetch_site=None,
    method="POST",
    allowed=(),
) -> int:
    """The status the Origin check gives a request the way an ASGI server hands it over.

    ``scheme`` is ``scope["scheme"]``: what Uvicorn made of the connection and, from a
    trusted proxy (``FORWARDED_ALLOW_IPS``), of ``X-Forwarded-Proto``. Nothing else
    tells the middleware the external scheme.
    """
    headers = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/v1/auth/login",
        "headers": headers,
        "state": {},
    }
    if scheme is not ABSENT:
        scope["scheme"] = scheme
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = OriginCheckMiddleware(inner, allowed_origins=list(allowed))
    asyncio.run(middleware(scope, receive, send))
    return sent[0]["status"]


ABSENT = object()
OK = 204
REFUSED = 403


class SchemeHostPortTest(unittest.TestCase):
    """The Origin must be the request's own origin: scheme, host AND port.

    The request's scheme is the trusted external one (``scope["scheme"]``); an
    Origin that only matches the authority (``http://host`` for a request to
    ``https://host``) is a different origin and is refused (login CSRF).
    """

    TABLE = (
        # (scheme, Host, Origin, expected)
        # -- the same origin, spelled in every way that is the same --------------
        ("https", "paw.example.org", "https://paw.example.org", OK),
        ("https", "paw.example.org", "https://paw.example.org:443", OK),
        ("https", "paw.example.org:443", "https://paw.example.org", OK),
        ("https", "PAW.Example.ORG", "https://paw.example.org", OK),
        ("http", "paw.example.org", "http://paw.example.org", OK),
        ("http", "paw.example.org:80", "http://paw.example.org", OK),
        ("http", "paw.example.org", "http://paw.example.org:80", OK),
        ("https", "paw.example.org:8443", "https://paw.example.org:8443", OK),
        ("http", "localhost:8000", "http://localhost:8000", OK),
        ("http", "[::1]:8000", "http://[::1]:8000", OK),
        ("https", "[::1]", "https://[::1]", OK),
        # -- the scheme differs (the authority alone is not enough) --------------
        ("https", "paw.example.org", "http://paw.example.org", REFUSED),
        ("http", "paw.example.org", "https://paw.example.org", REFUSED),
        ("https", "paw.example.org:8443", "http://paw.example.org:8443", REFUSED),
        ("http", "localhost:8000", "https://localhost:8000", REFUSED),
        ("https", "[::1]:8000", "http://[::1]:8000", REFUSED),
        ("https", "paw.example.org:443", "http://paw.example.org:443", REFUSED),
        # -- default ports belong to their own scheme ----------------------------
        ("https", "paw.example.org", "http://paw.example.org:443", REFUSED),
        ("https", "paw.example.org", "https://paw.example.org:80", REFUSED),
        ("http", "paw.example.org", "http://paw.example.org:443", REFUSED),
        ("https", "paw.example.org:80", "https://paw.example.org", REFUSED),
        ("https", "paw.example.org:80", "https://paw.example.org:80", OK),
        # -- the port differs -----------------------------------------------------
        ("https", "paw.example.org:8443", "https://paw.example.org", REFUSED),
        ("https", "paw.example.org:8443", "https://paw.example.org:443", REFUSED),
        ("https", "paw.example.org", "https://paw.example.org:8443", REFUSED),
        ("http", "localhost:8000", "http://localhost:9000", REFUSED),
        ("http", "localhost:8000", "http://localhost", REFUSED),
        # -- the host differs -----------------------------------------------------
        ("https", "paw.example.org", "https://evil.example", REFUSED),
        ("https", "paw.example.org", "https://sub.paw.example.org", REFUSED),
        ("https", "paw.example.org", "https://paw.example.org.evil.example", REFUSED),
        ("http", "localhost", "http://127.0.0.1", REFUSED),
        ("http", "[::1]:8000", "http://[::2]:8000", REFUSED),
        # -- an Origin that is not one --------------------------------------------
        ("https", "paw.example.org", "null", REFUSED),
        ("https", "paw.example.org", "", REFUSED),
        ("https", "paw.example.org", "paw.example.org", REFUSED),
        ("https", "paw.example.org", "https://paw.example.org/", OK),
        ("https", "paw.example.org", "https://paw.example.org/path", REFUSED),
        ("https", "paw.example.org", "https://user@paw.example.org", REFUSED),
        # -- a Host that is missing or cannot be an authority ---------------------
        ("https", None, "https://paw.example.org", REFUSED),
        ("https", "", "https://paw.example.org", REFUSED),
        ("https", "paw example.org", "https://paw.example.org", REFUSED),
    )

    def test_the_origin_must_have_the_scheme_host_and_port_of_the_request(self):
        for scheme, host, origin, expected in self.TABLE:
            with self.subTest(scheme=scheme, host=host, origin=origin):
                self.assertEqual(csrf_status(scheme, host, origin), expected)

    def test_a_scheme_that_is_unknown_is_never_the_same_origin(self):
        # Fail closed: without a trusted external scheme nothing can be compared.
        for scheme in (ABSENT, None, "", "ws", "wss", "ftp", "HTTPS", 5):
            for origin in ("https://paw.example.org", "http://paw.example.org"):
                with self.subTest(scheme=repr(scheme), origin=origin):
                    self.assertEqual(
                        csrf_status(scheme, "paw.example.org", origin), REFUSED
                    )

    def test_an_unknown_scheme_still_lets_a_listed_origin_through(self):
        # ... which is what ``PAW_ALLOWED_ORIGINS`` is for (an untrusted proxy).
        listed = ["https://paw.example.org"]
        for scheme in (ABSENT, None, "http", "https"):
            with self.subTest(scheme=repr(scheme)):
                self.assertEqual(
                    csrf_status(
                        scheme,
                        "backend:8000",
                        "https://paw.example.org",
                        allowed=listed,
                    ),
                    OK,
                )
        # The listed origin is one origin, scheme included.
        self.assertEqual(
            csrf_status(
                "https", "backend:8000", "http://paw.example.org", allowed=listed
            ),
            REFUSED,
        )
        self.assertEqual(
            csrf_status(
                "https", "backend:8000", "https://paw.example.org:8443", allowed=listed
            ),
            REFUSED,
        )

    def test_a_listed_origin_does_not_license_the_same_authority_on_another_scheme(
        self,
    ):
        listed = ["https://paw.example.org"]
        self.assertEqual(
            csrf_status(
                "https", "paw.example.org", "http://paw.example.org", allowed=listed
            ),
            REFUSED,
        )

    def test_the_origin_decides_before_fetch_metadata(self):
        for fetch_site in (None, "same-origin", "none", "same-site", "cross-site"):
            with self.subTest(fetch_site=fetch_site):
                self.assertEqual(
                    csrf_status(
                        "https",
                        "paw.example.org",
                        "http://paw.example.org",
                        fetch_site=fetch_site,
                    ),
                    REFUSED,
                )
                self.assertEqual(
                    csrf_status(
                        "https",
                        "paw.example.org",
                        "https://paw.example.org",
                        fetch_site="cross-site",
                    ),
                    OK,
                )

    def test_without_origin_fetch_metadata_decides_whatever_the_scheme(self):
        for scheme in ("http", "https", ABSENT):
            for fetch_site, expected in (
                ("same-origin", OK),
                ("none", OK),
                ("same-site", REFUSED),
                ("cross-site", REFUSED),
                ("", REFUSED),
            ):
                with self.subTest(scheme=repr(scheme), fetch_site=fetch_site):
                    self.assertEqual(
                        csrf_status(
                            scheme, "paw.example.org", None, fetch_site=fetch_site
                        ),
                        expected,
                    )
        # Neither header: not a browser that can be tricked.
        self.assertEqual(csrf_status("https", "paw.example.org", None), OK)

    def test_a_safe_method_is_not_judged_by_origin_at_all(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            self.assertEqual(
                csrf_status(
                    "https", "paw.example.org", "http://evil.example", method=method
                ),
                OK,
            )

    def test_the_default_port_rule_of_the_websocket_check_is_unchanged(self):
        # ``origin_allowed`` (the WebSocket handshake) is scheme-blind by design of
        # PAW-020; the request check above is a separate, stricter function.
        self.assertTrue(origin_allowed("http://paw.example.org", "paw.example.org", []))


class BehindAProxyTest(unittest.TestCase):
    """The external scheme is ``scope["scheme"]``: forwarded by a trusted proxy only."""

    def client(self, peer: str, trusted: str = "127.0.0.1") -> TestClient:
        app = ProxyHeadersMiddleware(tiny_app(), trusted_hosts=trusted)
        return TestClient(app, base_url="http://paw.example.org", client=(peer, 50000))

    def post(self, client, origin, forwarded_proto=None):
        headers = {"Origin": origin}
        if forwarded_proto:
            headers["X-Forwarded-Proto"] = forwarded_proto
        return client.post("/x", headers=headers)

    def test_a_trusted_proxy_that_terminates_tls_makes_the_scheme_https(self):
        client = self.client("127.0.0.1")
        self.assertEqual(
            self.post(client, "https://paw.example.org", "https").status_code, 200
        )
        # The page over plain HTTP is another origin, whatever the proxy says.
        self.assertEqual(
            self.post(client, "http://paw.example.org", "https").status_code, 403
        )

    def test_a_client_that_is_not_a_trusted_proxy_cannot_choose_the_scheme(self):
        # A direct client claiming X-Forwarded-Proto: https on a plain-HTTP
        # connection stays http, so an https Origin does not match.
        client = self.client("203.0.113.9")
        self.assertEqual(
            self.post(client, "https://paw.example.org", "https").status_code, 403
        )
        self.assertEqual(self.post(client, "http://paw.example.org").status_code, 200)

    def test_without_a_forwarded_scheme_the_connection_scheme_is_used(self):
        client = self.client("127.0.0.1")
        self.assertEqual(self.post(client, "http://paw.example.org").status_code, 200)
        self.assertEqual(self.post(client, "https://paw.example.org").status_code, 403)


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

    def test_an_origin_over_the_other_scheme_is_refused_end_to_end(self):
        # The default test client talks https://localhost; the page that posts is
        # http://localhost (a different origin, though the authority is the same).
        client = make_client(
            create_app(make_settings(), database=FakeDatabase()),
            base_url="https://localhost",
        )
        for origin, status in (("http://localhost", 403), ("https://localhost", 401)):
            with self.subTest(origin=origin):
                response = client.post(
                    "/api/v1/auth/logout", headers={"Origin": origin}
                )
                self.assertEqual(response.status_code, status)

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
