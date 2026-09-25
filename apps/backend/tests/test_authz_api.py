import time
import unittest
import uuid
from typing import Annotated

from fastapi import Depends
from starlette.websockets import WebSocketDisconnect

from paw_backend.app import create_app
from paw_backend.auth.principals import (
    DatabasePrincipalDirectory,
    SessionPrincipalProvider,
)
from paw_backend.authz import (
    Capability,
    PostgresAuditSink,
    Principal,
    ProjectRole,
    ProjectState,
    Resource,
    SystemRole,
    UnauthenticatedProvider,
    require_capability,
)

from .authz_support import (
    P1,
    P2,
    P3,
    REPO,
    REPO2,
    REPO3,
    REPO4,
    SECRET,
    U1,
    FailingSink,
    HangingSink,
    StaticProvider,
    make_test_app,
    principal,
)
from .support import FakeDatabase, make_client, make_settings

LOGGER = "paw_backend.authz.authorizer"
DEPS_LOGGER = "paw_backend.authz.deps"
# A UUID with hex letters, so that upper-casing it changes it.
LETTERS = uuid.UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
UNAUTHORIZED = {"code": "unauthorized", "message": "Authentication required"}
FORBIDDEN = {"code": "forbidden", "message": "Permission denied"}
UNAVAILABLE = {
    "code": "service_unavailable",
    "message": "Service temporarily unavailable",
}
WS_URL = "ws://localhost/test/ws"
WS_ADMIN_URL = "ws://localhost/test/ws-admin"


def error_without_request_id(response) -> dict:
    body = dict(response.json()["error"])
    request_id = body.pop("request_id")
    assert request_id == response.headers["X-Request-ID"]
    return body


class UnauthenticatedTest(unittest.TestCase):
    def test_create_app_installs_the_session_provider_and_the_directory(self):
        # PAW-022 replaced the placeholder that authenticated nobody: the cookie
        # of a session is what authenticates (tests/test_auth_http.py).
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsInstance(app.state.principal_provider, SessionPrincipalProvider)
        self.assertNotIsInstance(app.state.principal_provider, UnauthenticatedProvider)
        self.assertIsInstance(
            app.state.authorizer._directory, DatabasePrincipalDirectory
        )

    def test_protected_endpoints_answer_401_until_authentication_exists(self):
        app, sink, calls = make_test_app(default_provider=True)
        paths = (
            "/test/admin",
            "/test/shared-memory",
            f"/test/projects/{P1}/tasks",
            f"/test/stored/{P1}/tasks",
        )
        with self.assertLogs(LOGGER, level="INFO") as logs:
            with make_client(app) as client:
                for path in paths:
                    with self.subTest(path=path):
                        response = client.get(path)
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(
                            error_without_request_id(response), UNAUTHORIZED
                        )
        self.assertEqual(calls, [])
        # Unauthenticated denials are logged, never written to the audit table.
        self.assertEqual(sink.events, [])
        self.assertEqual(len(logs.output), len(paths))

    def test_the_401_carries_the_request_id_and_it_is_logged(self):
        app, _, _ = make_test_app(default_provider=True)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            with make_client(app) as client:
                response = client.get(
                    "/test/admin", headers={"X-Request-ID": "req-401"}
                )
        self.assertEqual(response.headers["X-Request-ID"], "req-401")
        self.assertEqual(response.json()["error"]["request_id"], "req-401")
        (line,) = logs.output
        self.assertIn("action=admin.users.manage", line)
        self.assertTrue(line.endswith("client_request_id=req-401"))

    def test_the_default_wiring_answers_401_without_touching_the_database(self):
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsInstance(app.state.authorizer._sink, PostgresAuditSink)

        @app.get(
            "/test/admin",
            dependencies=[Depends(require_capability(Capability.ADMIN_USERS_MANAGE))],
        )
        async def route() -> dict[str, str]:
            return {}

        with self.assertLogs(LOGGER, level="INFO") as logs:
            with make_client(app) as client:
                response = client.get("/test/admin")
        self.assertEqual(response.status_code, 401)
        # No audit write was even attempted (the database is not configured).
        self.assertTrue(all(line.startswith("INFO:") for line in logs.output))

    def test_a_client_cannot_assert_its_own_identity_or_role(self):
        app, sink, calls = make_test_app(default_provider=True)
        claims = {
            "X-User-Role": "owner",
            "X-Role": "admin",
            "X-User-Id": str(U1),
            "Authorization": "Bearer owner",
            "Cookie": "role=owner; session=admin",
        }
        with self.assertLogs(LOGGER, level="INFO"):
            with make_client(app) as client:
                response = client.get("/test/admin?role=owner&user=x", headers=claims)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])
        self.assertEqual(sink.events, [])


