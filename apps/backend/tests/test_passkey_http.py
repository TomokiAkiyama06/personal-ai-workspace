"""The Passkey routes over HTTP (real PostgreSQL): the whole story, and its errors."""

import uuid
from unittest.mock import patch

from paw_backend.app import create_app
from paw_backend.auth.limits import SESSION_COOKIE_NAME

from .auth_support import requires_postgres
from .passkey_http_support import (
    API,
    OWNER_PASSWORD,
    PASSKEYS,
    PasskeyHttpTestCase,
    cookie_of,
    error_code,
)
from .passkey_pg_support import RP_ID, device
from .passkey_support import b64url, unb64url
from .support import FakeDatabase, make_settings

RESTRICTED_ROUTES = (
    ("GET", f"{API}/sessions"),
    ("POST", f"{API}/sessions/revoke-others"),
    ("DELETE", f"{API}/sessions/{uuid.uuid4()}"),
    ("POST", f"{API}/password/change"),
    ("POST", f"{API}/step-up"),
    ("POST", f"{API}/users/{uuid.uuid4()}/unlock"),
    ("GET", f"{API}/policy"),
    ("PUT", f"{API}/policy"),
    ("DELETE", f"{PASSKEYS}/{uuid.uuid4()}"),
)
ALLOWED_WHILE_RESTRICTED = (
    ("GET", f"{API}/session"),
    ("POST", f"{API}/logout"),
    ("GET", PASSKEYS),
    ("POST", f"{PASSKEYS}/enroll/begin"),
    ("POST", f"{PASSKEYS}/enroll/finish"),
    ("POST", f"{PASSKEYS}/authenticate/begin"),
    ("POST", f"{PASSKEYS}/authenticate/finish"),
)


def _dependants(dependant):
    yield dependant
    for child in dependant.dependencies:
        yield from _dependants(child)


def restricted_routes(app) -> set[tuple[str, str]]:
    """The (method, path) of every route whose guard accepts a restricted session."""
    found = set()
    for route in app.routes:
        if not hasattr(route, "effective_route_contexts"):
            continue
        for context in route.effective_route_contexts():
            calls = [depends.dependency for depends in context.dependencies or []]
            for dependant in (
                context.dependant,
                getattr(context.original_route, "dependant", None),
            ):
                if dependant is not None:
                    calls += [d.call for d in _dependants(dependant)]
            if any(getattr(call, "paw_allow_restricted", False) for call in calls):
                for method in context.original_route.methods - {"HEAD"}:
                    found.add((method, context.path or ""))
    return found


@requires_postgres
class RouteInventoryTest(PasskeyHttpTestCase):
    def test_exactly_these_routes_accept_a_restricted_session(self):
        self.assertEqual(
            restricted_routes(self.app),
            {
                (method, path)
                for method, path in ALLOWED_WHILE_RESTRICTED
                if "{" not in path
            },
        )

    def test_every_other_authenticated_route_refuses_a_restricted_session(self):
        self.owner()
        token = self.login_token("boss", OWNER_PASSWORD)
        self.assertEqual(self.gate_of(token), "enrollment_required")
        for method, path in RESTRICTED_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                response = self.call(method, path, token=token, json={})
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (403, "passkey_required"),
                )

    def test_the_routes_a_restricted_session_needs_answer(self):
        self.owner()
        token = self.login_token("boss", OWNER_PASSWORD)
        for method, path in (
            ("GET", f"{API}/session"),
            ("GET", PASSKEYS),
            ("POST", f"{PASSKEYS}/enroll/begin"),
        ):
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(self.call(method, path, token=token).status_code, 200)
        # Nothing to authenticate with yet: a typed answer, not a 500.
        response = self.call("POST", f"{PASSKEYS}/authenticate/begin", token=token)
        self.assertEqual(
            (response.status_code, error_code(response)), (409, "no_passkey")
        )
        # It can leave.
        self.assertEqual(
            self.call("POST", f"{API}/logout", token=token).status_code, 204
        )
        self.assertEqual(
            self.call("GET", f"{API}/session", token=token).status_code, 401
        )

    def test_a_route_without_a_session_is_401_not_403(self):
        for method, path in (*RESTRICTED_ROUTES, *ALLOWED_WHILE_RESTRICTED):
            with self.subTest(route=f"{method} {path}"):
                response = self.call(method, path, json={})
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (401, "unauthorized"),
                )


