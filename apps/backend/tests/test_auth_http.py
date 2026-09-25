"""The authentication routes over HTTP: login, session, devices, password, Step-up.

Real PostgreSQL, the real application, ``TestClient``; time is the injected
clock, so an expiry is a moment on it and not a wait.
"""

import contextlib
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import text

from paw_backend.auth.errors import AuthUnavailableError
from paw_backend.auth.limits import SESSION_COOKIE_NAME

from .auth_http_support import (
    PASSWORD,
    T0,
    HttpTestCase,
    cookie_of,
    error_code,
    requires_postgres,
    set_cookie_header,
)

NEW_PASSWORD = "an entirely different passphrase"
GENERIC = {"code": "invalid_credentials", "message": "Invalid credentials"}


def body_error(response) -> dict:
    error = dict(response.json()["error"])
    error.pop("request_id")
    return error


@requires_postgres
class LoginTest(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.make_user("alice")

    def test_login_answers_with_the_user_the_session_and_the_auth_state(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["user"],
            {"id": str(self.alice), "login_name": "alice", "system_role": "user"},
        )
        session = body["session"]
        self.assertTrue(session["current"])
        self.assertFalse(session["remember_me"])
        self.assertEqual(session["created_at"], "2030-01-01T12:00:00Z")
        self.assertEqual(session["expires_at"], "2030-01-31T12:00:00Z")
        self.assertEqual(session["absolute_expires_at"], "2030-04-01T12:00:00Z")
        self.assertEqual(
            body["auth"],
            {
                "method": "password",
                "passkey": {
                    "requirement": "optional",
                    "enrolled": False,
                    "enrollment_required": False,
                    "recommended": True,
                    "available": False,
                    "gate": "open",
                    "next": None,
                },
                "step_up": {
                    "method": None,
                    "verified_at": None,
                    "valid_until": None,
                    "window_minutes": 30,
                    "satisfied": False,
                },
            },
        )

    def test_the_cookie_is_secure_httponly_strict_host_scoped_and_a_session_cookie(
        self,
    ):
        response = self.login()
        header = set_cookie_header(response)
        self.assertTrue(header.startswith(f"{SESSION_COOKIE_NAME}="), header)
        self.assertEqual(SESSION_COOKIE_NAME, "__Host-paw_session")
        attributes = {part.strip().lower() for part in header.split(";")[1:]}
        self.assertIn("secure", attributes)
        self.assertIn("httponly", attributes)
        self.assertIn("samesite=strict", attributes)
        self.assertIn("path=/", attributes)
        # __Host- forbids a Domain, and a normal session has no Max-Age / Expires.
        self.assertFalse([a for a in attributes if a.startswith("domain")])
        self.assertFalse(
            [a for a in attributes if a.startswith(("max-age", "expires"))]
        )
        token = self.token_of(response)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")

    def test_the_session_id_is_only_in_the_cookie_never_in_the_body_or_the_database(
        self,
    ):
        response = self.login()
        token = self.token_of(response)
        self.assertNotIn(token, response.text)
        self.assertNotIn(token, str(dict(response.headers)).replace(token, "", 1))
        self.assertNotIn(token, self.everything_stored())
        self.assertNotIn(PASSWORD, self.everything_stored())

    def test_remember_me_sets_a_persistent_cookie_of_90_days(self):
        response = self.login(remember_me=True, device_name="phone")
        header = set_cookie_header(response)
        self.assertRegex(header, r"Max-Age=7776000")
        self.assertTrue(response.json()["session"]["remember_me"])
        self.assertEqual(response.json()["session"]["device_name"], "phone")
        self.assertEqual(
            response.json()["session"]["expires_at"], "2030-04-01T12:00:00Z"
        )

    def test_a_wrong_password_and_an_unknown_name_get_the_same_answer(self):
        wrong = self.login(password="not the password at all")
        unknown = self.login("nobody-here", PASSWORD)
        for response in (wrong, unknown):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(body_error(response), GENERIC)
            self.assertEqual(list(cookie_of(response)), [])
        self.assertEqual(wrong.headers["content-type"], unknown.headers["content-type"])

    def test_an_account_that_cannot_log_in_gets_the_same_answer_too(self):
        self.make_user("pending-one", status="pending_deletion")
        self.make_user("no-password", password=None)
        for name in ("pending-one", "no-password"):
            response = self.login(name, PASSWORD)
            self.assertEqual(
                (response.status_code, body_error(response)), (401, GENERIC)
            )

    def test_five_failures_lock_the_account_and_the_answer_says_when_to_retry(self):
        for _ in range(5):
            self.assertEqual(self.login(password="wrong wrong wrong").status_code, 401)
        response = self.login()  # the right password, while locked
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "30")
        self.assertEqual(error_code(response), "rate_limited")
        self.assertEqual(list(cookie_of(response)), [])
        self.clock.advance(seconds=30)
        self.assertEqual(self.login().status_code, 200)

    def test_the_lock_of_an_unknown_name_looks_the_same(self):
        answers = []
        for _ in range(7):
            response = self.login("nobody-here", "wrong wrong wrong")
            answers.append((response.status_code, response.headers.get("Retry-After")))
        self.assertEqual(answers, [(401,) + (None,)] * 5 + [(429, "30")] * 2)

    def test_a_locked_source_is_refused_whatever_the_account(self):
        for index in range(20):
            self.login(f"guess-{index:03d}", "wrong wrong wrong")
        locked = self.login()
        self.assertEqual(
            (locked.status_code, locked.headers["Retry-After"]), (429, "30")
        )
        other = self.login(source="198.51.100.9")
        self.assertEqual(other.status_code, 200)

    def test_every_failure_is_recorded_for_a_known_account_only(self):
        self.login(password="wrong wrong wrong")
        self.login("nobody-here", "wrong wrong wrong")
        summary = self.audit_summary()
        self.assertEqual(summary[("auth.login", "deny", "invalid_credentials")], 1)
        self.assertEqual(sum(summary.values()), 1)

    def test_a_bad_body_is_a_validation_error_that_never_echoes_the_input(self):
        secret = "pw-that-must-not-come-back"
        cases = [
            {},
            {"login_name": "alice"},
            {"password": secret},
            {"login_name": "alice", "password": secret, "remember": True},
            {"login_name": "alice", "password": secret, "remember_me": "yes"},
            {"login_name": "alice", "password": secret, "remember_me": 1},
            {"login_name": 5, "password": secret},
            {"login_name": "alice", "password": 12345},
            {"login_name": "", "password": secret},
            {"login_name": "a" * 129, "password": secret},
            {"login_name": "alice", "password": "p" * 3000},
            {"login_name": "alice", "password": secret, "device_name": "d" * 65},
            {"login_name": "alice", "password": secret, "device_name": 5},
            {"login_name": "alice", "password": secret, "extra": {"x": 1}},
        ]
        for body in cases:
            with self.subTest(body=repr(body)[:60]):
                response = self.call("POST", "/api/v1/auth/login", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(error_code(response), "validation_error")
                self.assertNotIn(secret, response.text)
        for content in (b"not json", b"[1, 2]", b'"text"', b"null", b""):
            with self.subTest(content=content):
                response = self.call(
                    "POST",
                    "/api/v1/auth/login",
                    content=content,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.scalar("SELECT count(*) FROM auth_sessions"), 0)

    def test_a_device_name_with_control_characters_is_refused(self):
        for name in ("bell\x07", "nul\x00", "line\nbreak"):
            with self.subTest(name=name.encode("unicode_escape")):
                response = self.login(device_name=name)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.scalar("SELECT count(*) FROM auth_sessions"), 0)

    def test_a_password_that_cannot_be_one_is_the_wrong_password(self):
        response = self.login(password="nul\x00inside the password")
        self.assertEqual((response.status_code, body_error(response)), (401, GENERIC))

    def test_the_cookie_of_a_browser_that_logs_in_again_is_replaced(self):
        first = self.token_of(self.login())
        response = self.call(
            "POST",
            "/api/v1/auth/login",
            token=first,
            json={"login_name": "alice", "password": PASSWORD},
        )
        second = self.token_of(response)
        self.assertNotEqual(first, second)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=first).status_code, 401
        )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=second).status_code, 200
        )

    def test_a_client_that_keeps_its_cookies_sends_and_replaces_them_like_a_browser(
        self,
    ):
        self.client.cookies.clear()
        response = self.client.post(
            "/api/v1/auth/login", json={"login_name": "alice", "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/session").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/auth/session").status_code, 401)

    def test_the_response_is_not_cacheable_and_carries_the_request_id(self):
        response = self.call(
            "POST",
            "/api/v1/auth/login",
            headers={"X-Request-ID": "req-login-1"},
            json={"login_name": "alice", "password": PASSWORD},
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Request-ID"], "req-login-1")
        self.assertIn("Strict-Transport-Security", response.headers)

    def test_no_password_reaches_the_log_of_the_application(self):
        with self.assertLogs(level="DEBUG") as logs:
            self.login()
            self.login(password="wrong wrong wrong")
            self.login("nobody-here", "another wrong one")
        output = "\n".join(logs.output)
        for secret in (
            PASSWORD,
            "wrong wrong wrong",
            "another wrong one",
            "$argon2id$",
        ):
            self.assertNotIn(secret, output)


@requires_postgres
class SessionRoutesTest(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.make_user("alice")
        self.token = self.login_token()

    def test_anonymous_requests_are_401_and_never_touch_the_database(self):
        for method, path in (
            ("GET", "/api/v1/auth/session"),
            ("GET", "/api/v1/auth/sessions"),
            ("POST", "/api/v1/auth/logout"),
            ("POST", "/api/v1/auth/sessions/revoke-others"),
            ("POST", "/api/v1/auth/password/change"),
            ("POST", "/api/v1/auth/step-up"),
            ("DELETE", f"/api/v1/auth/sessions/{uuid.uuid4()}"),
        ):
            with self.subTest(route=f"{method} {path}"):
                response = self.call(method, path, json=None if method == "GET" else {})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(error_code(response), "unauthorized")

    def test_the_current_session_endpoint_describes_the_session(self):
        response = self.call("GET", "/api/v1/auth/session", token=self.token)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user"]["login_name"], "alice")
        self.assertTrue(body["session"]["current"])
        # A read sets nothing.
        self.assertEqual(list(cookie_of(response)), [])

    def test_a_cookie_that_is_not_a_session_is_anonymous(self):
        for value in ("", "x", "A" * 43, "A" * 44, "%00", "a b"):
            with self.subTest(value=value):
                response = self.call("GET", "/api/v1/auth/session", token=value)
                self.assertEqual(response.status_code, 401)

    def test_a_client_cannot_assert_its_own_identity(self):
        for headers in (
            {"X-User-Id": str(self.alice)},
            {"X-Role": "owner"},
            {"Authorization": f"Bearer {self.token}"},
            {"Authorization": f"Basic {self.token}"},
        ):
            with self.subTest(headers=list(headers)):
                response = self.call("GET", "/api/v1/auth/session", headers=headers)
                self.assertEqual(response.status_code, 401)
        # A bearer token in the query string or the body is no cookie either.
        response = self.call("GET", f"/api/v1/auth/session?session={self.token}")
        self.assertEqual(response.status_code, 401)

    def test_a_session_ends_after_30_days_without_use_and_use_extends_it(self):
        self.clock.advance(days=29)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 200
        )
        self.clock.advance(days=29)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 200
        )
        self.clock.advance(days=30)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 401
        )

    def test_a_session_ends_at_90_days_however_much_it_is_used(self):
        for day in range(1, 90):
            self.clock.advance(days=1)
            if day % 10 == 0:
                self.assertEqual(
                    self.call(
                        "GET", "/api/v1/auth/session", token=self.token
                    ).status_code,
                    200,
                )
        self.clock.now = T0 + timedelta(days=90)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 401
        )

    def test_remember_me_lasts_beyond_the_idle_limit_of_a_normal_session(self):
        remember = self.login_token(remember_me=True)
        self.clock.advance(days=45)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 401
        )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=remember).status_code, 200
        )

    def test_a_user_who_is_no_longer_active_loses_the_session_at_once(self):
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE users SET status = 'pending_deletion'"))
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 401
        )

    def test_logout_ends_the_session_and_clears_the_cookie(self):
        response = self.call("POST", "/api/v1/auth/logout", token=self.token)
        self.assertEqual(response.status_code, 204)
        header = set_cookie_header(response)
        self.assertRegex(header, r"^__Host-paw_session=\"?\"?;")
        self.assertIn("Max-Age=0", header)
        self.assertIn("Secure", header)
        self.assertIn("HttpOnly", header)
        self.assertEqual(response.content, b"")
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 401
        )
        self.assertEqual(
            self.call("POST", "/api/v1/auth/logout", token=self.token).status_code, 401
        )

    def test_the_device_list_and_signing_a_device_out(self):
        phone = self.login_token(device_name="phone")
        listed = self.call("GET", "/api/v1/auth/sessions", token=self.token).json()[
            "sessions"
        ]
        self.assertEqual(len(listed), 2)
        self.assertEqual([s["current"] for s in listed].count(True), 1)
        self.assertTrue(all(s["expires_at"] and s["last_used_at"] for s in listed))
        phone_id = next(s["id"] for s in listed if s["device_name"] == "phone")
        response = self.call(
            "DELETE", f"/api/v1/auth/sessions/{phone_id}", token=self.token
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(list(cookie_of(response)), [])  # not the current one
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=phone).status_code, 401
        )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 200
        )

    def test_signing_the_current_device_out_by_id_clears_the_cookie(self):
        current = self.call("GET", "/api/v1/auth/session", token=self.token).json()[
            "session"
        ]["id"]
        response = self.call(
            "DELETE", f"/api/v1/auth/sessions/{current}", token=self.token
        )
        self.assertEqual(response.status_code, 204)
        self.assertIn("Max-Age=0", set_cookie_header(response))

    def test_another_users_session_and_an_unknown_one_are_404(self):
        self.make_user("bobby", password="another good passphrase")
        other = self.login_token("bobby", "another good passphrase")
        other_id = self.call("GET", "/api/v1/auth/session", token=other).json()[
            "session"
        ]["id"]
        for session_id in (other_id, str(uuid.uuid4())):
            response = self.call(
                "DELETE", f"/api/v1/auth/sessions/{session_id}", token=self.token
            )
            self.assertEqual(
                (response.status_code, error_code(response)), (404, "not_found")
            )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=other).status_code, 200
        )
        self.assertEqual(
            self.call(
                "DELETE", "/api/v1/auth/sessions/not-a-uuid", token=self.token
            ).status_code,
            422,
        )

    def test_sign_every_other_device_out(self):
        others = [self.login_token() for _ in range(2)]
        response = self.call(
            "POST", "/api/v1/auth/sessions/revoke-others", token=self.token
        )
        self.assertEqual((response.status_code, response.json()), (200, {"revoked": 2}))
        for token in others:
            self.assertEqual(
                self.call("GET", "/api/v1/auth/session", token=token).status_code, 401
            )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 200
        )

    def test_the_guard_of_each_route_is_audited_as_the_capability_it_needs(self):
        self.call(
            "GET", "/api/v1/auth/session", token=self.token
        )  # a read: not recorded
        self.call("POST", "/api/v1/auth/sessions/revoke-others", token=self.token)
        summary = self.audit_summary()
        self.assertEqual(
            summary[("account.manage", "allow", "granted_by_system_role")], 1
        )
        self.assertNotIn(("account.read", "allow", "granted_by_system_role"), summary)
        self.assertEqual(
            summary[("auth.session.revoke_others", "allow", "revoked_others")], 1
        )


