import unittest
from typing import Annotated

from fastapi import Depends

from paw_backend.app import create_app
from paw_backend.authz import (
    Capability,
    PostgresAuditSink,
    Principal,
    ProjectRole,
    SystemRole,
    UnauthenticatedProvider,
    require_capability,
)

from .authz_support import (
    SECRET,
    FailingSink,
    HangingSink,
    StaticProvider,
    make_test_app,
    principal,
)
from .support import FakeDatabase, make_client, make_settings

UNAUTHORIZED = {
    "code": "unauthorized",
    "message": "Authentication required",
}
FORBIDDEN = {"code": "forbidden", "message": "Permission denied"}


def error_without_request_id(response) -> dict:
    body = dict(response.json()["error"])
    request_id = body.pop("request_id")
    assert request_id == response.headers["X-Request-ID"]
    return body


class UnauthenticatedTest(unittest.TestCase):
    def test_create_app_installs_a_provider_that_authenticates_nobody(self):
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsInstance(app.state.principal_provider, UnauthenticatedProvider)

    def test_protected_endpoints_answer_401_until_authentication_exists(self):
        app, sink, calls = make_test_app(default_provider=True)
        with make_client(app) as client:
            for path in (
                "/test/admin",
                "/test/chat",
                "/test/projects/p1/tasks",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(error_without_request_id(response), UNAUTHORIZED)
        self.assertEqual(calls, [])
        self.assertEqual(
            [(e.decision, e.reason, e.actor_id) for e in sink.events],
            [("deny", "unauthenticated", None)] * 3,
        )

    def test_the_401_is_audited_with_the_request_id(self):
        app, sink, _ = make_test_app(default_provider=True)
        with make_client(app) as client:
            response = client.get("/test/admin", headers={"X-Request-ID": "req-401"})
        self.assertEqual(response.headers["X-Request-ID"], "req-401")
        self.assertEqual(response.json()["error"]["request_id"], "req-401")
        self.assertEqual(sink.events[0].request_id, "req-401")
        self.assertEqual(sink.events[0].action, "admin.users.manage")

    def test_the_default_wiring_answers_401_even_with_no_database_to_audit_to(self):
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsInstance(app.state.authorizer._sink, PostgresAuditSink)

        @app.get(
            "/test/admin",
            dependencies=[Depends(require_capability(Capability.ADMIN_USERS_MANAGE))],
        )
        async def route() -> dict[str, str]:
            return {}

        with self.assertLogs("paw_backend.authz.authorizer", level="WARNING") as logs:
            with make_client(app) as client:
                response = client.get("/test/admin")
        self.assertEqual(response.status_code, 401)
        self.assertIn("DatabaseNotConfiguredError", "\n".join(logs.output))

    def test_a_client_cannot_assert_its_own_identity_or_role(self):
        app, sink, calls = make_test_app(default_provider=True)
        claims = {
            "X-User-Role": "owner",
            "X-Role": "admin",
            "X-User-Id": "u1",
            "Authorization": "Bearer owner",
            "Cookie": "role=owner; session=admin",
        }
        with make_client(app) as client:
            response = client.get("/test/admin?role=owner&user=u1", headers=claims)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])
        self.assertEqual(sink.events[0].actor_id, None)


