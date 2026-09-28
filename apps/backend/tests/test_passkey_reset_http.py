"""``POST /api/v1/auth/users/{id}/passkeys/reset`` over HTTP (#108, PostgreSQL).

The whole story of an Admin who lost every device: the Owner steps up with a
Passkey, resets the Admin, receives the one-time password reset token once, the
Admin sets a new password with ``POST /auth/token/redeem`` and enrols again. And the
refusals: no step-up, a password step-up, the wrong role, an unknown account, an
Agent, a restricted session, another origin.
"""

import uuid
from datetime import timedelta

from .auth_support import requires_postgres
from .passkey_http_support import (
    ADMIN_PASSWORD,
    API,
    OWNER_PASSWORD,
    PasskeyHttpTestCase,
    error_code,
)
from .passkey_pg_support import device

NEW_PASSWORD = "a passphrase chosen after the reset"


def reset_url(user_id) -> str:
    return f"{API}/users/{user_id}/passkeys/reset"


@requires_postgres
class ResetStoryTest(PasskeyHttpTestCase):
    def setUp(self):
        super().setUp()
        # The token function refuses a ``created_at`` more than 5 minutes off the
        # database's clock: the fake clock starts just ahead of it.
        self.clock.now = self.started_at + timedelta(minutes=1)

    def test_the_owner_brings_back_an_admin_who_lost_every_device(self):
        admin_token, _lost = self.enrolled("admin-one", ADMIN_PASSWORD, role="admin")
        admin_id = self.scalar("SELECT id FROM users WHERE login_name = 'admin-one'")
        # With the device gone, a password sign-in only reaches the assertion gate.
        pending = self.login_token("admin-one", ADMIN_PASSWORD)
        self.assertEqual(self.gate_of(pending), "assertion_required")

        owner_token, _ = self.stepped_up()
        response = self.call("POST", reset_url(admin_id), token=owner_token)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            {k: body[k] for k in ("user_id", "passkeys_revoked", "sessions_ended")},
            {"user_id": str(admin_id), "passkeys_revoked": 1, "sessions_ended": 2},
        )
        self.assertEqual(body["next"], "deliver_reset_token")
        self.assertEqual(response.headers["cache-control"], "no-store")
        token = body["reset_token"]
        self.assertTrue(token.startswith("pawst1."))
        # Every session of the Admin ended; the old password no longer signs in.
        for old in (admin_token, pending):
            self.assertEqual(
                self.call("GET", f"{API}/session", token=old).status_code, 401
            )
        self.assertEqual(self.login("admin-one", ADMIN_PASSWORD).status_code, 401)

        redeemed = self.call(
            "POST",
            f"{API}/token/redeem",
            json={"token": token, "new_password": NEW_PASSWORD},
        )
        self.assertEqual(redeemed.status_code, 200, redeemed.text)
        self.assertEqual(
            redeemed.json(),
            {"purpose": "password_reset", "passkey_required": True, "next": "login"},
        )
        fresh = self.login_token("admin-one", NEW_PASSWORD)
        self.assertEqual(self.gate_of(fresh), "enrollment_required")
        registered, fresh = self.register(fresh, device())
        self.assertEqual(registered.status_code, 200, registered.text)
        self.assertEqual(self.gate_of(fresh), "open")
        summary = self.audit_summary()
        self.assertEqual(summary[("auth.passkey.reset", "allow", "reset")], 1)
        # The token is in no table and no audit row as text.
        self.assertNotIn(token.split(".")[2], self.everything_stored())

    def test_the_refusals_are_typed_and_change_nothing(self):
        alice = self.make_user("alice")
        admin_token, admin_device = self.enrolled(
            "admin-one", ADMIN_PASSWORD, role="admin"
        )
        owner_id = self.make_user("boss", role="owner", password=OWNER_PASSWORD)
        other_admin = self.make_user("admin-two", role="admin")
        # No step-up yet (the enrolment is not one): refused before any lookup.
        for target in (alice, uuid.uuid4()):
            response = self.call("POST", reset_url(target), token=admin_token)
            self.assertEqual(
                (response.status_code, error_code(response)),
                (403, "step_up_required"),
            )
        # A password step-up is not a Passkey one.
        stepped = self.call(
            "POST",
            f"{API}/step-up",
            token=admin_token,
            json={"password": ADMIN_PASSWORD},
        )
        self.assertEqual(stepped.status_code, 200, stepped.text)
        admin_token = self.token_after(stepped, admin_token)
        response = self.call("POST", reset_url(alice), token=admin_token)
        self.assertEqual(
            (response.status_code, error_code(response)),
            (403, "step_up_method_insufficient"),
        )
        _, admin_token = self.authenticate(admin_token, admin_device)
        for target in (other_admin, owner_id):
            response = self.call("POST", reset_url(target), token=admin_token)
            self.assertEqual(
                (response.status_code, error_code(response)), (403, "forbidden")
            )
        response = self.call("POST", reset_url(uuid.uuid4()), token=admin_token)
        self.assertEqual(
            (response.status_code, error_code(response)), (404, "not_found")
        )
        response = self.call(
            "POST", f"{API}/users/not-a-uuid/passkeys/reset", token=admin_token
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.rows("SELECT * FROM setup_tokens"), [])
        # An Admin may reset a User.
        self.assertEqual(
            self.call("POST", reset_url(alice), token=admin_token).status_code, 200
        )

    def test_a_user_holds_no_capability_for_it(self):
        bob = self.make_user("bob")
        self.make_user("carol")
        token = self.login_token("carol")
        self.fake_passkey_step_up(token)
        response = self.call("POST", reset_url(bob), token=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.rows("SELECT * FROM setup_tokens"), [])

    def test_a_cross_origin_page_cannot_reset_anyone(self):
        alice = self.make_user("alice")
        owner_token, _ = self.stepped_up()
        response = self.call(
            "POST",
            reset_url(alice),
            token=owner_token,
            origin="https://evil.example",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.rows("SELECT * FROM setup_tokens"), [])

    def test_without_a_session_it_is_401(self):
        alice = self.make_user("alice")
        response = self.call("POST", reset_url(alice))
        self.assertEqual(
            (response.status_code, error_code(response)), (401, "unauthorized")
        )
