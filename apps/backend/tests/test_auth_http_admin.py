"""The token, unlock and policy routes over HTTP; the role matrix; CSRF end to end."""

import unittest
import uuid

from sqlalchemy import text

from paw_backend.authz.audit import PostgresAuditSink
from paw_backend.identity.operator import ROOT_UID, OwnerOperator

from .auth_http_support import (
    PASSWORD,
    T0,
    HttpTestCase,
    cookie_of,
    error_code,
    requires_postgres,
)
from .auth_support import fast_settings
from .identity_support import running_as, wrong_secret_for

OWNER_PASSWORD = "the owner passphrase, set at setup"
RECOVERED = "a passphrase chosen after recovery"
ADMIN_PASSWORD = "the admin passphrase"

ACTIVE_PROJECT = """
INSERT INTO projects (id, name, status, created_at, updated_at)
VALUES (:id, :name, 'active', :now, :now)
"""
DELETED_PROJECT = """
INSERT INTO projects (id, name, status, created_at, updated_at,
    deletion_started_at, deletion_scheduled_at, deleted_at)
VALUES (:id, 'Deleted Project', 'deleted', :now, :now, :now,
    CAST(:now AS timestamptz) + interval '720 hours', :now)
"""
MEMBER_INSERT = """
INSERT INTO project_members (project_id, user_id, role, status, invited_at,
    invite_expires_at, joined_at)
VALUES (:p, :u, :r, :s, :now,
    CASE WHEN :s = 'invited' THEN CAST(:now AS timestamptz) + interval '1 day' END,
    CASE WHEN :s = 'active' THEN CAST(:now AS timestamptz) END)
"""