class ForbiddenTest(unittest.TestCase):
    def test_an_authenticated_user_without_the_capability_gets_403(self):
        who = principal(SystemRole.USER, user_id=U1)
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            response = client.get("/test/admin", headers={"X-User-Role": "owner"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(error_without_request_id(response), FORBIDDEN)
        self.assertEqual(calls, [])
        (event,) = sink.events
        self.assertEqual(
            (event.actor_id, event.actor_role, event.decision, event.reason),
            (U1, "user", "deny", "capability_not_granted"),
        )

    def test_401_and_403_are_different_answers_to_different_questions(self):
        anonymous, _, _ = make_test_app(None)
        user, _, _ = make_test_app(principal(SystemRole.USER))
        with self.assertLogs(LOGGER, level="INFO"):
            with make_client(anonymous) as client:
                self.assertEqual(client.get("/test/admin").status_code, 401)
        with make_client(user) as client:
            self.assertEqual(client.get("/test/admin").status_code, 403)

    def test_the_403_body_does_not_reveal_which_rule_denied(self):
        bodies = set()
        cases = {
            "not a member": principal(SystemRole.USER),
            "viewer": principal(SystemRole.USER, projects={P1: ProjectRole.VIEWER}),
            "other project": principal(
                SystemRole.OWNER, projects={P2: ProjectRole.MANAGER}
            ),
        }
        for name, who in cases.items():
            app, sink, _ = make_test_app(who)
            with make_client(app) as client:
                response = client.get(f"/test/projects/{P1}/tasks")
            with self.subTest(case=name):
                self.assertEqual(response.status_code, 403)
                bodies.add(tuple(sorted(error_without_request_id(response).items())))
                self.assertEqual(sink.events[0].decision, "deny")
        self.assertEqual(bodies, {tuple(sorted(FORBIDDEN.items()))})

    def test_project_roles_are_isolated_per_project(self):
        who = principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            self.assertEqual(client.get(f"/test/projects/{P1}/tasks").status_code, 200)
            self.assertEqual(client.get(f"/test/projects/{P2}/tasks").status_code, 403)
        self.assertEqual(calls, [f"task:{P1}"])
        self.assertEqual(
            [(e.project_id, e.reason) for e in sink.events],
            [(P1, "granted_by_project_role"), (P2, "not_project_member")],
        )

    def test_a_viewer_may_not_run_tasks_but_a_contributor_may(self):
        for role, status in (
            (ProjectRole.VIEWER, 403),
            (ProjectRole.CONTRIBUTOR, 200),
            (ProjectRole.MANAGER, 200),
        ):
            app, _, _ = make_test_app(principal(SystemRole.USER, projects={P1: role}))
            with self.subTest(role=role.value):
                with make_client(app) as client:
                    response = client.get(f"/test/projects/{P1}/tasks")
                    self.assertEqual(response.status_code, status)

    def test_a_malformed_resource_id_is_denied_not_an_error(self):
        app, sink, calls = make_test_app(
            principal(SystemRole.USER, projects={LETTERS: ProjectRole.MANAGER})
        )
        # The canonical spelling is allowed for this member ...
        with make_client(app) as client:
            self.assertEqual(
                client.get(f"/test/projects/{LETTERS}/tasks").status_code, 200
            )
        calls.clear()
        sink.events.clear()
        malformed = (
            "a%20b",
            "x" * 200,
            "a%0Ab",
            "not-a-uuid",
            str(LETTERS).upper(),  # ... but only the canonical (lower-case) one
            str(LETTERS).replace("-", ""),
        )
        with make_client(app) as client:
            for project_id in malformed:
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
        with self.assertLogs(LOGGER, level="INFO"):
            with make_client(app) as client:
                response = client.get("/test/projects/a%20b/tasks")
        self.assertEqual(response.status_code, 401)


class ResolverTest(unittest.TestCase):
    def test_an_async_resolver_is_awaited(self):
        who = principal(SystemRole.USER, projects={P1: ProjectRole.CONTRIBUTOR})
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            response = client.get(f"/test/stored/{P1}/tasks")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [f"stored:{P1}"])
        self.assertEqual(sink.events[0].reason, "granted_by_project_role")

    def test_the_stored_project_state_decides_through_an_async_resolver(self):
        who = principal(
            SystemRole.USER,
            projects={p: ProjectRole.MANAGER for p in (P1, P2, P3)},
        )
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            statuses = [
                client.get(f"/test/stored/{p}/tasks").status_code for p in (P1, P2, P3)
            ]
        # P2 is archived (read-only) and P3 is pending deletion.
        self.assertEqual(statuses, [200, 403, 403])
        self.assertEqual(
            [e.reason for e in sink.events],
            [
                "granted_by_project_role",
                "project_state_forbids",
                "project_state_forbids",
            ],
        )

    def test_a_resolver_that_raises_is_denied_and_audited_not_a_500(self):
        who = principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})
        app, sink, calls = make_test_app(who)
        with self.assertLogs(DEPS_LOGGER, level="WARNING") as logs:
            with make_client(app) as client:
                response = client.get(f"/test/broken/{P1}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(error_without_request_id(response), FORBIDDEN)
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertEqual(calls, [])
        (event,) = sink.events
        self.assertEqual((event.decision, event.reason), ("deny", "invalid_resource"))

    def test_nobody_is_refused_before_any_resource_is_resolved(self):
        # A resolver of a project or repository route loads state from storage:
        # an anonymous request must not be able to force (or stall on) that work.
        resolved: list[str] = []

        async def counting(connection):
            resolved.append(connection.path_params["project_id"])
            return Resource.project(
                connection.path_params["project_id"], ProjectState.ACTIVE
            )

        def add_route(app):
            @app.get(
                "/test/counted/{project_id}",
                dependencies=[
                    Depends(require_capability(Capability.PROJECT_TASK_RUN, counting))
                ],
            )
            async def counted(project_id: str) -> dict[str, str]:
                return {"ok": project_id}

        nobody, sink, _ = make_test_app(None)
        add_route(nobody)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            with make_client(nobody) as client:
                response = client.get(f"/test/counted/{P1}")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(error_without_request_id(response), UNAUTHORIZED)
        self.assertEqual(resolved, [])  # the resolver did not run
        self.assertEqual(sink.events, [])
        self.assertIn("reason=unauthenticated", "\n".join(logs.output))
        self.assertIn("resource_kind=unknown", "\n".join(logs.output))

        # Control: for an authenticated user the same resolver does run.
        member = principal(SystemRole.USER, projects={P1: ProjectRole.CONTRIBUTOR})
        user, user_sink, _ = make_test_app(member)
        add_route(user)
        with make_client(user) as client:
            self.assertEqual(client.get(f"/test/counted/{P1}").status_code, 200)
        self.assertEqual(resolved, [str(P1)])
        self.assertEqual(user_sink.events[0].resource_kind, "project")

    def test_the_capability_is_checked_when_the_dependency_is_built(self):
        for bad in ("admin.users.manage", "chat.use", None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    require_capability(bad)


class RepositoryRouteTest(unittest.TestCase):
    """A repository route resolves the repo and its ACL (async) for the policy."""

    def get(self, who, project_id, repo_id, path="files"):
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            response = client.get(f"/test/projects/{project_id}/repos/{repo_id}/{path}")
        return response, sink, calls

    def test_an_inherit_repository_follows_the_project_role(self):
        for role, status in (
            (ProjectRole.VIEWER, 403),
            (ProjectRole.CONTRIBUTOR, 200),
            (ProjectRole.MANAGER, 200),
        ):
            with self.subTest(role=role.value):
                who = principal(SystemRole.USER, projects={P1: role})
                response, sink, _ = self.get(who, P1, REPO)
                self.assertEqual(response.status_code, status)
                (event,) = sink.events
                self.assertEqual(
                    (event.repo_id, event.repo_acl, event.project_id),
                    (REPO, "inherit", P1),
                )

    def test_a_non_member_is_refused_on_an_inherit_repository(self):
        response, sink, calls = self.get(principal(SystemRole.USER), P1, REPO)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(error_without_request_id(response), FORBIDDEN)
        self.assertEqual(sink.events[0].reason, "not_project_member")
        self.assertEqual(calls, [])

    def test_a_read_only_override_stops_even_a_manager_from_writing(self):
        manager = principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})
        response, sink, calls = self.get(manager, P1, REPO2)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(calls, [])
        (event,) = sink.events
        self.assertEqual(
            (event.reason, event.repo_id, event.repo_acl),
            ("repo_acl_forbids", REPO2, "override"),
        )
        # An "access denied" repository refuses everything.
        response, sink, _ = self.get(manager, P1, REPO4)
        self.assertEqual(response.status_code, 403)

    def test_a_repository_of_another_project_cannot_be_reached_through_this_one(self):
        # REPO3 is stored under P2; the user is a Manager of P1 only.
        manager = principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})
        response, sink, calls = self.get(manager, P1, REPO3)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(sink.events[0].reason, "repo_acl_mismatch")
        self.assertEqual(calls, [])

    def test_a_resolver_that_forgot_the_acl_is_refused_not_treated_as_inherit(self):
        manager = principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})
        app, sink, calls = make_test_app(manager)
        with make_client(app) as client:
            response = client.get(f"/test/forgetful/{P1}/{REPO}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(sink.events[0].reason, "repo_acl_unresolved")
        self.assertEqual(calls, [])


class AllowedTest(unittest.TestCase):
    def test_an_allowed_request_reaches_the_handler_and_is_audited(self):
        who = principal(SystemRole.ADMIN, user_id=U1)
        app, sink, calls = make_test_app(who)
        with make_client(app) as client:
            response = client.get("/test/admin", headers={"X-Request-ID": "req-ok"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": "admin"})
        self.assertEqual(calls, ["admin"])
        (event,) = sink.events
        self.assertEqual(
            (event.actor_id, event.decision, event.reason, event.client_request_id),
            (U1, "allow", "granted_by_system_role", "req-ok"),
        )

    def test_an_allowed_read_is_not_written_to_the_audit_table(self):
        app, sink, calls = make_test_app(principal(SystemRole.USER))
        with make_client(app) as client:
            self.assertEqual(client.get("/test/shared-memory").status_code, 200)
        self.assertEqual(calls, ["shared"])
        self.assertEqual(sink.events, [])

    def test_the_dependency_returns_the_principal_to_the_endpoint(self):
        who = principal(SystemRole.USER, user_id=U1)
        app, _, _ = make_test_app(who)

        @app.get("/test/whoami")
        async def whoami(
            me: Annotated[
                Principal, Depends(require_capability(Capability.SHARED_MEMORY_READ))
            ],
        ) -> dict[str, str]:
            return {"user": str(me.user_id)}

        with make_client(app) as client:
            self.assertEqual(client.get("/test/whoami").json(), {"user": str(U1)})


class CorrelationTest(unittest.TestCase):
    def test_decisions_of_one_request_share_a_server_generated_correlation_id(self):
        app, sink, _ = make_test_app(principal(SystemRole.ADMIN))
        with make_client(app) as client:
            client.get("/test/two", headers={"X-Request-ID": "forged-id"})
            client.get("/test/two", headers={"X-Request-ID": "forged-id"})
        self.assertEqual(len(sink.events), 4)
        first, second, third, fourth = sink.events
        self.assertEqual(first.correlation_id, second.correlation_id)
        self.assertEqual(third.correlation_id, fourth.correlation_id)
        # A client repeating (or forging) X-Request-ID cannot merge two requests.
        self.assertNotEqual(first.correlation_id, third.correlation_id)
        for event in sink.events:
            self.assertEqual(event.client_request_id, "forged-id")
            self.assertIsInstance(event.correlation_id, uuid.UUID)
            self.assertNotEqual(str(event.correlation_id), "forged-id")


class AuditOutageTest(unittest.TestCase):
    def test_a_privileged_endpoint_answers_503_when_the_audit_write_fails(self):
        app, _, calls = make_test_app(principal(SystemRole.OWNER), sink=FailingSink())
        with self.assertLogs(LOGGER, level="ERROR"):
            with make_client(app) as client:
                response = client.get("/test/admin")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(error_without_request_id(response), UNAVAILABLE)
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(calls, [])

    def test_a_side_effect_endpoint_does_not_run_without_an_audit_record(self):
        who = principal(SystemRole.USER, projects={P1: ProjectRole.CONTRIBUTOR})
        app, _, calls = make_test_app(who, sink=FailingSink())
        with self.assertLogs(LOGGER, level="ERROR"):
            with make_client(app) as client:
                response = client.get(f"/test/projects/{P1}/tasks")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [])

    def test_a_read_only_endpoint_keeps_working_when_the_audit_write_fails(self):
        sink = FailingSink()
        app, _, calls = make_test_app(principal(SystemRole.USER), sink=sink)
        with make_client(app) as client:
            response = client.get("/test/shared-memory")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["shared"])
        self.assertEqual(sink.attempts, 0)

    def test_the_audit_write_is_bounded_by_the_database_timeout_setting(self):
        app, _, calls = make_test_app(
            principal(SystemRole.OWNER),
            sink=HangingSink(),
            database_timeout_seconds=0.05,
        )
        started = time.monotonic()
        with self.assertLogs(LOGGER, level="ERROR"):
            with make_client(app) as client:
                response = client.get("/test/admin")
        # HangingSink sleeps for 5 seconds; without the timeout this takes that long.
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [])