@requires_postgres
class OwnerStoryTest(PasskeyHttpTestCase):
    """From the first sign-in of the Owner to a policy change with a Passkey."""

    def test_the_owner_enrols_signs_in_again_and_changes_the_policy(self):
        self.owner()
        # 1. Password sign-in: an enrolment-only session.
        response = self.login("boss", OWNER_PASSWORD)
        self.assertEqual(response.status_code, 200)
        passkey = response.json()["auth"]["passkey"]
        self.assertEqual(
            (
                passkey["gate"],
                passkey["next"],
                passkey["available"],
                passkey["enrolled"],
            ),
            ("enrollment_required", "register", True, False),
        )
        first = self.token_of(response)
        # 2. Register a Passkey: the gate opens, the cookie rotates, the old one dies.
        authenticator = device()
        response, opened = self.register(first, authenticator, name="Boss laptop")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["passkey"]["name"], "Boss laptop")
        self.assertEqual(body["session"]["auth"]["passkey"]["gate"], "open")
        self.assertNotEqual(opened, first)
        self.assertEqual(
            self.call("GET", f"{API}/session", token=first).status_code, 401
        )
        self.assertEqual(
            self.call("GET", f"{API}/sessions", token=opened).status_code, 200
        )
        # 3. The policy cannot be changed yet: no step-up (registering is not one).
        current = self.call("GET", f"{API}/policy", token=opened).json()
        change = {
            **{
                k: current[k]
                for k in (
                    "passkey_owner",
                    "passkey_admin",
                    "passkey_user",
                    "recommend_passkey_to_users",
                    "stepup_window_minutes",
                )
            },
            "expected_version": current["version"],
            "passkey_admin": "optional",
        }
        response = self.call("PUT", f"{API}/policy", token=opened, json=change)
        self.assertEqual(
            (response.status_code, error_code(response)), (403, "step_up_required")
        )
        # 4. A password step-up is not enough (Decision 0015 section 12).
        stepped = self.call(
            "POST", f"{API}/step-up", token=opened, json={"password": OWNER_PASSWORD}
        )
        self.assertEqual(stepped.status_code, 200)
        by_password = self.token_of(stepped)
        response = self.call("PUT", f"{API}/policy", token=by_password, json=change)
        self.assertEqual(
            (response.status_code, error_code(response)),
            (403, "step_up_method_insufficient"),
        )
        # 5. A Passkey step-up is: the policy changes, end to end.
        response, by_passkey = self.authenticate(by_password, authenticator)
        self.assertEqual(response.status_code, 200, response.text)
        step_up = response.json()["auth"]["step_up"]
        self.assertEqual((step_up["method"], step_up["satisfied"]), ("passkey", True))
        response = self.call("PUT", f"{API}/policy", token=by_passkey, json=change)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            (response.json()["passkey_admin"], response.json()["version"]),
            ("optional", current["version"] + 1),
        )
        # 6. The next sign-in is restricted again (Owner: required): it waits for the
        #    Passkey ...
        again = self.login_token("boss", OWNER_PASSWORD)
        state = self.call("GET", f"{API}/session", token=again).json()["auth"][
            "passkey"
        ]
        self.assertEqual(
            (state["gate"], state["next"]), ("assertion_required", "authenticate")
        )
        # ... and completing the authentication opens it (and is a step-up too).
        response, opened_again = self.authenticate(again, authenticator)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["auth"]["passkey"]["gate"], "open")
        self.assertEqual(response.json()["auth"]["step_up"]["method"], "passkey")
        self.assertEqual(
            self.call("GET", f"{API}/policy", token=opened_again).status_code, 200
        )

    def test_an_admin_needs_a_passkey_step_up_to_unlock_an_account(self):
        alice = self.make_user("alice")
        for _ in range(5):
            self.login("alice", "wrong wrong wrong", source="198.51.100.4")
        self.assertEqual(self.login("alice", source="203.0.113.99").status_code, 429)
        token, authenticator = self.enrolled(
            "admin-one", "admin passphrase", role="admin"
        )
        url = f"{API}/users/{alice}/unlock"
        response = self.call("POST", url, token=token)
        self.assertEqual(
            (response.status_code, error_code(response)), (403, "step_up_required")
        )
        response, token = self.authenticate(token, authenticator)
        self.assertEqual(self.call("POST", url, token=token).status_code, 204)
        self.assertEqual(self.login("alice", source="203.0.113.99").status_code, 200)
        # A Passkey step-up is judged with the policy's window (30 minutes).
        self.clock.advance(minutes=31)
        response = self.call("POST", url, token=token)
        self.assertEqual(
            (response.status_code, error_code(response)), (403, "step_up_required")
        )

    def test_the_step_up_endpoint_only_takes_a_password(self):
        token, _ = self.enrolled()
        for body in ({"method": "passkey"}, {"method": "passkey", "password": "x"}):
            with self.subTest(body=body):
                response = self.call("POST", f"{API}/step-up", token=token, json=body)
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (422, "validation_error"),
                )