class OwnerTokenTestCase(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(running_as(ROOT_UID))
        # The operator side runs on its own engine, in the test thread's loop.
        import asyncio

        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def issue(self, kind="setup"):
        from paw_backend.db import Database

        async def go():
            # The operator side (creates the Owner, issues tokens) has its own
            # database user, never the web application's.
            database = Database(fast_settings())
            try:
                operator = OwnerOperator(
                    database, PostgresAuditSink(database), clock=self.clock
                )
                if kind == "setup":
                    return await operator.setup_owner("boss")
                return await operator.recover_owner()
            finally:
                await database.dispose()

        return self.loop.run_until_complete(go())

    def redeem(self, token, password=OWNER_PASSWORD, source="203.0.113.7", **extra):
        return self.call(
            "POST",
            "/api/v1/auth/token/redeem",
            source=source,
            json={"token": token, "new_password": password, **extra},
        )


@requires_postgres
class RedeemRouteTest(OwnerTokenTestCase):
    def test_the_setup_token_sets_the_owners_password_and_nobody_is_signed_in(self):
        issued = self.issue()
        response = self.redeem(issued.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"purpose": "setup", "passkey_required": True, "next": "login"},
        )
        self.assertEqual(list(cookie_of(response)), [])  # no session is created
        self.assertEqual(
            self.scalar("SELECT status FROM users WHERE login_name = 'boss'"), "active"
        )
        login = self.login("boss", OWNER_PASSWORD)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["system_role"], "owner")
        self.assertEqual(
            login.json()["auth"]["passkey"],
            {
                "requirement": "required",
                "enrolled": False,
                "enrollment_required": True,
                "recommended": False,
            },
        )

    def test_a_recovery_signs_every_device_out_and_replaces_the_password(self):
        self.redeem(self.issue().token)
        devices = [self.login_token("boss", OWNER_PASSWORD) for _ in range(2)]
        response = self.redeem(self.issue("recovery").token, RECOVERED)
        self.assertEqual(response.json()["purpose"], "recovery")
        for token in devices:
            self.assertEqual(
                self.call("GET", "/api/v1/auth/session", token=token).status_code, 401
            )
        self.assertEqual(self.login("boss", OWNER_PASSWORD).status_code, 401)
        self.assertEqual(self.login("boss", RECOVERED).status_code, 200)

    def test_a_bad_token_is_400_with_one_answer_and_changes_nothing(self):
        issued = self.issue()
        answers = set()
        for token in (
            "not-a-token",
            "pawst1.a.b",
            wrong_secret_for(issued.token),
            "x" * 100,
        ):
            response = self.redeem(token, source=f"192.0.2.{len(answers) + 1}")
            self.assertEqual(response.status_code, 400)
            answers.add((error_code(response), response.json()["error"]["message"]))
        self.assertEqual(answers, {("invalid_token", "The token was not accepted")})
        self.assertEqual(self.scalar("SELECT status FROM users"), "invited")

    def test_a_password_that_breaks_the_policy_is_422_and_the_token_survives(self):
        issued = self.issue()
        response = self.redeem(issued.token, "short")
        self.assertEqual(
            (response.status_code, error_code(response)), (422, "password_policy")
        )
        self.assertIn("too_short", response.json()["error"]["message"])
        self.assertEqual(self.redeem(issued.token).status_code, 200)

    def test_the_body_is_checked(self):
        issued = self.issue()
        for body in (
            {},
            {"token": issued.token},
            {"new_password": OWNER_PASSWORD},
            {
                "token": issued.token,
                "new_password": OWNER_PASSWORD,
                "purpose": "recovery",
            },
            {"token": 5, "new_password": OWNER_PASSWORD},
            {"token": issued.token, "new_password": 5},
            {"token": "t" * 513, "new_password": OWNER_PASSWORD},
            {"token": "", "new_password": OWNER_PASSWORD},
        ):
            with self.subTest(body=repr(body)[:50]):
                response = self.call("POST", "/api/v1/auth/token/redeem", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(issued.token, response.text)
        self.assertEqual(self.scalar("SELECT count(*) FROM auth_throttles"), 0)

    def test_a_source_that_keeps_guessing_gets_429_before_the_token_is_looked_at(self):
        issued = self.issue()
        for _ in range(5):
            self.assertEqual(
                self.redeem("pawst1." + uuid.uuid4().hex + "." + "A" * 43).status_code,
                400,
            )
        response = self.redeem(issued.token)
        self.assertEqual(
            (response.status_code, response.headers["Retry-After"]), (429, "60")
        )
        self.assertEqual(error_code(response), "rate_limited")
        self.assertEqual(self.scalar("SELECT attempts FROM setup_tokens"), 0)
        # Another source is not affected.
        self.assertEqual(
            self.redeem(issued.token, source="198.51.100.9").status_code, 200
        )

    def test_the_global_limit_stops_many_sources(self):
        issued = self.issue()
        for index in range(30):
            self.assertEqual(
                self.redeem(
                    "pawst1." + uuid.uuid4().hex + "." + "A" * 43,
                    source=f"192.0.2.{index + 1}",
                ).status_code,
                400,
            )
        response = self.redeem(issued.token, source="198.51.100.77")
        self.assertEqual(
            (response.status_code, response.headers["Retry-After"]), (429, "60")
        )

    def test_the_token_and_the_password_reach_neither_the_log_nor_the_database(self):
        issued = self.issue()
        with self.assertLogs(level="DEBUG") as logs:
            self.redeem(wrong_secret_for(issued.token), "a wrong-attempt passphrase")
            self.redeem(issued.token)
        output = "\n".join(logs.output)
        for secret in (issued.token, OWNER_PASSWORD, "a wrong-attempt passphrase"):
            self.assertNotIn(secret, output)
        self.assertNotIn(issued.token, self.everything_stored())
        self.assertNotIn(OWNER_PASSWORD, self.everything_stored())


@requires_postgres
class RoleMatrixTest(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.make_user("boss", role="owner", password=OWNER_PASSWORD)
        self.admin_id = self.make_user(
            "admin-one", role="admin", password=ADMIN_PASSWORD
        )
        self.user_id = self.make_user("alice")
        self.owner = self.login_token("boss", OWNER_PASSWORD)
        self.admin = self.login_token("admin-one", ADMIN_PASSWORD)
        self.user = self.login_token("alice")

    def status(self, method, path, token, **options):
        return self.call(method, path, token=token, **options).status_code

    def test_the_account_routes_are_open_to_every_role_and_only_to_signed_in_people(
        self,
    ):
        for token in (self.owner, self.admin, self.user):
            self.assertEqual(self.status("GET", "/api/v1/auth/session", token), 200)
            self.assertEqual(self.status("GET", "/api/v1/auth/sessions", token), 200)
        self.assertEqual(self.status("GET", "/api/v1/auth/session", None), 401)

    def test_the_policy_can_be_read_by_an_admin_and_the_owner_only(self):
        self.assertEqual(self.status("GET", "/api/v1/auth/policy", self.owner), 200)
        self.assertEqual(self.status("GET", "/api/v1/auth/policy", self.admin), 200)
        self.assertEqual(self.status("GET", "/api/v1/auth/policy", self.user), 403)
        self.assertEqual(self.status("GET", "/api/v1/auth/policy", None), 401)

    def test_the_policy_body_read_by_an_admin(self):
        body = self.call("GET", "/api/v1/auth/policy", token=self.admin).json()
        self.assertEqual(
            {k: v for k, v in body.items() if k != "updated_at"},
            {
                "version": 1,
                "passkey_owner": "required",
                "passkey_admin": "required",
                "passkey_user": "optional",
                "recommend_passkey_to_users": True,
                "stepup_window_minutes": 30,
                "updated_by": None,
                "applies_to": "new_sign_ins",
            },
        )

    def put_policy(self, token, version=1, **changes):
        body = {
            "expected_version": version,
            "passkey_owner": "required",
            "passkey_admin": "required",
            "passkey_user": "optional",
            "recommend_passkey_to_users": True,
            "stepup_window_minutes": 30,
            **changes,
        }
        return self.call("PUT", "/api/v1/auth/policy", token=token, json=body)

    def step_up(self, token, password):
        response = self.call(
            "POST", "/api/v1/auth/step-up", token=token, json={"password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return self.token_of(response)

    def test_only_the_owner_changes_the_policy_and_only_after_a_step_up(self):
        for token in (self.admin, self.user):
            response = self.put_policy(token, passkey_user="required")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(error_code(response), "forbidden")
        self.assertEqual(
            self.put_policy(None, passkey_user="required").status_code, 401
        )
        # The Owner without a step-up:
        response = self.put_policy(self.owner, passkey_user="required")
        self.assertEqual(
            (response.status_code, error_code(response)), (403, "step_up_required")
        )
        stepped = self.step_up(self.owner, OWNER_PASSWORD)
        response = self.put_policy(stepped, passkey_user="required")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (
                response.json()["version"],
                response.json()["passkey_user"],
                response.json()["updated_by"],
            ),
            (2, "required", str(self.owner_id)),
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM auth_policy_changes"), 1)

    def test_a_stale_version_is_409_and_the_body_is_checked_strictly(self):
        stepped = self.step_up(self.owner, OWNER_PASSWORD)
        self.assertEqual(
            self.put_policy(stepped, passkey_user="required").status_code, 200
        )
        stale = self.put_policy(stepped, passkey_admin="optional")
        self.assertEqual(
            (stale.status_code, error_code(stale)), (409, "version_conflict")
        )
        for changes in (
            {"expected_version": 0},
            {"expected_version": "1"},
            {"expected_version": True},
            {"passkey_owner": "sometimes"},
            {"passkey_user": None},
            {"recommend_passkey_to_users": "yes"},
            {"stepup_window_minutes": 4},
            {"stepup_window_minutes": 241},
            {"stepup_window_minutes": 30.5},
            {"unknown": 1},
        ):
            with self.subTest(changes=changes):
                response = self.put_policy(stepped, 2, **changes)
                self.assertIn(response.status_code, (422,), response.text)
        self.assertEqual(self.scalar("SELECT version FROM auth_policy"), 2)

    def test_tightening_does_not_sign_anybody_out_and_the_owner_can_still_sign_in(self):
        stepped = self.step_up(self.owner, OWNER_PASSWORD)
        response = self.put_policy(
            stepped, passkey_user="required", passkey_admin="required"
        )
        self.assertEqual(response.status_code, 200)
        for token in (self.admin, self.user):
            self.assertEqual(self.status("GET", "/api/v1/auth/session", token), 200)
        report = self.call("GET", "/api/v1/auth/session", token=self.user).json()[
            "auth"
        ]["passkey"]
        self.assertEqual(
            (report["requirement"], report["enrollment_required"]), ("required", True)
        )
        self.assertEqual(self.login("boss", OWNER_PASSWORD).status_code, 200)

    def test_relaxing_is_the_owners_too_and_recorded_with_who_and_what(self):
        stepped = self.step_up(self.owner, OWNER_PASSWORD)
        self.assertEqual(
            self.put_policy(
                stepped, passkey_owner="optional", passkey_admin="optional"
            ).status_code,
            200,
        )
        row = self.rows("SELECT * FROM auth_policy_changes")[0]
        self.assertEqual(
            (
                row.changed_by,
                row.old_passkey_owner,
                row.new_passkey_owner,
                row.old_passkey_admin,
                row.new_passkey_admin,
            ),
            (self.owner_id, "required", "optional", "required", "optional"),
        )
        audit = [r for r in self.audit_rows() if r.action == "auth.policy.update"]
        self.assertEqual(
            [(r.decision, r.reason, r.actor_id) for r in audit],
            [("allow", "updated", self.owner_id)],
        )

    def test_an_admin_unlocks_a_user_and_not_an_admin_or_the_owner(self):
        for name in ("alice", "boss", "admin-one"):
            for _ in range(5):
                self.login(name, "wrong wrong wrong", source="198.51.100.4")
        # (the source above is locked after 20 attempts; the account locks come first)
        for name, target, expected in (
            ("alice", self.user_id, 204),
            ("boss", self.owner_id, 403),
            ("admin-one", self.admin_id, 403),
        ):
            with self.subTest(target=name):
                response = self.call(
                    "POST", f"/api/v1/auth/users/{target}/unlock", token=self.admin
                )
                self.assertEqual(response.status_code, expected)
        self.assertEqual(self.login("alice", source="203.0.113.99").status_code, 200)
        self.assertEqual(
            self.login("boss", OWNER_PASSWORD, source="203.0.113.98").status_code, 429
        )

    def test_the_owner_unlocks_the_owners_own_locked_account(self):
        for _ in range(5):
            self.login("boss", "wrong wrong wrong", source="198.51.100.4")
        self.assertEqual(
            self.login("boss", OWNER_PASSWORD, source="203.0.113.98").status_code, 429
        )
        response = self.call(
            "POST", f"/api/v1/auth/users/{self.owner_id}/unlock", token=self.owner
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            self.login("boss", OWNER_PASSWORD, source="203.0.113.98").status_code, 200
        )

    def test_a_user_cannot_unlock_and_an_unknown_account_is_404(self):
        response = self.call(
            "POST", f"/api/v1/auth/users/{self.user_id}/unlock", token=self.user
        )
        self.assertEqual(response.status_code, 403)
        response = self.call(
            "POST", f"/api/v1/auth/users/{uuid.uuid4()}/unlock", token=self.admin
        )
        self.assertEqual(
            (response.status_code, error_code(response)), (404, "not_found")
        )
        self.assertEqual(
            self.call(
                "POST", "/api/v1/auth/users/not-a-uuid/unlock", token=self.admin
            ).status_code,
            422,
        )

    def test_every_guarded_route_answers_401_without_a_session(self):
        some = str(uuid.uuid4())
        for method, path in (
            ("GET", "/api/v1/auth/session"),
            ("GET", "/api/v1/auth/sessions"),
            ("POST", "/api/v1/auth/logout"),
            ("POST", "/api/v1/auth/sessions/revoke-others"),
            ("DELETE", f"/api/v1/auth/sessions/{some}"),
            ("POST", "/api/v1/auth/password/change"),
            ("POST", "/api/v1/auth/step-up"),
            ("POST", f"/api/v1/auth/users/{some}/unlock"),
            ("GET", "/api/v1/auth/policy"),
            ("PUT", "/api/v1/auth/policy"),
        ):
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(
                    self.call(
                        method, path, json={} if method != "GET" else None
                    ).status_code,
                    401,
                )

    def test_the_denials_of_signed_in_people_are_audited_the_anonymous_ones_are_not(
        self,
    ):
        self.call("GET", "/api/v1/auth/policy", token=self.user)
        self.call("GET", "/api/v1/auth/policy")
        summary = self.audit_summary()
        self.assertEqual(
            summary[("admin.auth_policy.view", "deny", "capability_not_granted")], 1
        )
        self.assertEqual(
            sum(v for (a, d, r), v in summary.items() if a == "admin.auth_policy.view"),
            1,
        )


@requires_postgres
class CsrfEndToEndTest(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.make_user("alice")
        self.token = self.login_token()

    def test_a_cross_origin_request_with_a_valid_cookie_does_nothing(self):
        for method, path, body in (
            ("POST", "/api/v1/auth/logout", None),
            ("POST", "/api/v1/auth/sessions/revoke-others", None),
            (
                "POST",
                "/api/v1/auth/password/change",
                {
                    "current_password": PASSWORD,
                    "new_password": "a brand new passphrase",
                },
            ),
            ("POST", "/api/v1/auth/step-up", {"password": PASSWORD}),
        ):
            with self.subTest(route=path):
                response = self.call(
                    method,
                    path,
                    token=self.token,
                    origin="https://evil.example",
                    json=body,
                )
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (403, "forbidden_origin"),
                )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 200
        )
        self.assertEqual(self.login(password=PASSWORD).status_code, 200)
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NOT NULL"
            ),
            0,
        )

    def test_a_cross_origin_login_is_refused_too(self):
        response = self.call(
            "POST",
            "/api/v1/auth/login",
            origin="https://evil.example",
            json={"login_name": "alice", "password": PASSWORD},
        )
        self.assertEqual(
            (response.status_code, error_code(response)), (403, "forbidden_origin")
        )
        self.assertEqual(list(cookie_of(response)), [])

    def test_the_applications_own_origin_is_accepted(self):
        response = self.call(
            "POST", "/api/v1/auth/logout", token=self.token, origin="https://localhost"
        )
        self.assertEqual(response.status_code, 204)

    def test_a_refused_request_does_not_count_against_the_login_backoff(self):
        for _ in range(10):
            self.call(
                "POST",
                "/api/v1/auth/login",
                origin="https://evil.example",
                json={"login_name": "alice", "password": "wrong wrong wrong"},
            )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM auth_throttles"), 1
        )  # only the setUp login's source row
        self.assertEqual(self.login().status_code, 200)