@requires_postgres
class PasswordRoutesTest(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.make_user("alice")
        self.token = self.login_token()

    def change(self, token=None, **body):
        payload = {"current_password": PASSWORD, "new_password": NEW_PASSWORD, **body}
        return self.call(
            "POST",
            "/api/v1/auth/password/change",
            token=token or self.token,
            json=payload,
        )

    def test_changing_the_password_rotates_the_cookie_and_keeps_other_devices(self):
        other = self.login_token()
        response = self.change()
        self.assertEqual(response.status_code, 200)
        new_token = self.token_of(response)
        self.assertNotEqual(new_token, self.token)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 401
        )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=new_token).status_code, 200
        )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=other).status_code, 200
        )
        self.assertEqual(self.login(password=PASSWORD).status_code, 401)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)

    def test_on_request_every_other_device_is_signed_out(self):
        other = self.login_token()
        response = self.change(revoke_other_sessions=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=other).status_code, 401
        )
        self.assertEqual(
            self.call(
                "GET", "/api/v1/auth/session", token=self.token_of(response)
            ).status_code,
            200,
        )

    def test_a_wrong_current_password_is_403_and_changes_nothing(self):
        response = self.change(current_password="not my password")
        self.assertEqual((response.status_code, body_error(response)), (403, GENERIC))
        self.assertEqual(list(cookie_of(response)), [])
        self.assertEqual(self.login(password=PASSWORD).status_code, 200)

    def test_a_new_password_that_breaks_the_policy_is_422_and_says_which_rule(self):
        for new, problem in (
            ("short", "too_short"),
            ("password123", "too_common"),
            (PASSWORD, "same_as_current"),
            ("x" * 400, "too_long"),
        ):
            with self.subTest(new=new[:10]):
                response = self.change(new_password=new)
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (422, "password_policy"),
                )
                self.assertIn(problem, response.json()["error"]["message"])
                self.assertNotIn(
                    new[:10] if len(new) > 10 else "\0",
                    response.json()["error"]["message"],
                )

    def test_the_body_is_checked(self):
        for body in (
            {},
            {"current_password": PASSWORD},
            {"new_password": NEW_PASSWORD},
            {
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "revoke_other_sessions": "yes",
            },
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD, "extra": 1},
            {"current_password": 5, "new_password": NEW_PASSWORD},
        ):
            with self.subTest(body=repr(body)[:50]):
                response = self.call(
                    "POST", "/api/v1/auth/password/change", token=self.token, json=body
                )
                self.assertEqual(response.status_code, 422)

    def test_the_stored_hash_is_argon2id(self):
        self.change()
        self.assertTrue(
            self.scalar("SELECT hash FROM password_credentials").startswith(
                "$argon2id$v=19$"
            )
        )

    def test_step_up_marks_the_session_and_rotates_the_cookie(self):
        response = self.call(
            "POST",
            "/api/v1/auth/step-up",
            token=self.token,
            json={"password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        step = response.json()["auth"]["step_up"]
        self.assertEqual(
            (
                step["method"],
                step["satisfied"],
                step["verified_at"],
                step["valid_until"],
            ),
            ("password", True, "2030-01-01T12:00:00Z", "2030-01-01T12:30:00Z"),
        )
        new_token = self.token_of(response)
        self.assertNotEqual(new_token, self.token)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=self.token).status_code, 401
        )
        self.assertTrue(
            self.call("GET", "/api/v1/auth/session", token=new_token).json()["auth"][
                "step_up"
            ]["satisfied"]
        )

    def test_a_wrong_step_up_password_is_403_and_counts_against_the_account(self):
        for _ in range(5):
            response = self.call(
                "POST",
                "/api/v1/auth/step-up",
                token=self.token,
                json={"password": "wrong wrong wrong"},
            )
            self.assertEqual(
                (response.status_code, error_code(response)),
                (403, "invalid_credentials"),
            )
        response = self.call(
            "POST",
            "/api/v1/auth/step-up",
            token=self.token,
            json={"password": PASSWORD},
        )
        self.assertEqual(
            (response.status_code, response.headers["Retry-After"]), (429, "30")
        )

    def test_the_step_up_body_is_checked(self):
        for body in (
            {"method": "sms", "password": PASSWORD},
            {"password": 5},
            {"extra": 1},
        ):
            with self.subTest(body=body):
                response = self.call(
                    "POST", "/api/v1/auth/step-up", token=self.token, json=body
                )
                self.assertEqual(response.status_code, 422)
        # A method nobody has registered (PAW-023's) is refused, not guessed.
        response = self.call(
            "POST", "/api/v1/auth/step-up", token=self.token, json={"method": "passkey"}
        )
        self.assertEqual(response.status_code, 422)

    def test_no_password_reaches_a_log_line(self):
        with self.assertLogs(level="DEBUG") as logs:
            self.change()
            self.change(
                current_password="not my password",
                new_password="yet another passphrase",
            )
        output = "\n".join(logs.output)
        for secret in (
            PASSWORD,
            NEW_PASSWORD,
            "not my password",
            "yet another passphrase",
            "$argon2id$",
        ):
            self.assertNotIn(secret, output)


