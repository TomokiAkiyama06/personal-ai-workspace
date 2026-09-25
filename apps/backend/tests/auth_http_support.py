"""HTTP-level fixtures for the authentication routes (real PostgreSQL)."""

import unittest
import uuid
from http.cookies import SimpleCookie

from argon2 import PasswordHasher as Argon2
from argon2 import Type
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from paw_backend.app import create_app
from paw_backend.auth.limits import SESSION_COOKIE_NAME
from paw_backend.auth.wiring import build_auth
from paw_backend.db import Database

from .auth_support import (
    PASSWORD,
    T0,
    FakeClock,
    fast_settings,
    migrate,
    requires_postgres,
    sync_database_url,
    url_for_role,
)

__all__ = [
    "PASSWORD",
    "T0",
    "HttpTestCase",
    "cookie_of",
    "requires_postgres",
]

SOURCE_HEADER = "X-Test-Source"
DEFAULT_SOURCE = "203.0.113.7"
ARGON2 = Argon2(time_cost=1, memory_cost=19_456, parallelism=1, type=Type.ID)


class SourceOverride:
    """ASGI wrapper: the client address of a request comes from ``X-Test-Source``.

    ``TestClient`` has one fixed client address; the throttles are per source, so
    a test that needs several sources sends this header. Lifespan and everything
    else pass through untouched.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == SOURCE_HEADER.lower().encode():
                    scope = {**scope, "client": (value.decode(), 12345)}
        await self.app(scope, receive, send)


def cookie_of(response) -> SimpleCookie:
    """The session cookie a response sets, parsed; an empty jar if it sets none."""
    jar = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    return jar


def set_cookie_header(response) -> str:
    headers = response.headers.get_list("set-cookie")
    assert len(headers) == 1, headers
    return headers[0]


class HttpTestCase(unittest.TestCase):
    """The application on a migrated database, driven through ``TestClient``.

    One ``TestClient`` (one event loop) serves the whole test, so the pooled
    engine the token flow uses stays on one loop. The clock is injected, the
    source of a request is the ``X-Test-Source`` header (default
    ``203.0.113.7``) and a cookie is sent explicitly (``call(..., token=...)``),
    so a test can hold several browsers at once.
    """

    settings_overrides: dict = {}
    base_url = "https://localhost"
    # The role the application connects as (``None``: the test user's own).
    migration_environment: dict = {}
    service_role: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        migrate("downgrade", "base")
        migrate("upgrade", "head", **cls.migration_environment)

    @classmethod
    def tearDownClass(cls) -> None:
        migrate("downgrade", "base")

    def setUp(self) -> None:
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE users CASCADE"))
            connection.execute(text("TRUNCATE auth_throttles"))
            connection.execute(text("TRUNCATE auth_policy_changes"))
            connection.execute(text("TRUNCATE auth_policy"))
            connection.execute(
                text(
                    "INSERT INTO auth_policy (id, version, passkey_owner, "
                    "passkey_admin, passkey_user, recommend_passkey_to_users, "
                    "stepup_window_minutes, updated_at) VALUES (1, 1, 'required', "
                    "'required', 'optional', true, 30, now())"
                )
            )
            self.started_at = connection.execute(
                text("SELECT clock_timestamp()")
            ).scalar()
        self.clock = FakeClock(T0)
        overrides = dict(self.settings_overrides)
        if self.service_role is not None:
            overrides["database_url"] = url_for_role(self.service_role)
        self.settings = fast_settings(**overrides)
        self.database = Database(self.settings)
        self.services = build_auth(self.settings, self.database, clock=self.clock)
        self.app = create_app(self.settings, database=self.database, auth=self.services)
        self.client = TestClient(SourceOverride(self.app), base_url=self.base_url)
        self.enterContext(self.client)  # runs the lifespan; one loop for the test

    # -- requests ---------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        source: str = DEFAULT_SOURCE,
        origin: str | None = None,
        headers: dict | None = None,
        **options,
    ):
        """One request with an explicit cookie (or none) and source."""
        request_headers = {SOURCE_HEADER: source, **(headers or {})}
        if token is not None:
            request_headers["Cookie"] = f"{SESSION_COOKIE_NAME}={token}"
        if origin is not None:
            request_headers["Origin"] = origin
        self.client.cookies.clear()
        return self.client.request(method, path, headers=request_headers, **options)

    def login(self, name="alice", password=PASSWORD, *, source=DEFAULT_SOURCE, **body):
        return self.call(
            "POST",
            "/api/v1/auth/login",
            source=source,
            json={"login_name": name, "password": password, **body},
        )

    def token_of(self, response) -> str:
        jar = cookie_of(response)
        self.assertIn(SESSION_COOKIE_NAME, jar, response.text)
        return jar[SESSION_COOKIE_NAME].value

    def login_token(self, name="alice", password=PASSWORD, **options) -> str:
        response = self.login(name, password, **options)
        self.assertEqual(response.status_code, 200, response.text)
        return self.token_of(response)

    # -- data ------------------------------------------------------------------------

    def make_user(
        self,
        login_name: str = "alice",
        *,
        role: str = "user",
        status: str = "active",
        password: str | None = PASSWORD,
    ) -> uuid.UUID:
        user_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status, "
                    "passkey_required, created_at, updated_at) VALUES (:id, :name, "
                    ":role, :status, :required, :now, :now)"
                ),
                {
                    "id": user_id,
                    "name": login_name,
                    "role": role,
                    "status": status,
                    "required": role in ("owner", "admin"),
                    "now": T0,
                },
            )
            if password is not None:
                connection.execute(
                    text(
                        "INSERT INTO password_credentials (user_id, hash, "
                        "created_at, changed_at) VALUES (:id, :hash, :now, :now)"
                    ),
                    {"id": user_id, "hash": ARGON2.hash(password), "now": T0},
                )
        return user_id

    def rows(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params).all())

    def scalar(self, sql: str, **params):
        return self.rows(sql, **params)[0][0]

    def audit_rows(self) -> list:
        return self.rows(
            "SELECT * FROM audit_events WHERE recorded_at >= :since "
            "ORDER BY recorded_at, occurred_at",
            since=self.started_at,
        )

    def audit_summary(self):
        from collections import Counter

        return Counter((r.action, r.decision, r.reason) for r in self.audit_rows())

    def everything_stored(self) -> str:
        parts = []
        for table in (
            "users",
            "password_credentials",
            "auth_sessions",
            "auth_throttles",
            "auth_policy_changes",
        ):
            parts += [str(r[0]) for r in self.rows(f"SELECT t::text FROM {table} t")]
        parts += [
            str(r[0])
            for r in self.rows(
                "SELECT t::text FROM audit_events t WHERE recorded_at >= :since",
                since=self.started_at,
            )
        ]
        return "\n".join(parts)


def error_code(response) -> str:
    return response.json()["error"]["code"]