class ForbiddenTest(unittest.TestCase):
    def test_an_authenticated_user_without_the_capability_gets_403(self):
        who = principal(SystemRole.USER, user_id="u1")
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            response = client.get("/test/admin", headers={"X-User-Role": "owner"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(error_without_request_id(response), FORBIDDEN)
        self.assertEqual(calls, [])
        (event,) = sink.events
        self.assertEqual(
            (event.actor_id, event.actor_role, event.decision, event.reason),
            ("u1", "user", "deny", "capability_not_granted"),
        )

    def test_401_and_403_are_different_answers_to_different_questions(self):
        anonymous, _, _ = make_test_app(None)
        user, _, _ = make_test_app(principal(SystemRole.USER))
        with make_client(anonymous) as client:
            self.assertEqual(client.get("/test/admin").status_code, 401)
        with make_client(user) as client:
            self.assertEqual(client.get("/test/admin").status_code, 403)

    def test_the_403_body_does_not_reveal_which_rule_denied(self):
        bodies = set()
        cases = {
            "not a member": principal(SystemRole.USER),
            "viewer": principal(SystemRole.USER, p1=ProjectRole.VIEWER),
            "other project": principal(SystemRole.OWNER, p2=ProjectRole.MANAGER),
        }
        for name, who in cases.items():
            app, sink, _ = make_test_app(who)
            with make_client(app) as client:
                response = client.get("/test/projects/p1/tasks")
            with self.subTest(case=name):
                self.assertEqual(response.status_code, 403)
                bodies.add(tuple(sorted(error_without_request_id(response).items())))
                self.assertEqual(sink.events[0].decision, "deny")
        self.assertEqual(bodies, {tuple(sorted(FORBIDDEN.items()))})

    def test_project_roles_are_isolated_per_project(self):
        who = principal(SystemRole.USER, p1=ProjectRole.MANAGER)
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            self.assertEqual(client.get("/test/projects/p1/tasks").status_code, 200)
            self.assertEqual(client.get("/test/projects/p2/tasks").status_code, 403)
        self.assertEqual(calls, ["task:p1"])
        self.assertEqual(
            [(e.project_id, e.reason) for e in sink.events],
            [("p1", "granted_by_project_role"), ("p2", "not_project_member")],
        )

    def test_a_viewer_may_not_run_tasks_but_a_contributor_may(self):
        for role, status in (
            (ProjectRole.VIEWER, 403),
            (ProjectRole.CONTRIBUTOR, 200),
            (ProjectRole.MANAGER, 200),
        ):
            app, _, _ = make_test_app(principal(SystemRole.USER, p1=role))
            with self.subTest(role=role.value):
                with make_client(app) as client:
                    self.assertEqual(
                        client.get("/test/projects/p1/tasks").status_code, status
                    )

    def test_a_malformed_resource_id_is_denied_not_an_error(self):
        app, sink, calls = make_test_app(
            principal(SystemRole.USER, p1=ProjectRole.MANAGER)
        )
        with make_client(app) as client:
            for project_id in ("a%20b", "x" * 200, "a%0Ab"):
                with self.subTest(project_id=project_id):
                    response = client.get(f"/test/projects/{project_id}/tasks")
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(calls, [])
        self.assertEqual(
            {(e.resource_kind, e.resource_id, e.reason) for e in sink.events},
            {("unknown", None, "invalid_resource")},
        )

    def test_a_malformed_resource_id_from_nobody_is_still_401(self):
        app, _, _ = make_test_app(None)
        with make_client(app) as client:
            self.assertEqual(client.get("/test/projects/a%20b/tasks").status_code, 401)


class AllowedTest(unittest.TestCase):
    def test_an_allowed_request_reaches_the_handler_and_is_audited(self):
        who = principal(SystemRole.ADMIN, user_id="admin-1")
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            response = client.get("/test/admin", headers={"X-Request-ID": "req-ok"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": "admin"})
        self.assertEqual(calls, ["admin"])
        (event,) = sink.events
        self.assertEqual(
            (event.actor_id, event.decision, event.reason, event.request_id),
            ("admin-1", "allow", "granted_by_system_role", "req-ok"),
        )

    def test_the_dependency_returns_the_principal_to_the_endpoint(self):
        who = principal(SystemRole.USER, user_id="u42")
        app, _, _ = make_test_app(who)

        @app.get("/test/whoami")
        async def whoami(
            me: Annotated[
                Principal, Depends(require_capability(Capability.SHARED_MEMORY_READ))
            ],
        ) -> dict[str, str]:
            return {"user": me.user_id}

        with make_client(app) as client:
            self.assertEqual(client.get("/test/whoami").json(), {"user": "u42"})


class AuditOutageTest(unittest.TestCase):
    def test_a_privileged_endpoint_answers_503_when_the_audit_write_fails(self):
        app, _, calls = make_test_app(principal(SystemRole.OWNER), sink=FailingSink())
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            with make_client(app) as client:
                response = client.get("/test/admin")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            error_without_request_id(response),
            {
                "code": "service_unavailable",
                "message": "Service temporarily unavailable",
            },
        )
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(calls, [])

    def test_an_ordinary_endpoint_keeps_working_when_the_audit_write_fails(self):
        app, _, calls = make_test_app(principal(SystemRole.USER), sink=FailingSink())
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            with make_client(app) as client:
                response = client.get("/test/chat")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["chat"])


class AuditTimeoutTest(unittest.TestCase):
    def test_the_audit_write_is_bounded_by_the_database_timeout_setting(self):
        app, _, calls = make_test_app(
            principal(SystemRole.OWNER),
            sink=HangingSink(),
            database_timeout_seconds=0.05,
        )
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            with make_client(app) as client:
                response = client.get("/test/admin")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [])


class OpenApiTest(unittest.TestCase):
    def test_protected_routes_appear_in_the_schema_without_extra_parameters(self):
        app, _, _ = make_test_app(None)
        with make_client(app) as client:
            paths = client.get("/api/v1/openapi.json").json()["paths"]
        self.assertEqual(
            [
                p["name"]
                for p in paths["/test/projects/{project_id}/tasks"]["get"]["parameters"]
            ],
            ["project_id"],
        )
        self.assertNotIn("parameters", paths["/test/admin"]["get"])


class ProviderSeamTest(unittest.TestCase):
    def test_the_provider_can_be_replaced_after_the_app_is_built(self):
        app, _, _ = make_test_app(None)
        with make_client(app) as client:
            self.assertEqual(client.get("/test/chat").status_code, 401)
            app.state.principal_provider = StaticProvider(principal(SystemRole.USER))
            self.assertEqual(client.get("/test/chat").status_code, 200)


if __name__ == "__main__":
    unittest.main()