@requires_postgres
class PasskeyRoutesTest(PasskeyHttpTestCase):
    def test_the_list_shows_the_devices_and_nothing_secret(self):
        token, authenticator = self.stepped_up()
        response, token = self.register(
            token, device(backup_eligible=True), name="Phone"
        )
        self.assertEqual(response.status_code, 200, response.text)
        listed = self.call("GET", PASSKEYS, token=token).json()["passkeys"]
        self.assertEqual(len(listed), 2)
        for entry in listed:
            self.assertEqual(
                set(entry),
                {
                    "id",
                    "name",
                    "created_at",
                    "last_used_at",
                    "backup_eligible",
                    "backed_up",
                },
            )
        self.assertEqual(sorted(e["name"] for e in listed), ["Passkey", "Phone"])
        credential = b64url(authenticator.credentials[0].credential_id)
        self.assertNotIn(credential, self.call("GET", PASSKEYS, token=token).text)

    def test_a_device_is_revoked_over_http(self):
        token, authenticator = self.stepped_up()
        other = device()
        response, token = self.register(token, other, name="Second")
        listed = self.call("GET", PASSKEYS, token=token).json()["passkeys"]
        first_id = next(p["id"] for p in listed if p["name"] == "Passkey")
        response = self.call("DELETE", f"{PASSKEYS}/{first_id}", token=token)
        # This session was opened by the first Passkey: it ends with it.
        self.assertEqual(
            response.json(), {"revoked": True, "sessions_ended": 1, "signed_out": True}
        )
        cleared = cookie_of(response)
        self.assertEqual(cleared[SESSION_COOKIE_NAME].value, "")
        self.assertEqual(
            self.call("GET", f"{API}/session", token=token).status_code, 401
        )
        # Signing in again: the remaining Passkey is the one to use.
        again = self.login_token("boss", OWNER_PASSWORD)
        self.assertEqual(self.gate_of(again), "assertion_required")
        response, again = self.authenticate(again, other)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            len(self.call("GET", PASSKEYS, token=again).json()["passkeys"]), 1
        )

    def test_revoking_needs_a_step_up_and_the_last_required_passkey_stays(self):
        token, authenticator = self.stepped_up()
        only = self.call("GET", PASSKEYS, token=token).json()["passkeys"][0]["id"]
        response = self.call("DELETE", f"{PASSKEYS}/{only}", token=token)
        self.assertEqual(
            (response.status_code, error_code(response)), (409, "last_passkey")
        )
        # Not found for anything else, with no difference between unknown and foreign.
        other_user = self.make_user("alice")
        other_token = self.login_token("alice")
        _, other_token = self.register(other_token, device())
        foreign = self.call("GET", PASSKEYS, token=other_token).json()["passkeys"][0][
            "id"
        ]
        for target in (uuid.uuid4(), foreign):
            response = self.call("DELETE", f"{PASSKEYS}/{target}", token=token)
            self.assertEqual(
                (response.status_code, error_code(response)), (404, "not_found")
            )
        self.assertEqual(
            self.call("DELETE", f"{PASSKEYS}/not-a-uuid", token=token).status_code, 422
        )
        self.assertIsNotNone(other_user)

    def test_the_errors_of_the_ceremonies_are_typed(self):
        token, authenticator = self.stepped_up()
        begun = self.register_begin(token).json()["options"]
        good = device().create(begun)
        finish = f"{PASSKEYS}/enroll/finish"
        # A rejected answer, then the same answer again (its challenge is spent).
        bad = device().create(begun, origin="https://evil.example")
        response = self.call("POST", finish, token=token, json={"credential": bad})
        self.assertEqual(
            (response.status_code, error_code(response)), (400, "passkey_rejected")
        )
        response = self.call("POST", finish, token=token, json={"credential": good})
        self.assertEqual(
            (response.status_code, error_code(response)), (400, "challenge_invalid")
        )
        # A credential that exists (the same authenticator's id, presented again).
        begun = self.register_begin(token).json()["options"]
        again = device().create(
            begun, credential_id=authenticator.credentials[0].credential_id
        )
        response = self.call("POST", finish, token=token, json={"credential": again})
        self.assertEqual(
            (response.status_code, error_code(response)), (409, "passkey_exists")
        )

    def test_hostile_bodies_are_422_and_nothing_is_stored(self):
        token, authenticator = self.stepped_up()
        begun = self.register_begin(token).json()["options"]
        answer = device().create(begun)
        finish = f"{PASSKEYS}/enroll/finish"

        def tampered(**changes):
            copy = {**answer, "response": dict(answer["response"])}
            for key, value in changes.items():
                if key.startswith("response."):
                    copy["response"][key.removeprefix("response.")] = value
                else:
                    copy[key] = value
            return copy

        credential = answer["rawId"]
        cases = {
            "not an object": "credential",
            "no response": tampered(response=None),
            "another type": tampered(type="password"),
            "id and rawId differ": tampered(id=b64url(b"\x01" * 32)),
            "padding in the id": tampered(
                rawId=credential + "==", id=credential + "=="
            ),
            "a character outside the alphabet": tampered(rawId="+" * 43, id="+" * 43),
            "an id that is too short": tampered(rawId="AAAA", id="AAAA"),
            "an id that is too long": tampered(rawId="A" * 1500, id="A" * 1500),
            "a number as the id": tampered(rawId=5, id=5),
            "no client data": tampered(**{"response.clientDataJSON": None}),
            "client data too big": tampered(**{"response.clientDataJSON": "A" * 4000}),
            "an attestation object too big": tampered(
                **{"response.attestationObject": "A" * 6000}
            ),
            "a non-canonical encoding": tampered(
                **{
                    "response.clientDataJSON": answer["response"]["clientDataJSON"][:-1]
                    + "B"
                }
            ),
            "extensions that are not an object": tampered(clientExtensionResults=[1]),
            "too many extensions": tampered(
                clientExtensionResults={f"k{i}": 1 for i in range(17)}
            ),
        }
        for label, credential_value in cases.items():
            with self.subTest(label):
                response = self.call(
                    "POST", finish, token=token, json={"credential": credential_value}
                )
                self.assertIn(response.status_code, (400, 422), label)
                self.assertIn(
                    error_code(response),
                    ("validation_error", "passkey_rejected"),
                    label,
                )
        # The body itself: unknown fields, a name of the wrong type or size, no body.
        for body in (
            {"credential": answer, "extra": 1},
            {"credential": answer, "name": 5},
            {"credential": answer, "name": "x" * 65},
            {"name": "x"},
            [],
        ):
            with self.subTest(body=repr(body)[:30]):
                response = self.call("POST", finish, token=token, json=body)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(
            len(self.call("GET", PASSKEYS, token=token).json()["passkeys"]), 1
        )

    def test_a_body_over_the_limit_is_refused_before_the_application(self):
        token, _ = self.stepped_up()
        response = self.call(
            "POST",
            f"{PASSKEYS}/enroll/finish",
            token=token,
            content=b'{"credential": {"x": "' + b"A" * 20_000 + b'"}}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(
            (response.status_code, error_code(response)), (413, "payload_too_large")
        )

    def test_a_cross_origin_page_cannot_drive_the_ceremonies(self):
        token, authenticator = self.stepped_up()
        begun = self.register_begin(token).json()["options"]
        answer = device().create(begun)
        for path, body in (
            (f"{PASSKEYS}/enroll/begin", None),
            (f"{PASSKEYS}/enroll/finish", {"credential": answer}),
            (f"{PASSKEYS}/authenticate/begin", None),
            (f"{PASSKEYS}/authenticate/finish", {"credential": answer}),
            (f"{PASSKEYS}/{uuid.uuid4()}", None),
        ):
            with self.subTest(path=path):
                method = "DELETE" if path.rsplit("/", 1)[-1].count("-") == 4 else "POST"
                response = self.call(
                    method, path, token=token, json=body, origin="https://evil.example"
                )
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (403, "forbidden_origin"),
                )
        self.assertEqual(
            len(self.call("GET", PASSKEYS, token=token).json()["passkeys"]), 1
        )
        # The page's own origin is fine (the WebAuthn origin is a different check).
        response = self.call(
            "POST",
            f"{PASSKEYS}/enroll/begin",
            token=token,
            origin="https://localhost",
        )
        self.assertEqual(response.status_code, 200)

    def test_bad_assertions_are_throttled_like_bad_passwords(self):
        token, authenticator = self.enrolled()
        for _ in range(5):
            response, token = self.authenticate(
                token, authenticator, origin="https://evil.example"
            )
            self.assertEqual(
                (response.status_code, error_code(response)),
                (403, "invalid_credentials"),
            )
        begun = self.call("POST", f"{PASSKEYS}/authenticate/begin", token=token)
        answer = authenticator.get(begun.json()["options"])
        response = self.call(
            "POST",
            f"{PASSKEYS}/authenticate/finish",
            token=token,
            json={"credential": answer},
        )
        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)

    def test_the_cookie_of_a_committed_rotation_is_delivered_even_if_the_reply_fails(
        self,
    ):
        token, authenticator = self.enrolled()
        begun = self.call("POST", f"{PASSKEYS}/authenticate/begin", token=token)
        answer = authenticator.get(begun.json()["options"])
        with patch.object(
            self.services.service, "view", side_effect=RuntimeError("secret-detail-023")
        ):
            response = self.call(
                "POST",
                f"{PASSKEYS}/authenticate/finish",
                token=token,
                json={"credential": answer},
            )
        # The step-up committed and the old cookie is dead: the answer carries the
        # new cookie and no error, only a body without the auth state.
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["auth"])
        rotated = self.token_of(response)
        self.assertNotEqual(rotated, token)
        self.assertNotIn("secret-detail-023", response.text)
        self.assertEqual(
            self.call("GET", f"{API}/session", token=rotated).status_code, 200
        )
        self.assertEqual(
            self.call("GET", f"{API}/session", token=token).status_code, 401
        )

    def test_registering_over_http_delivers_the_rotated_cookie_even_if_the_reply_fails(
        self,
    ):
        self.owner()
        token = self.login_token("boss", OWNER_PASSWORD)
        begun = self.register_begin(token).json()["options"]
        answer = device().create(begun)
        with patch.object(
            self.services.service, "view", side_effect=RuntimeError("boom")
        ):
            response = self.call(
                "POST",
                f"{PASSKEYS}/enroll/finish",
                token=token,
                json={"credential": answer},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["session"]["auth"])
        rotated = self.token_of(response)
        self.assertEqual(self.gate_of(rotated), "open")

    def test_the_credential_and_the_challenge_never_appear_in_logs_or_the_store_as_text(
        self,
    ):
        token, authenticator = self.enrolled()
        begun = self.call("POST", f"{PASSKEYS}/authenticate/begin", token=token)
        challenge = begun.json()["options"]["challenge"]
        answer = authenticator.get(
            begun.json()["options"], origin="https://evil.example"
        )
        from .identity_support import LogCapture

        with LogCapture() as logs:
            response = self.call(
                "POST",
                f"{PASSKEYS}/authenticate/finish",
                token=token,
                json={"credential": answer},
            )
        self.assertEqual(response.status_code, 403)
        for secret in (
            answer["response"]["signature"],
            answer["id"],
            challenge,
            "evil.example",
        ):
            self.assertNotIn(secret, logs.text)
            self.assertNotIn(secret, response.text)
        audit = str(self.audit_rows())
        for secret in (answer["response"]["signature"], answer["id"], challenge):
            self.assertNotIn(secret, audit)