@requires_postgres
class ProviderTest(HttpTestCase):
    """The principal is built from stored data only (users and project members)."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("alice")
        self.token = self.login_token()

    def test_the_principal_has_the_role_from_the_users_table_and_the_active_memberships(
        self,
    ):
        from typing import Annotated

        from fastapi import Depends

        from paw_backend.authz import Capability, Principal, require_capability

        @self.app.get("/test/whoami")
        async def whoami(
            principal: Annotated[
                Principal, Depends(require_capability(Capability.ACCOUNT_READ))
            ],
        ) -> dict:
            return {
                "user": str(principal.user_id),
                "role": principal.system_role.value,
                "projects": {
                    str(k): v.value for k, v in principal.project_roles.items()
                },
            }

        project = uuid.uuid4()
        gone = uuid.uuid4()
        invited = uuid.uuid4()
        with self.engine.begin() as connection:
            for pid, deleted in ((project, False), (gone, True), (invited, False)):
                connection.execute(
                    text(DELETED_PROJECT if deleted else ACTIVE_PROJECT),
                    {"id": pid, "name": f"P-{str(pid)[:6]}", "now": T0},
                )
            for pid, role, status in (
                (project, "contributor", "active"),
                (gone, "manager", "active"),
                (invited, "manager", "invited"),
            ):
                connection.execute(
                    text(MEMBER_INSERT),
                    {"p": pid, "u": self.user_id, "r": role, "s": status, "now": T0},
                )
        response = self.call("GET", "/test/whoami", token=self.token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "user": str(self.user_id),
                "role": "user",
                "projects": {str(project): "contributor"},
            },
        )

    def test_a_role_change_in_the_database_takes_effect_on_the_next_request(self):
        from typing import Annotated

        from fastapi import Depends

        from paw_backend.authz import Capability, Principal, require_capability

        @self.app.get("/test/admin-only")
        async def admin_only(
            principal: Annotated[
                Principal, Depends(require_capability(Capability.ADMIN_USERS_MANAGE))
            ],
        ) -> dict:
            return {"role": principal.system_role.value}

        self.assertEqual(
            self.call("GET", "/test/admin-only", token=self.token).status_code, 403
        )
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET system_role = 'admin', passkey_required = true")
            )
        response = self.call("GET", "/test/admin-only", token=self.token)
        self.assertEqual(
            (response.status_code, response.json()), (200, {"role": "admin"})
        )

    def test_the_session_is_looked_up_once_per_request_even_with_several_guards(self):
        from typing import Annotated

        from fastapi import Depends

        from paw_backend.authz import Capability, Principal, require_capability

        @self.app.get(
            "/test/two-guards",
            dependencies=[Depends(require_capability(Capability.ACCOUNT_READ))],
        )
        async def two_guards(
            principal: Annotated[
                Principal, Depends(require_capability(Capability.ACCOUNT_MANAGE))
            ],
        ) -> dict:
            return {"ok": True}

        calls = []
        provider = self.services.provider
        real = provider._resolve

        async def counting(connection, token):
            calls.append(1)
            return await real(connection, token)

        provider._resolve = counting
        try:
            self.assertEqual(
                self.call("GET", "/test/two-guards", token=self.token).status_code, 200
            )
        finally:
            del provider._resolve
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
