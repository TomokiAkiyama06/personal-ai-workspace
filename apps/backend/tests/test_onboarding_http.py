"""PAW-024 over HTTP: invitations, the user lifecycle and pairing (real PostgreSQL)."""

from .auth_http_support import (
    HttpTestCase,
    cookie_of,
    error_code,
    requires_postgres,
)

NEW_PASSWORD = "a brand new passphrase here"


class OnboardingHttpCase(HttpTestCase):
    def administrator(self, role="admin", name=None) -> str:
        """A signed-in, Passkey-stepped-up administrator's session token."""
        name = name or f"{role}-one"
        self.make_user(name, role=role)
        token = self.login_token(name)
        self.fake_passkey_step_up(token)
        return token

    def status_of(self, name: str) -> str:
        return self.scalar("SELECT status FROM users WHERE login_name = :n", n=name)


@requires_postgres
class InvitationRoutesTest(OnboardingHttpCase):
    def test_invite_redeem_and_sign_in(self):
        admin = self.administrator()

        response = self.call(
            "POST",
            "/api/v1/auth/invitations",
            token=admin,
            json={"login_name": "bob"},
        )

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(
            (body["login_name"], body["system_role"], body["status"]),
            ("bob", "user", "invited"),
        )
        redeemed = self.call(
            "POST",
            "/api/v1/auth/invitations/redeem",
            json={"token": body["token"], "new_password": NEW_PASSWORD},
        )
        self.assertEqual(redeemed.status_code, 200, redeemed.text)
        self.assertEqual(redeemed.json(), {"next": "login"})
        self.assertEqual(cookie_of(redeemed), {})  # nobody is signed in
        self.assertEqual(self.login("bob", NEW_PASSWORD).status_code, 200)
        again = self.call(
            "POST",
            "/api/v1/auth/invitations/redeem",
            json={"token": body["token"], "new_password": NEW_PASSWORD},
        )
        self.assertEqual((again.status_code, error_code(again)), (400, "invalid_token"))

    def test_the_errors_of_the_admin_routes(self):
        admin = self.administrator()
        path = "/api/v1/auth/invitations"
        taken = self.call("POST", path, token=admin, json={"login_name": "admin-one"})
        self.assertEqual(
            (taken.status_code, error_code(taken)), (409, "login_name_taken")
        )
        admin_role = self.call(
            "POST",
            path,
            token=admin,
            json={"login_name": "x-y", "system_role": "admin"},
        )
        self.assertEqual(
            (admin_role.status_code, error_code(admin_role)), (403, "forbidden")
        )
        owner_role = self.call(
            "POST",
            path,
            token=admin,
            json={"login_name": "x-y", "system_role": "owner"},
        )
        self.assertEqual(owner_role.status_code, 422)
        extra = self.call(
            "POST", path, token=admin, json={"login_name": "x-y", "extra": 1}
        )
        self.assertEqual(extra.status_code, 422)
        # A plain user holds no admin.users.manage.
        self.make_user("plain")
        plain = self.login_token("plain")
        self.assertEqual(
            self.call(
                "POST", path, token=plain, json={"login_name": "x-y"}
            ).status_code,
            403,
        )
        # No session: 401.
        self.assertEqual(
            self.call("POST", path, json={"login_name": "x-y"}).status_code, 401
        )

    def test_an_invitation_needs_a_passkey_step_up(self):
        self.make_user("admin-two", role="admin")
        token = self.login_token("admin-two")
        response = self.call(
            "POST", "/api/v1/auth/invitations", token=token, json={"login_name": "bob"}
        )
        self.assertEqual(
            (response.status_code, error_code(response)), (403, "step_up_required")
        )

    def test_reissue_revoke_and_cancel(self):
        admin = self.administrator()
        body = self.call(
            "POST", "/api/v1/auth/invitations", token=admin, json={"login_name": "bob"}
        ).json()
        user_id = body["user_id"]
        reissued = self.call(
            "POST", f"/api/v1/auth/invitations/{user_id}/reissue", token=admin
        )
        self.assertEqual(reissued.status_code, 200, reissued.text)
        self.assertNotEqual(reissued.json()["token"], body["token"])
        revoked = self.call(
            "POST", f"/api/v1/auth/invitations/{user_id}/revoke", token=admin
        )
        self.assertEqual(revoked.status_code, 204)
        missing = self.call(
            "POST", f"/api/v1/auth/invitations/{user_id}/revoke", token=admin
        )
        self.assertEqual((missing.status_code, error_code(missing)), (404, "not_found"))
        cancelled = self.call("DELETE", f"/api/v1/auth/users/{user_id}", token=admin)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "deleted")

    def test_the_public_route_is_rate_limited(self):
        for _ in range(5):
            self.call(
                "POST",
                "/api/v1/auth/invitations/redeem",
                json={"token": "garbage", "new_password": NEW_PASSWORD},
            )
        response = self.call(
            "POST",
            "/api/v1/auth/invitations/redeem",
            json={"token": "garbage", "new_password": NEW_PASSWORD},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)