@requires_postgres
class CommittedChangeIsAlwaysDeliveredTest(HttpTestCase):
    """A login, password change or step-up that COMMITTED must hand over its cookie.

    Each of them commits (a session, a rotated session id) and then reads the
    policy to describe the session. That read can fail (its own query times
    out); the response must still carry the new cookie and say "done", because
    the old cookie (after a rotation) is already dead: a 503 without the cookie
    would sign the user out while telling them the change failed.
    """

    FAILURES = (
        ("the policy query is unavailable", AuthUnavailableError()),
        ("the policy query times out", TimeoutError()),
        ("an unexpected error", RuntimeError("policy-secret-detail-022")),
    )

    def setUp(self):
        super().setUp()
        self.make_user("alice")

    @contextlib.contextmanager
    def policy_read_fails(self, error):
        async def failing(self_):
            raise error

        with patch.object(type(self.services.policy), "get", failing):
            yield

    def assert_degraded(self, response, *, cookie_of_new_session=True):
        """200, the cookie, the user and session; ``auth`` left out (not failed)."""
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["auth"])
        self.assertEqual(body["user"]["login_name"], "alice")
        self.assertTrue(body["session"]["current"])
        if cookie_of_new_session:
            self.assertRegex(self.token_of(response), r"^[A-Za-z0-9_-]{43}$")

    def test_a_login_delivers_its_cookie_when_the_policy_read_fails(self):
        for label, error in self.FAILURES:
            with self.subTest(label):
                with self.policy_read_fails(error):
                    response = self.login()
                self.assert_degraded(response)
                token = self.token_of(response)
                # The session exists: with the policy readable again it is valid,
                # and the body that was left out is there.
                described = self.call("GET", "/api/v1/auth/session", token=token)
                self.assertEqual(described.status_code, 200)
                self.assertEqual(described.json()["auth"]["method"], "password")

    def test_a_remember_me_login_keeps_its_persistent_cookie(self):
        with self.policy_read_fails(AuthUnavailableError()):
            response = self.login(remember_me=True)
        self.assert_degraded(response)
        self.assertRegex(set_cookie_header(response), r"Max-Age=7776000")

    def test_a_password_change_delivers_the_rotated_cookie(self):
        for label, error in self.FAILURES:
            with self.subTest(label):
                old = self.login_token(password=PASSWORD)
                with self.policy_read_fails(error):
                    response = self.call(
                        "POST",
                        "/api/v1/auth/password/change",
                        token=old,
                        json={
                            "current_password": PASSWORD,
                            "new_password": NEW_PASSWORD,
                        },
                    )
                self.assert_degraded(response)
                rotated = self.token_of(response)
                # What was committed: the old id is dead, the new one lives, the
                # new password is the password.
                self.assertNotEqual(rotated, old)
                self.assertEqual(
                    self.call("GET", "/api/v1/auth/session", token=old).status_code, 401
                )
                self.assertEqual(
                    self.call("GET", "/api/v1/auth/session", token=rotated).status_code,
                    200,
                )
                self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)
                # Back to the original password for the next failure.
                back = self.call(
                    "POST",
                    "/api/v1/auth/password/change",
                    token=rotated,
                    json={"current_password": NEW_PASSWORD, "new_password": PASSWORD},
                )
                self.assertEqual(back.status_code, 200)

    def test_a_step_up_delivers_the_rotated_cookie(self):
        for label, error in self.FAILURES:
            with self.subTest(label):
                old = self.login_token()
                with self.policy_read_fails(error):
                    response = self.call(
                        "POST",
                        "/api/v1/auth/step-up",
                        token=old,
                        json={"password": PASSWORD},
                    )
                self.assert_degraded(response)
                rotated = self.token_of(response)
                self.assertNotEqual(rotated, old)
                self.assertEqual(
                    self.call("GET", "/api/v1/auth/session", token=old).status_code, 401
                )
                described = self.call("GET", "/api/v1/auth/session", token=rotated)
                self.assertEqual(
                    described.json()["auth"]["step_up"]["method"], "password"
                )

    def test_the_passkey_lookup_failing_is_survived_too(self):
        class Broken:
            async def is_enrolled(self, user_id):
                raise RuntimeError("passkey-secret-detail-022")

        token = self.login_token()
        with patch.object(self.services.service, "_passkeys", Broken()):
            response = self.call(
                "POST",
                "/api/v1/auth/step-up",
                token=token,
                json={"password": PASSWORD},
            )
        self.assert_degraded(response)

    def test_the_failure_is_logged_by_type_only(self):
        with self.assertLogs("paw_backend.api.v1.auth", "WARNING") as logs:
            with self.policy_read_fails(RuntimeError("policy-secret-detail-022")):
                self.login()
        output = "\n".join(logs.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("policy-secret-detail-022", output)
        self.assertNotIn(PASSWORD, output)

    def test_a_change_that_did_not_commit_still_fails_and_sets_no_cookie(self):
        token = self.login_token()
        response = self.call(
            "POST",
            "/api/v1/auth/password/change",
            token=token,
            json={"current_password": "not my password", "new_password": NEW_PASSWORD},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(list(cookie_of(response)), [])
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=token).status_code, 200
        )

    def test_a_read_of_the_session_has_no_cookie_to_deliver_and_still_fails_closed(
        self,
    ):
        token = self.login_token()
        with self.policy_read_fails(AuthUnavailableError()):
            response = self.call("GET", "/api/v1/auth/session", token=token)
        self.assertEqual(
            (response.status_code, error_code(response)), (503, "service_unavailable")
        )
        self.assertEqual(list(cookie_of(response)), [])

    def test_the_degraded_answer_is_a_valid_session_response_for_a_client(self):
        with self.policy_read_fails(AuthUnavailableError()):
            body = self.login().json()
        self.assertEqual(sorted(body), ["auth", "session", "user"])
        self.assertEqual(
            sorted(body["session"]),
            [
                "absolute_expires_at",
                "created_at",
                "current",
                "device_name",
                "expires_at",
                "id",
                "last_used_at",
                "remember_me",
            ],
        )


@requires_postgres
class SameSiteSettingTest(HttpTestCase):
    settings_overrides = {"session_cookie_samesite": "lax"}

    def test_the_samesite_setting_is_honoured(self):
        self.make_user("alice")
        header = set_cookie_header(self.login())
        self.assertIn("SameSite=lax", header)
        self.assertIn("Secure", header)
        self.assertIn("HttpOnly", header)


if __name__ == "__main__":
    unittest.main()
