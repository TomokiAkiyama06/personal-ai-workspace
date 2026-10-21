"""Attaching a stored credential to a shared connection over HTTP (issue #277,
Decision 0087, 2): real PostgreSQL, the real file store in a temporary directory.

The service's own rules are tested in ``test_connections_credential_store``;
here the routes: ``admin.config.manage``, the Passkey Step-up of a change, a
handle the store does not hold (or holds for the other kind), the
compare-and-swap of a replacement, the audit rows, and that neither a value
nor a handle is ever in an answer, the audit trail or the log.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import text

from paw_backend.connections import ConnectionKind, FileSecretStore
from paw_backend.connections.wiring import build_connection_service

from .auth_http_support import HttpTestCase, error_code, requires_postgres

CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE
VALUE = "paw-attach-canary-" + "5c1e" + "-first-value"
OTHER = "paw-attach-canary-" + "a07f" + "-second-value"
KEY = bytes(range(32))
LIST = "/api/v1/admin/connections"


class ConnectionsHttpCase(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.chmod(root, 0o700)
        self.store = FileSecretStore(root, KEY)
        self.use_store(self.store)
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE shared_connections"))
        self.owner_id = self.make_user("owner-one", role="owner")
        self.admin_id = self.make_user("admin-one", role="admin")
        self.user_id = self.make_user("alice")
        self.owner = self.login_token("owner-one")
        self.admin = self.login_token("admin-one")
        self.user = self.login_token("alice")

    def use_store(self, store: FileSecretStore | None) -> None:
        self.app.state.connections = build_connection_service(
            self.settings,
            self.database,
            self.app.state.authorizer,
            self.services.audit_sink,
            secret_store=store,
        )
        self.app.state.secret_store_configured = store is not None

    def put(self, kind: ConnectionKind, value: str = VALUE) -> str:
        return self.client.portal.call(self.store.put, kind, value)

    def attach(self, kind: str, handle: str, token: str | None = None):
        return self.call(
            "POST",
            f"{LIST}/{kind}",
            token=token or self.admin,
            json={"secret_handle": handle},
        )

    def replace(self, kind: str, handle: str, revision: str, token=None):
        return self.call(
            "PUT",
            f"{LIST}/{kind}/credential",
            token=token or self.admin,
            json={"secret_handle": handle, "expected_revision": revision},
        )

    def status_of(self, kind: str, token: str | None = None) -> dict:
        response = self.call("GET", LIST, token=token or self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        (found,) = [c for c in response.json()["connections"] if c["kind"] == kind]
        return found

    def attached(self) -> list:
        return self.rows("SELECT kind, secret_handle FROM shared_connections")


@requires_postgres
class AttachTest(ConnectionsHttpCase):
    def test_attach_view_replace_and_detach(self):
        self.fake_passkey_step_up(self.admin)
        first = self.put(CODEX)
        second = self.put(CODEX, OTHER)

        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("paw_backend").debug("probe")  # at least one line
            created = self.attach("codex", first)
            self.assertEqual(created.status_code, 201, created.text)
            body = created.json()
            self.assertEqual(
                (body["kind"], body["configured"], body["status"], body["enabled"]),
                ("codex", True, "unavailable", True),
            )
            self.assertEqual(body["credential"], {"stored": True, "version": 1})
            self.assertFalse(body["available"])

            listed = self.call("GET", LIST, token=self.admin)
            self.assertEqual(
                [(c["kind"], c["configured"]) for c in listed.json()["connections"]],
                [("codex", True), ("claude", False)],
            )

            replaced = self.replace("codex", second, body["revision"])
            self.assertEqual(replaced.status_code, 200, replaced.text)
            self.assertNotEqual(replaced.json()["revision"], body["revision"])
            self.assertEqual(self.attached(), [("codex", second)])

            removed = self.call("DELETE", f"{LIST}/codex", token=self.admin)
            self.assertEqual(removed.status_code, 204, removed.text)
            self.assertEqual(self.attached(), [])

        summary = self.audit_summary()
        for action in (
            "connection.connect",
            "connection.replace",
            "connection.disconnect",
        ):
            self.assertEqual(summary[(action, "allow", "succeeded")], 1, action)
        answers = created.text + listed.text + replaced.text
        stored = self.everything_stored() + "\n".join(logs.output)
        for secret in (first, second, VALUE, OTHER):
            self.assertNotIn(secret, answers)
            self.assertNotIn(secret, stored)

    def test_a_change_needs_admin_config_manage(self):
        self.fake_passkey_step_up(self.user)
        stored = self.put(CODEX)

        for response in (
            self.call("GET", LIST, token=self.user),
            self.attach("codex", stored, self.user),
            self.replace("codex", stored, "0" * 32, self.user),
            self.call("DELETE", f"{LIST}/codex", token=self.user),
        ):
            self.assertEqual(
                (response.status_code, error_code(response)), (403, "forbidden")
            )
        self.assertEqual(self.attached(), [])
        nobody = self.call("GET", LIST)
        self.assertEqual(nobody.status_code, 401)

    def test_an_owner_may_too(self):
        self.fake_passkey_step_up(self.owner)
        response = self.attach("claude", self.put(CLAUDE), self.owner)
        self.assertEqual(response.status_code, 201, response.text)

    def test_a_change_needs_a_recent_passkey_step_up(self):
        stored = self.put(CODEX)

        attach = self.attach("codex", stored)
        replace = self.replace("codex", stored, "0" * 32)
        detach = self.call("DELETE", f"{LIST}/codex", token=self.admin)

        for response in (attach, replace, detach):
            self.assertEqual(
                (response.status_code, error_code(response)), (403, "step_up_required")
            )
        self.assertEqual(self.attached(), [])
        summary = self.audit_summary()
        for action in (
            "connection.connect",
            "connection.replace",
            "connection.disconnect",
        ):
            self.assertEqual(summary[(action, "deny", "step_up_required")], 1, action)

    def test_an_unknown_or_other_kinds_handle_is_refused(self):
        self.fake_passkey_step_up(self.admin)
        claude = self.put(CLAUDE)
        unknown = "cred_" + "0" * 32

        for handle in (unknown, claude):
            response = self.attach("codex", handle)
            self.assertEqual(
                (response.status_code, error_code(response)),
                (422, "credential_not_stored"),
            )
        not_a_handle = self.attach("codex", VALUE)
        self.assertEqual(
            (not_a_handle.status_code, error_code(not_a_handle)),
            (422, "validation_error"),
        )
        self.assertNotIn(VALUE, not_a_handle.text)
        self.assertEqual(self.attached(), [])

        codex = self.put(CODEX)
        revision = self.attach("codex", codex).json()["revision"]
        for handle in (unknown, claude):
            response = self.replace("codex", handle, revision)
            self.assertEqual(
                (response.status_code, error_code(response)),
                (422, "credential_not_stored"),
            )
        self.assertEqual(self.attached(), [("codex", codex)])

    def test_a_second_attach_is_a_conflict(self):
        self.fake_passkey_step_up(self.admin)
        first = self.put(CODEX)
        self.attach("codex", first)

        again = self.attach("codex", self.put(CODEX, OTHER))

        self.assertEqual(
            (again.status_code, error_code(again)), (409, "connection_exists")
        )
        self.assertEqual(self.attached(), [("codex", first)])

    def test_a_replacement_is_a_compare_and_swap(self):
        self.fake_passkey_step_up(self.admin)
        self.fake_passkey_step_up(self.owner)
        first, second, third = (self.put(CODEX, VALUE + str(i)) for i in range(3))
        seen = self.attach("codex", first).json()["revision"]
        # The Owner replaces it first, from the same status.
        self.assertEqual(
            self.replace("codex", second, seen, self.owner).status_code, 200
        )

        late = self.replace("codex", third, seen)

        self.assertEqual(
            (late.status_code, error_code(late)), (409, "stale_connection_credential")
        )
        self.assertEqual(self.attached(), [("codex", second)])
        self.assertEqual(
            self.audit_summary()[
                ("connection.replace", "deny", "stale_connection_credential")
            ],
            1,
        )
        fresh = self.status_of("codex")["revision"]
        self.assertEqual(self.replace("codex", third, fresh).status_code, 200)

    def test_a_missing_connection_is_not_found(self):
        self.fake_passkey_step_up(self.admin)
        replace = self.replace("claude", self.put(CLAUDE), "0" * 32)
        detach = self.call("DELETE", f"{LIST}/claude", token=self.admin)
        for response in (replace, detach):
            self.assertEqual(
                (response.status_code, error_code(response)), (404, "not_found")
            )

    def test_the_status_shows_the_version_and_a_lost_credential(self):
        self.fake_passkey_step_up(self.admin)
        stored = self.put(CLAUDE)
        self.attach("claude", stored)
        self.client.portal.call(
            self.app.state.connections.rotate_credential, CLAUDE, stored, 1, OTHER
        )
        self.assertEqual(
            self.status_of("claude")["credential"], {"stored": True, "version": 2}
        )
        shutil.rmtree(self.store._root / stored)
        self.assertEqual(
            self.status_of("claude")["credential"], {"stored": False, "version": None}
        )

    def test_the_request_is_validated(self):
        self.fake_passkey_step_up(self.admin)
        stored = self.put(CODEX)
        for body in ({}, {"secret_handle": 1}, {"secret_handle": stored, "x": 1}):
            response = self.call("POST", f"{LIST}/codex", token=self.admin, json=body)
            self.assertEqual(response.status_code, 422, body)
        for body in ({"secret_handle": stored}, {"expected_revision": "a"}):
            response = self.call(
                "PUT", f"{LIST}/codex/credential", token=self.admin, json=body
            )
            self.assertEqual(response.status_code, 422, body)
        bad_kind = self.attach("gemini", stored)
        self.assertEqual(bad_kind.status_code, 422)
        self.assertEqual(self.attached(), [])

    def test_a_cross_origin_change_is_refused(self):
        self.fake_passkey_step_up(self.admin)
        response = self.call(
            "POST",
            f"{LIST}/codex",
            token=self.admin,
            origin="https://evil.example",
            json={"secret_handle": self.put(CODEX)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.attached(), [])

    def test_without_a_secret_store_nothing_is_attached(self):
        self.fake_passkey_step_up(self.admin)
        self.use_store(None)
        handle = "cred_" + "1" * 32

        attach = self.attach("codex", handle)
        replace = self.replace("codex", handle, "0" * 32)

        for response in (attach, replace):
            self.assertEqual(
                (response.status_code, error_code(response)),
                (503, "secret_store_not_configured"),
            )
        self.assertEqual(self.attached(), [])
        self.assertEqual(self.status_of("codex"), {
            "kind": "codex", "configured": False, "status": None, "enabled": None,
            "available": None, "checked_at": None, "created_at": None,
            "updated_at": None, "credential": None, "revision": None,
        })  # fmt: skip