@requires_postgres
class LifecycleRoutesTest(OnboardingHttpCase):
    def test_delete_and_restore(self):
        owner = self.administrator("owner")
        bob = self.make_user("bob")
        bob_token = self.login_token("bob")

        deleted = self.call("DELETE", f"/api/v1/auth/users/{bob}", token=owner)

        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(
            deleted.json(), {"user_id": str(bob), "status": "pending_deletion"}
        )
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=bob_token).status_code, 401
        )
        again = self.call("DELETE", f"/api/v1/auth/users/{bob}", token=owner)
        self.assertEqual((again.status_code, error_code(again)), (409, "invalid_state"))
        restored = self.call("POST", f"/api/v1/auth/users/{bob}/restore", token=owner)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["status"], "active")
        self.assertEqual(self.login("bob").status_code, 200)

    def test_an_admin_may_not_restore(self):
        owner = self.administrator("owner")
        admin = self.administrator()
        bob = self.make_user("bob")
        self.call("DELETE", f"/api/v1/auth/users/{bob}", token=owner)
        refused = self.call("POST", f"/api/v1/auth/users/{bob}/restore", token=admin)
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.status_of("bob"), "pending_deletion")

    def test_the_owner_is_not_found_and_an_admin_is_the_owners(self):
        admin = self.administrator()
        owner_id = self.make_user("boss", role="owner")
        carol = self.make_user("carol", role="admin")
        response = self.call("DELETE", f"/api/v1/auth/users/{owner_id}", token=admin)
        self.assertEqual(response.status_code, 404)
        response = self.call("DELETE", f"/api/v1/auth/users/{carol}", token=admin)
        self.assertEqual(response.status_code, 403)


