"""The session provider without a database: what it does when there is none."""

import unittest
import uuid
from typing import Annotated

from fastapi import Depends
from starlette.websockets import WebSocketDisconnect

from paw_backend.app import create_app
from paw_backend.auth.limits import SESSION_COOKIE_NAME
from paw_backend.auth.principals import (
    DatabasePrincipalDirectory,
    SessionPrincipalProvider,
    authenticated_context,
)
from paw_backend.auth.sessions import SessionLifetimes, SessionStore
from paw_backend.auth.wiring import build_auth
from paw_backend.authz import Capability, Principal, require_capability
from paw_backend.db import Database, DatabaseNotConfiguredError

from .support import make_client, make_settings

GOOD_COOKIE = "A" * 43


class CountingDatabase(Database):
    """A database whose every use is counted and fails as an unreachable one does."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(make_settings(database_url="postgresql://u:p@127.0.0.1:1/x"))
        self.calls = 0
        self.error = error or DatabaseNotConfiguredError("down")

    async def run_abortable(self, work):
        self.calls += 1
        raise self.error

    async def dispose(self) -> None:
        return None


def make_app(database):
    settings = make_settings(database_url="postgresql://u:p@127.0.0.1:1/x")
    auth = build_auth(settings, database)
    app = create_app(settings, database=database, auth=auth)

    @app.get("/test/guarded")
    async def guarded(
        principal: Annotated[
            Principal, Depends(require_capability(Capability.ACCOUNT_READ))
        ],
    ) -> dict:
        return {"user": str(principal.user_id)}

    @app.websocket("/test/ws")
    async def ws(
        websocket,
        principal: Annotated[
            Principal, Depends(require_capability(Capability.ACCOUNT_READ))
        ],
    ) -> None:
        await websocket.accept()
        await websocket.close()

    return app, auth


class AnonymousTest(unittest.TestCase):
    def test_a_request_without_a_cookie_is_anonymous_and_never_touches_the_database(
        self,
    ):
        database = CountingDatabase()
        app, _ = make_app(database)
        with make_client(app) as client:
            response = client.get("/test/guarded")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")
        self.assertEqual(database.calls, 0)

    def test_a_cookie_that_cannot_be_a_session_id_costs_no_query(self):
        database = CountingDatabase()
        app, _ = make_app(database)
        with make_client(app) as client:
            for value in ("x", "A" * 42, "A" * 44, "!" * 43, "a b"):
                with self.subTest(value=value):
                    client.cookies.clear()
                    response = client.get(
                        "/test/guarded",
                        headers={"Cookie": f"{SESSION_COOKIE_NAME}={value}"},
                    )
                    self.assertEqual(response.status_code, 401)
        self.assertEqual(database.calls, 0)

    def test_an_empty_cookie_is_anonymous_without_a_query(self):
        database = CountingDatabase()
        app, _ = make_app(database)
        with make_client(app) as client:
            response = client.get(
                "/test/guarded", headers={"Cookie": f"{SESSION_COOKIE_NAME}="}
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(database.calls, 0)

    def test_other_cookies_and_headers_do_not_authenticate(self):
        database = CountingDatabase()
        app, _ = make_app(database)
        with make_client(app) as client:
            response = client.get(
                "/test/guarded",
                headers={
                    "Cookie": f"session={GOOD_COOKIE}; paw_session={GOOD_COOKIE}",
                    "Authorization": f"Bearer {GOOD_COOKIE}",
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(database.calls, 0)


class UnavailableTest(unittest.TestCase):
    def cookie(self):
        return {"Cookie": f"{SESSION_COOKIE_NAME}={GOOD_COOKIE}"}

    def test_a_database_that_is_not_there_is_503_not_a_wrong_401(self):
        database = CountingDatabase()
        app, _ = make_app(database)
        # A database that is not configured is silent (nothing to log).
        with make_client(app) as client:
            response = client.get("/test/guarded", headers=self.cookie())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")
        self.assertEqual(database.calls, 1)

    def test_a_database_error_is_503_and_logs_only_the_type(self):
        from sqlalchemy.exc import OperationalError

        database = CountingDatabase(
            OperationalError(
                "SELECT", {}, Exception("password=hunter2 host=db.internal")
            )
        )
        app, _ = make_app(database)
        with self.assertLogs("paw_backend.auth.db", "ERROR") as logs:
            with make_client(app) as client:
                response = client.get("/test/guarded", headers=self.cookie())
        self.assertEqual(response.status_code, 503)
        text = "\n".join(logs.output)
        self.assertIn("OperationalError", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("db.internal", text)
        self.assertNotIn("hunter2", response.text)

    def test_a_websocket_is_refused_with_1013_when_the_database_is_down(self):
        database = CountingDatabase()
        app, _ = make_app(database)
        with make_client(app) as client:
            with self.assertRaises(WebSocketDisconnect) as caught:
                with client.websocket_connect(
                    "ws://localhost/test/ws", headers=self.cookie()
                ):
                    pass
        self.assertEqual(caught.exception.code, 1013)

    def test_a_websocket_without_a_session_is_refused_with_1008(self):
        database = CountingDatabase()
        app, _ = make_app(database)
        with make_client(app) as client:
            with self.assertRaises(WebSocketDisconnect) as caught:
                with client.websocket_connect("ws://localhost/test/ws"):
                    pass
        self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(database.calls, 0)


class DirectoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_unreachable_database_means_no_principal(self):
        from sqlalchemy.exc import OperationalError

        database = CountingDatabase(OperationalError("SELECT", {}, Exception("x")))
        directory = DatabasePrincipalDirectory(database, timeout_seconds=1)
        with self.assertLogs("paw_backend.auth.db", "ERROR"):
            self.assertIsNone(await directory.get_principal_by_id(uuid.uuid4()))

    def test_the_directory_is_typed(self):
        with self.assertRaises(TypeError):
            DatabasePrincipalDirectory("database", timeout_seconds=1)
        with self.assertRaises(TypeError):
            SessionPrincipalProvider(
                "database",
                SessionStore(SessionLifetimes(1, 1, 1, 1), clock=lambda: None),
                timeout_seconds=1,
            )
        with self.assertRaises(TypeError):
            SessionPrincipalProvider(CountingDatabase(), "sessions", timeout_seconds=1)


class ContextTest(unittest.TestCase):
    def test_a_request_that_was_not_resolved_has_no_context(self):
        class Connection:
            state = type("State", (), {})()

        self.assertIsNone(authenticated_context(Connection()))


if __name__ == "__main__":
    unittest.main()
