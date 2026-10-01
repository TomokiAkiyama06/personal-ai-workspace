"""Projects, repositories and members over HTTP (issue #184; real PostgreSQL).

The routes of ``paw_backend/api/v1/projects.py`` on the application with its
sessions: the screen's list and detail, create, the lifecycle, the role change and
the administrator's list, with the negative cases (another user's project, a
project that does not exist, a role that may not act) that must not disclose
anything. Registering a repository runs git as the user's Linux account; its
translation is tested with a stand-in service (``RepositoryRegistrationTest``),
the service itself in ``test_repositories_*``.
"""

import uuid
from datetime import UTC, datetime, timedelta

from paw_backend.repositories import RepositoryNameTakenError
from paw_backend.repositories.errors import GitHubUnavailableError

from .auth_http_support import HttpTestCase, error_code, requires_postgres

NOW = datetime.now(UTC) - timedelta(days=1)


class ProjectsHttpCase(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice_id = self.make_user("alice")
        self.alice = self.login_token("alice")

    def user(self, name: str, role: str = "user") -> tuple[uuid.UUID, str]:
        user_id = self.make_user(name, role=role)
        return user_id, self.login_token(name)

    def create(self, name="Backend", token=None, **body) -> dict:
        response = self.call(
            "POST",
            "/api/v1/projects",
            token=token or self.alice,
            json={"name": name, **body},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def add_member(
        self,
        project_id: str,
        user_id: uuid.UUID,
        role: str = "viewer",
        *,
        invited: bool = False,
    ) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO project_members (project_id, user_id, role, status, "
                "invited_at, invite_expires_at, joined_at) VALUES "
                "(%(p)s, %(u)s, %(r)s, %(s)s, %(at)s, %(exp)s, %(joined)s)",
                {
                    "p": project_id,
                    "u": user_id,
                    "r": role,
                    "s": "invited" if invited else "active",
                    "at": NOW,
                    "exp": NOW + timedelta(days=14) if invited else None,
                    # After the creator's own row (the list is by join time).
                    "joined": None if invited else datetime.now(UTC),
                },
            )

    def add_repository(
        self, project_id: str, name: str, acl: list[str] | None = None
    ) -> str:
        repository_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO repositories (id, project_id, name, default_branch, "
                "source, acl_allowed, created_by, created_at, updated_at) VALUES "
                "(%(id)s, %(p)s, %(n)s, 'main', 'new_local', %(acl)s, NULL, "
                "%(at)s, %(at)s)",
                {
                    "id": repository_id,
                    "p": project_id,
                    "n": name,
                    "acl": acl,
                    "at": NOW,
                },
            )
        return str(repository_id)

    def get(self, path: str, token: str):
        return self.call("GET", f"/api/v1{path}", token=token)

    def post(self, path: str, token: str, body=None):
        return self.call("POST", f"/api/v1{path}", token=token, json=body)

    def assert_error(self, response, status: int, code: str) -> None:
        self.assertEqual(
            (response.status_code, error_code(response)), (status, code), response.text
        )


@requires_postgres
class ListAndDetailTest(ProjectsHttpCase):
    def test_create_list_and_detail(self):
        created = self.create("Backend", description="API")
        self.assertEqual(
            (created["name"], created["description"], created["status"]),
            ("Backend", "API", "active"),
        )
        project_id = created["id"]
        self.add_repository(project_id, "backend")
        self.add_repository(project_id, "ios", ["read"])

        listed = self.get("/projects", self.alice)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            listed.json(),
            {
                "projects": [
                    {
                        "id": project_id,
                        "name": "Backend",
                        "status": "active",
                        "my_role": "manager",
                        "repository_names": ["backend", "ios"],
                        "deletion_scheduled_at": None,
                    }
                ],
                "truncated": False,
            },
        )

        detail = self.get(f"/projects/{project_id}", self.alice)
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(
            (body["name"], body["description"], body["status"], body["my_role"]),
            ("Backend", "API", "active", "manager"),
        )
        self.assertEqual(
            [(r["name"], r["source"], r["acl"]) for r in body["repositories"]],
            [("backend", "new_local", None), ("ios", "new_local", ["read"])],
        )
        self.assertEqual(
            body["members"],
            [
                {
                    "user_id": str(self.alice_id),
                    "login_name": "alice",
                    "role": "manager",
                    "status": "active",
                    "creator": True,
                    "invite_expires_at": None,
                }
            ],
        )

    def test_a_closed_repository_is_left_out_and_the_override_is_listed(self):
        project_id = self.create()["id"]
        self.add_repository(project_id, "open")
        self.add_repository(project_id, "closed", [])
        self.add_repository(project_id, "narrow", ["agent", "read"])
        repositories = self.get(f"/projects/{project_id}/repositories", self.alice)
        self.assertEqual(repositories.status_code, 200, repositories.text)
        self.assertEqual(
            [(r["name"], r["acl"]) for r in repositories.json()["repositories"]],
            [("narrow", ["read", "agent"]), ("open", None)],
        )
        listed = self.get("/projects", self.alice).json()["projects"]
        self.assertEqual(listed[0]["repository_names"], ["narrow", "open"])

    def test_invitations_are_shown_to_a_manager_only(self):
        project_id = self.create()["id"]
        bob_id, bob = self.user("bob")
        carol_id = self.make_user("carol")
        self.add_member(project_id, bob_id, "viewer")
        self.add_member(project_id, carol_id, "contributor", invited=True)

        manager_view = self.get(f"/projects/{project_id}/members", self.alice)
        self.assertEqual(manager_view.status_code, 200, manager_view.text)
        self.assertEqual(
            [
                (m["login_name"], m["role"], m["status"], m["creator"])
                for m in manager_view.json()["members"]
            ],
            [
                ("alice", "manager", "active", True),
                ("bob", "viewer", "active", False),
                ("carol", "contributor", "invited", False),
            ],
        )
        invite = manager_view.json()["members"][2]
        self.assertIsNotNone(invite["invite_expires_at"])

        viewer_view = self.get(f"/projects/{project_id}", bob)
        self.assertEqual(viewer_view.status_code, 200, viewer_view.text)
        self.assertEqual(viewer_view.json()["my_role"], "viewer")
        self.assertEqual(
            [m["login_name"] for m in viewer_view.json()["members"]], ["alice", "bob"]
        )

    def test_an_invitation_does_not_show_the_project(self):
        project_id = self.create()["id"]
        carol_id, carol = self.user("carol")
        self.add_member(project_id, carol_id, "viewer", invited=True)
        self.assertEqual(self.get("/projects", carol).json()["projects"], [])
        self.assert_error(self.get(f"/projects/{project_id}", carol), 403, "forbidden")

    def test_the_list_holds_the_three_statuses(self):
        active = self.create("A")["id"]
        archived = self.create("B")["id"]
        pending = self.create("C")["id"]
        self.post(f"/projects/{archived}/archive", self.alice)
        self.post(
            f"/projects/{pending}/begin-deletion", self.alice, {"confirm_name": "C"}
        )
        listed = self.get("/projects", self.alice).json()["projects"]
        self.assertEqual(
            [(p["id"], p["status"]) for p in listed],
            [(active, "active"), (archived, "archived"), (pending, "pending_deletion")],
        )
        self.assertIsNotNone(listed[2]["deletion_scheduled_at"])
        self.assertEqual(listed[2]["repository_names"], [])


@requires_postgres
class IsolationTest(ProjectsHttpCase):
    """Another user's project, or one that does not exist, discloses nothing."""

    def test_a_non_member_learns_nothing(self):
        project_id = self.create("Secret")["id"]
        self.add_repository(project_id, "secret-repo")
        _, mallory = self.user("mallory")
        missing = str(uuid.uuid4())

        self.assertEqual(self.get("/projects", mallory).json()["projects"], [])
        for path in ("", "/repositories", "/members"):
            for target in (project_id, missing):
                response = self.get(f"/projects/{target}{path}", mallory)
                self.assert_error(response, 403, "forbidden")
                self.assertNotIn("Secret", response.text)
                self.assertNotIn("secret-repo", response.text)
        for action in ("archive", "unarchive", "restore"):
            self.assert_error(
                self.post(f"/projects/{project_id}/{action}", mallory), 403, "forbidden"
            )
        self.assert_error(
            self.post(
                f"/projects/{project_id}/begin-deletion",
                mallory,
                {"confirm_name": "Secret"},
            ),
            403,
            "forbidden",
        )
        self.assert_error(
            self.call(
                "PUT",
                f"/api/v1/projects/{project_id}/members/{self.alice_id}/role",
                token=mallory,
                json={"role": "viewer"},
            ),
            403,
            "forbidden",
        )
        self.assert_error(
            self.post(
                f"/projects/{project_id}/repositories",
                mallory,
                {"source": "new_local", "name": "x"},
            ),
            403,
            "forbidden",
        )
        self.assertEqual(
            self.scalar("SELECT status FROM projects WHERE id = :p", p=project_id),
            "active",
        )

    def test_a_viewer_and_a_contributor_cannot_manage(self):
        project_id = self.create("Team")["id"]
        bob_id, bob = self.user("bob")
        carol_id, carol = self.user("carol")
        self.add_member(project_id, bob_id, "viewer")
        self.add_member(project_id, carol_id, "contributor")
        for token in (bob, carol):
            self.assert_error(
                self.post(f"/projects/{project_id}/archive", token), 403, "forbidden"
            )
            self.assert_error(
                self.call(
                    "PUT",
                    f"/api/v1/projects/{project_id}/members/{bob_id}/role",
                    token=token,
                    json={"role": "manager"},
                ),
                403,
                "forbidden",
            )
            self.assert_error(
                self.post(
                    f"/projects/{project_id}/repositories",
                    token,
                    {"source": "new_local", "name": "x"},
                ),
                403,
                "forbidden",
            )
        self.assertEqual(
            self.scalar(
                "SELECT role FROM project_members WHERE user_id = :u", u=bob_id
            ),
            "viewer",
        )

    def test_an_anonymous_request_is_refused_before_anything_is_read(self):
        project_id = self.create()["id"]
        self.assert_error(self.get("/projects", None), 401, "unauthorized")
        self.assert_error(
            self.get(f"/projects/{project_id}", None), 401, "unauthorized"
        )
        self.assert_error(
            self.post("/projects", None, {"name": "x"}), 401, "unauthorized"
        )

    def test_a_malformed_id_is_refused(self):
        response = self.get("/projects/not-a-uuid", self.alice)
        self.assertIn(response.status_code, (403, 422), response.text)

    def test_the_admin_list_needs_an_administrator(self):
        self.create("Backend")
        self.assert_error(self.get("/admin/projects", self.alice), 403, "forbidden")


@requires_postgres
class LifecycleTest(ProjectsHttpCase):
    def test_archive_delete_and_restore(self):
        project_id = self.create("Backend")["id"]
        archived = self.post(f"/projects/{project_id}/archive", self.alice)
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertEqual(archived.json()["status"], "archived")
        # Archived is read-only: no repository can be added.
        self.assert_error(
            self.post(
                f"/projects/{project_id}/repositories",
                self.alice,
                {"source": "new_local", "name": "x"},
            ),
            403,
            "forbidden",
        )
        self.assertEqual(
            self.post(f"/projects/{project_id}/unarchive", self.alice).json()["status"],
            "active",
        )

        wrong = self.post(
            f"/projects/{project_id}/begin-deletion",
            self.alice,
            {"confirm_name": "backend"},
        )
        self.assert_error(wrong, 422, "confirmation_mismatch")
        pending = self.post(
            f"/projects/{project_id}/begin-deletion",
            self.alice,
            {"confirm_name": "Backend"},
        )
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual(pending.json()["status"], "pending_deletion")
        self.assertIsNotNone(pending.json()["deletion_scheduled_at"])
        # Pending deletion: the content is not readable any more.
        self.assert_error(
            self.get(f"/projects/{project_id}", self.alice), 403, "forbidden"
        )

        restored = self.post(f"/projects/{project_id}/restore", self.alice)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(
            (restored.json()["status"], restored.json()["deletion_scheduled_at"]),
            ("archived", None),
        )

    def test_a_viewer_does_not_see_a_project_pending_deletion(self):
        project_id = self.create("Backend")["id"]
        bob_id, bob = self.user("bob")
        self.add_member(project_id, bob_id, "viewer")
        self.post(
            f"/projects/{project_id}/begin-deletion",
            self.alice,
            {"confirm_name": "Backend"},
        )
        self.assertEqual(self.get("/projects", bob).json()["projects"], [])
        self.assert_error(
            self.post(f"/projects/{project_id}/restore", bob), 403, "forbidden"
        )

    def test_an_administrator_restores_without_being_a_member(self):
        project_id = self.create("Backend")["id"]
        self.post(
            f"/projects/{project_id}/begin-deletion",
            self.alice,
            {"confirm_name": "Backend"},
        )
        _, admin = self.user("admin-one", role="admin")
        page = self.get("/admin/projects?status=pending_deletion", admin)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(
            [(p["id"], p["name"], p["status"]) for p in page.json()["projects"]],
            [(project_id, "Backend", "pending_deletion")],
        )
        self.assertIsNone(page.json()["next_cursor"])
        restored = self.post(f"/projects/{project_id}/restore", admin)
        self.assertEqual(restored.status_code, 200, restored.text)
        # Managing is not reading (Decision 0004): the content stays closed.
        self.assert_error(self.get(f"/projects/{project_id}", admin), 403, "forbidden")

    def test_the_admin_list_pages(self):
        for name in ("A", "B", "C"):
            self.create(name)
        _, admin = self.user("admin-one", role="admin")
        first = self.get("/admin/projects?limit=2", admin).json()
        self.assertEqual([p["name"] for p in first["projects"]], ["C", "B"])
        second = self.get(
            f"/admin/projects?limit=2&cursor={first['next_cursor']}", admin
        ).json()
        self.assertEqual(
            ([p["name"] for p in second["projects"]], second["next_cursor"]),
            (["A"], None),
        )
        bad = self.get("/admin/projects?cursor=nonsense", admin)
        self.assert_error(bad, 422, "validation_error")


@requires_postgres
class MembersTest(ProjectsHttpCase):
    def test_change_role(self):
        project_id = self.create("Team")["id"]
        bob_id, bob = self.user("bob")
        self.add_member(project_id, bob_id, "viewer")
        path = f"/api/v1/projects/{project_id}/members/{bob_id}/role"
        changed = self.call("PUT", path, token=self.alice, json={"role": "manager"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json(), {"user_id": str(bob_id), "role": "manager"})
        # Bob's next request carries the new role.
        self.assertEqual(
            self.get(f"/projects/{project_id}", bob).json()["my_role"], "manager"
        )

        own = f"/api/v1/projects/{project_id}/members/{self.alice_id}/role"
        self.assertEqual(
            self.call("PUT", own, token=bob, json={"role": "viewer"}).status_code, 200
        )
        last = self.call("PUT", path, token=bob, json={"role": "viewer"})
        self.assert_error(last, 409, "last_manager")
        unknown = self.call(
            "PUT",
            f"/api/v1/projects/{project_id}/members/{uuid.uuid4()}/role",
            token=bob,
            json={"role": "viewer"},
        )
        self.assert_error(unknown, 404, "not_found")
        invalid = self.call("PUT", path, token=bob, json={"role": "owner"})
        self.assert_error(invalid, 422, "validation_error")

    def test_create_refuses_an_invalid_name(self):
        response = self.call(
            "POST", "/api/v1/projects", token=self.alice, json={"name": "   "}
        )
        self.assert_error(response, 422, "validation_error")
        self.assertEqual(self.scalar("SELECT count(*) FROM projects"), 0)


class _StandInRepositories:
    """Records the registration calls; answers with a fixed error or result."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.error = error

    async def _record(self, method, /, *args, **kwargs):
        self.calls.append((method, args[1:], kwargs))
        if self.error is not None:
            raise self.error
        raise AssertionError("the stand-in only answers with an error")

    async def register_existing(self, *args, **kwargs):
        return await self._record("register_existing", *args, **kwargs)

    async def clone_from_github(self, *args, **kwargs):
        return await self._record("clone_from_github", *args, **kwargs)

    async def create_local(self, *args, **kwargs):
        return await self._record("create_local", *args, **kwargs)

    async def create_github(self, *args, **kwargs):
        return await self._record("create_github", *args, **kwargs)


@requires_postgres
class RepositoryRegistrationTest(ProjectsHttpCase):
    def register(self, project_id: str, body: dict):
        return self.post(f"/projects/{project_id}/repositories", self.alice, body)

    def test_each_way_reaches_its_service_method(self):
        project_id = self.create()["id"]
        stand_in = _StandInRepositories(RepositoryNameTakenError())
        self.app.state.repositories = stand_in
        bodies = [
            {"source": "existing_path", "path": "/home/alice/src/api"},
            {"source": "github_clone", "url": "owner/api", "branch": "dev"},
            {"source": "new_local", "name": "api", "default_branch": "trunk"},
            {"source": "new_github", "name": "api", "private": False},
        ]
        for body in bodies:
            self.assert_error(
                self.register(project_id, body), 409, "repository_name_taken"
            )
        pid = uuid.UUID(project_id)
        self.assertEqual(
            stand_in.calls,
            [
                ("register_existing", (pid, "/home/alice/src/api"), {"name": None}),
                (
                    "clone_from_github",
                    (pid, "owner/api"),
                    {"name": None, "branch": "dev"},
                ),
                ("create_local", (pid, "api"), {"default_branch": "trunk"}),
                ("create_github", (pid, "api"), {"private": False}),
            ],
        )

    def test_github_unavailable_and_bad_bodies(self):
        project_id = self.create()["id"]
        self.app.state.repositories = _StandInRepositories(GitHubUnavailableError())
        self.assert_error(
            self.register(project_id, {"source": "new_github", "name": "api"}),
            503,
            "github_unavailable",
        )
        for body in (
            {"source": "elsewhere", "name": "api"},
            {"source": "new_local"},
            {"source": "new_local", "name": "api", "path": "/tmp"},
            {"source": "new_github", "name": "api", "private": "yes"},
        ):
            self.assert_error(self.register(project_id, body), 422, "validation_error")

    def test_the_real_service_refuses_a_user_without_a_linux_account(self):
        # The test user "alice" has no Linux account of a person on this machine.
        project_id = self.create()["id"]
        response = self.register(project_id, {"source": "new_local", "name": "api"})
        self.assert_error(response, 409, "linux_account_unavailable")
        self.assertEqual(self.scalar("SELECT count(*) FROM repositories"), 0)