@requires_postgres
class PairingRoutesTest(OnboardingHttpCase):
    def test_a_user_adds_a_device_by_link(self):
        self.make_user("bob")
        trusted = self.login_token("bob")

        issued = self.call("POST", "/api/v1/auth/pairing", token=trusted)

        self.assertEqual(issued.status_code, 201, issued.text)
        body = issued.json()
        self.assertEqual(body["link_path"], "/pair#" + body["token"])
        self.assertFalse(body["approval_required"])
        claimed = self.call(
            "POST",
            "/api/v1/auth/pairing/claim",
            source="192.0.2.50",
            json={"token": body["token"], "device_name": "Phone"},
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["status"], "completed")
        new_device = self.token_of(claimed)
        session = self.call("GET", "/api/v1/auth/session", token=new_device).json()
        self.assertEqual(session["user"]["login_name"], "bob")
        self.assertEqual(session["auth"]["method"], "pairing")
        devices = self.call("GET", "/api/v1/auth/sessions", token=trusted).json()
        self.assertIn("Phone", [d["device_name"] for d in devices["sessions"]])
        reused = self.call(
            "POST",
            "/api/v1/auth/pairing/claim",
            json={"token": body["token"], "device_name": "Laptop"},
        )
        self.assertEqual(
            (reused.status_code, error_code(reused)), (400, "invalid_token")
        )

    def test_an_admin_device_waits_for_the_approval(self):
        trusted = self.administrator()
        body = self.call("POST", "/api/v1/auth/pairing", token=trusted).json()
        self.assertTrue(body["approval_required"])

        claimed = self.call(
            "POST",
            "/api/v1/auth/pairing/claim",
            json={"token": body["token"], "device_name": "Tablet", "remember_me": True},
        )
        self.assertEqual(claimed.status_code, 202, claimed.text)
        self.assertEqual(claimed.json()["status"], "pending_approval")
        self.assertEqual(cookie_of(claimed), {})
        claim = claimed.json()["claim"]
        waiting = self.call(
            "POST", "/api/v1/auth/pairing/complete", json={"claim": claim}
        )
        self.assertEqual(waiting.status_code, 202)
        self.assertIsNone(waiting.json()["claim"])

        pending = self.call("GET", "/api/v1/auth/pairing/pending", token=trusted).json()
        self.assertEqual(
            [(p["pairing_id"], p["device_name"]) for p in pending["pending"]],
            [(body["pairing_id"], "Tablet")],
        )
        approved = self.call(
            "POST", f"/api/v1/auth/pairing/{body['pairing_id']}/approve", token=trusted
        )
        self.assertEqual(approved.status_code, 204, approved.text)
        done = self.call("POST", "/api/v1/auth/pairing/complete", json={"claim": claim})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["status"], "completed")
        self.assertIn("Max-Age", done.headers["set-cookie"])  # remember_me
        new_device = self.token_of(done)
        self.assertEqual(
            self.call("GET", "/api/v1/auth/session", token=new_device).status_code, 200
        )

    def test_approving_without_a_passkey_step_up_is_refused(self):
        self.make_user("carol", role="admin")
        trusted = self.login_token("carol")
        body = self.call("POST", "/api/v1/auth/pairing", token=trusted).json()
        self.call(
            "POST",
            "/api/v1/auth/pairing/claim",
            json={"token": body["token"], "device_name": "Tablet"},
        )
        refused = self.call(
            "POST", f"/api/v1/auth/pairing/{body['pairing_id']}/approve", token=trusted
        )
        self.assertEqual(
            (refused.status_code, error_code(refused)), (403, "step_up_required")
        )
        rejected = self.call(
            "POST", f"/api/v1/auth/pairing/{body['pairing_id']}/reject", token=trusted
        )
        self.assertEqual(rejected.status_code, 204)

    def test_revoke_and_the_errors(self):
        self.make_user("bob")
        trusted = self.login_token("bob")
        body = self.call("POST", "/api/v1/auth/pairing", token=trusted).json()
        revoked = self.call("DELETE", "/api/v1/auth/pairing", token=trusted)
        self.assertEqual(revoked.json(), {"revoked": 1})
        dead = self.call(
            "POST",
            "/api/v1/auth/pairing/claim",
            json={"token": body["token"], "device_name": "Phone"},
        )
        self.assertEqual(dead.status_code, 400)
        no_name = self.call(
            "POST", "/api/v1/auth/pairing/claim", json={"token": body["token"]}
        )
        self.assertEqual(no_name.status_code, 422)
        self.assertEqual(self.call("POST", "/api/v1/auth/pairing").status_code, 401)
        unknown = self.call(
            "POST",
            # (approving checks the step-up first, like unlocking an account)
            "/api/v1/auth/pairing/00000000-0000-4000-8000-000000000000/reject",
            token=trusted,
        )
        self.assertEqual(unknown.status_code, 404)

    def test_a_cross_origin_claim_is_refused(self):
        response = self.call(
            "POST",
            "/api/v1/auth/pairing/claim",
            origin="https://evil.example",
            json={"token": "x", "device_name": "Phone"},
        )
        self.assertEqual(
            (response.status_code, error_code(response)), (403, "forbidden_origin")
        )

    def test_nothing_secret_is_stored(self):
        self.make_user("bob")
        trusted = self.login_token("bob")
        body = self.call("POST", "/api/v1/auth/pairing", token=trusted).json()
        claimed = self.call(
            "POST",
            "/api/v1/auth/pairing/claim",
            json={"token": body["token"], "device_name": "Phone"},
        )
        stored = self.everything_stored() + "\n".join(
            str(r[0]) for r in self.rows("SELECT t::text FROM device_pairings t")
        )
        for value in (
            body["token"],
            body["token"].rsplit(".", 1)[1],
            self.token_of(claimed),
        ):
            self.assertNotIn(value, stored)