class WebSocketTest(unittest.TestCase):
    def test_an_anonymous_websocket_is_refused_before_it_is_accepted(self):
        app, sink, _ = make_test_app(None)
        with self.assertLogs(LOGGER, level="INFO"):
            with make_client(app) as client:
                with self.assertRaises(WebSocketDisconnect) as caught:
                    with client.websocket_connect(WS_URL):
                        pass
        self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(sink.events, [])

    def test_a_websocket_without_the_capability_is_refused_and_audited(self):
        app, sink, _ = make_test_app(principal(SystemRole.USER))
        with make_client(app) as client:
            with self.assertRaises(WebSocketDisconnect) as caught:
                with client.websocket_connect(WS_ADMIN_URL):
                    pass
        self.assertEqual(caught.exception.code, 1008)
        (event,) = sink.events
        self.assertEqual((event.action, event.decision), ("admin.users.manage", "deny"))

    def test_an_allowed_websocket_is_accepted(self):
        app, sink, _ = make_test_app(principal(SystemRole.ADMIN))
        with make_client(app) as client:
            with client.websocket_connect(WS_ADMIN_URL) as socket:
                self.assertEqual(socket.receive_json(), {"hello": "admin"})
        self.assertEqual([e.decision for e in sink.events], ["allow"])

    def test_a_websocket_that_cannot_be_audited_is_told_to_retry_later(self):
        app, _, _ = make_test_app(principal(SystemRole.OWNER), sink=FailingSink())
        with self.assertLogs(LOGGER, level="ERROR"):
            with make_client(app) as client:
                with self.assertRaises(WebSocketDisconnect) as caught:
                    with client.websocket_connect(WS_ADMIN_URL):
                        pass
        self.assertEqual(caught.exception.code, 1013)


class OpenApiTest(unittest.TestCase):
    def test_protected_routes_appear_in_the_schema_without_extra_parameters(self):
        app, _, _ = make_test_app(None)
        with make_client(app) as client:
            paths = client.get("/api/v1/openapi.json").json()["paths"]
        parameters = paths["/test/projects/{project_id}/tasks"]["get"]["parameters"]
        self.assertEqual([p["name"] for p in parameters], ["project_id"])
        self.assertNotIn("parameters", paths["/test/admin"]["get"])


class ProviderSeamTest(unittest.TestCase):
    def test_the_provider_can_be_replaced_after_the_app_is_built(self):
        app, _, _ = make_test_app(None)
        with make_client(app) as client:
            with self.assertLogs(LOGGER, level="INFO"):
                self.assertEqual(client.get("/test/shared-memory").status_code, 401)
            app.state.principal_provider = StaticProvider(principal(SystemRole.USER))
            self.assertEqual(client.get("/test/shared-memory").status_code, 200)


if __name__ == "__main__":
    unittest.main()