@requires_postgres
class NotConfiguredTest(PasskeyHttpTestCase):
    settings_overrides = {}

    def test_the_routes_say_so_and_nothing_is_enforced(self):
        self.owner()
        token = self.login_token("boss", OWNER_PASSWORD)
        state = self.call("GET", f"{API}/session", token=token).json()["auth"][
            "passkey"
        ]
        self.assertEqual((state["available"], state["gate"]), (False, "open"))
        for path in (
            f"{PASSKEYS}/enroll/begin",
            f"{PASSKEYS}/authenticate/begin",
        ):
            with self.subTest(path=path):
                response = self.call("POST", path, token=token)
                self.assertEqual(
                    (response.status_code, error_code(response)),
                    (503, "passkey_unavailable"),
                )
        response = self.call(
            "POST",
            f"{PASSKEYS}/authenticate/finish",
            token=token,
            json={"credential": {}},
        )
        self.assertEqual(
            (response.status_code, error_code(response)), (503, "passkey_unavailable")
        )
        # The list is an empty one, and the rest of the API works (no dead end).
        self.assertEqual(
            self.call("GET", PASSKEYS, token=token).json(), {"passkeys": []}
        )
        self.assertEqual(
            self.call("GET", f"{API}/sessions", token=token).status_code, 200
        )

    def test_the_server_says_loudly_that_the_requirement_is_not_enforced(self):
        from .identity_support import LogCapture

        with LogCapture() as logs:
            import asyncio

            asyncio.run(self.services.start())
        self.assertIn("Passkeys are not configured", logs.application_text)
        self.assertIn("NOT enforced", logs.application_text)


@requires_postgres
class RelyingPartyTest(PasskeyHttpTestCase):
    def test_the_default_application_is_built_without_passkeys(self):
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertFalse(app.state.auth.passkeys.available)

    def test_the_options_use_the_configured_relying_party(self):
        self.owner()
        token = self.login_token("boss", OWNER_PASSWORD)
        options = self.register_begin(token).json()["options"]
        self.assertEqual(options["rp"]["id"], RP_ID)
        self.assertEqual(
            unb64url(options["user"]["id"]).hex(),
            self.scalar(
                "SELECT replace(id::text, '-', '') FROM users WHERE login_name = 'boss'"
            ),
        )
