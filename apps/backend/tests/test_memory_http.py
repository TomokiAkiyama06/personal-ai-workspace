"""``/api/v1/memory/*`` through the application (issue #186, Decision 0068).

Real sessions (a password sign-in) on a real PostgreSQL: the routes answer the
Memory screen's ``MemorySource`` (apps/web/src/memory), and never another
person's memory or a project the reader is not a member of (404, as a missing
id). The rules of each read and write are tested on the services
(``test_memory_board.py``, ``test_memory_versioning_service.py``); this is the
HTTP layer: authentication, the answers' shape, the error codes, the audit.
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import text

from paw_backend.memory.board import MemoryBoard
from paw_backend.memory.versioning import MemoryDatabaseError

from .auth_http_support import T0, HttpTestCase, requires_postgres

API = "/api/v1/memory"
# T0 as the API writes it.
STAMP = T0.isoformat().replace("+00:00", "Z")


@requires_postgres
class MemoryHttpTest(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE memories, projects CASCADE"))
        self.alice = self.make_user("alice")
        self.bob = self.make_user("bob")
        self.token = self.login_token("alice")

    # -- seeding (SQL) ---------------------------------------------------------------

    def seed_project(self, name: str = "Example") -> uuid.UUID:
        project_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, name, status, created_at, updated_at)"
                    " VALUES (:id, :name, 'active', :t, :t)"
                ),
                {"id": project_id, "name": name, "t": T0},
            )
        return project_id

    def seed_member(self, project_id: uuid.UUID, user_id: uuid.UUID, role: str):
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO project_members (project_id, user_id, role, status,"
                    " invited_at, joined_at) VALUES (:p, :u, :r, 'active', :t, :t)"
                ),
                {"p": project_id, "u": user_id, "r": role, "t": T0},
            )

    def seed_repo(self, project_id: uuid.UUID, name: str = "backend") -> uuid.UUID:
        repo_id = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repositories (id, project_id, name, default_branch,"
                    " source, created_at, updated_at) VALUES (:id, :p, :name, 'main',"
                    " 'new_local', :t, :t)"
                ),
                {"id": repo_id, "p": project_id, "name": name, "t": T0},
            )
        return repo_id

    def seed_memory(self, title: str, **columns) -> tuple[uuid.UUID, uuid.UUID]:
        """Version 1 of a new memory (user scope of alice unless told otherwise)."""
        values = {
            "scope": "user",
            "owner_user_id": self.alice,
            "project_id": None,
            "repo_id": None,
            "content": f"{title} content",
            "actor_type": "user",
            "actor_user_id": self.alice,
            "freshness_policy": "permanent",
            "revalidate_after": None,
            "verified_at": None,
        }
        values.update(columns)
        with self.engine.begin() as connection:
            memory_id = connection.execute(
                text("INSERT INTO memories (created_at) VALUES (:t) RETURNING id"),
                {"t": T0},
            ).scalar_one()
            version_id = connection.execute(
                text(
                    "INSERT INTO memory_versions (memory_id, version_number, scope,"
                    " owner_user_id, project_id, repo_id, memory_type, title, content,"
                    " status, confirmation_state, freshness_policy, verified_at,"
                    " revalidate_after, actor_type, actor_user_id, created_at)"
                    " VALUES (:m, 1, :scope, :owner_user_id, :project_id, :repo_id,"
                    " 'note', :title, :content, 'active', 'confirmed',"
                    " :freshness_policy, :verified_at, :revalidate_after,"
                    " :actor_type, :actor_user_id, :t) RETURNING id"
                ),
                {"m": memory_id, "title": title, "t": T0, **values},
            ).scalar_one()
        return memory_id, version_id

    def get(self, path: str, **options):
        return self.call("GET", f"{API}{path}", token=self.token, **options)

    def post(self, path: str, body: dict, **options):
        options.setdefault("token", self.token)
        return self.call("POST", f"{API}{path}", json=body, **options)

    def versions(self, memory_id: uuid.UUID) -> list:
        return self.rows(
            "SELECT version_number, status, title FROM memory_versions"
            " WHERE memory_id = :m ORDER BY version_number",
            m=memory_id,
        )

    # -- authentication ----------------------------------------------------------------

    def test_every_route_needs_a_session(self):
        memory_id, _ = self.seed_memory("mine")
        for method, path, body in (
            ("GET", "/scopes", None),
            ("GET", "/memories?scope=user", None),
            ("GET", f"/memories/{memory_id}/history", None),
            ("GET", f"/memories/{memory_id}/versions/1/sources", None),
            ("POST", f"/memories/{memory_id}/edit", {"expected_version": 1}),
            (
                "POST",
                f"/memories/{memory_id}/restore",
                {"expected_version": 1, "source_version": 1},
            ),
        ):
            response = self.call(method, f"{API}{path}", json=body)
            self.assertEqual(response.status_code, 401, (method, path))
            self.assertEqual(response.json()["error"]["code"], "unauthorized")
        self.assertEqual(len(self.versions(memory_id)), 1)

    # -- reads ------------------------------------------------------------------------

    def test_the_scope_tree(self):
        project = self.seed_project("Example")
        self.seed_member(project, self.alice, "viewer")
        repo = self.seed_repo(project, "backend")
        foreign = self.seed_project("Foreign")
        self.seed_memory("mine")
        self.seed_memory("theirs", owner_user_id=self.bob, actor_user_id=self.bob)
        self.seed_memory(
            "project", scope="project", owner_user_id=None, project_id=project
        )
        self.seed_memory("repo", scope="repo", owner_user_id=None, repo_id=repo)
        self.seed_memory(
            "foreign", scope="project", owner_user_id=None, project_id=foreign
        )
        self.seed_memory("shared", scope="shared", owner_user_id=None)

        response = self.get("/scopes")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "user": 1,
                "projects": [
                    {
                        "project_id": str(project),
                        "name": "Example",
                        "count": 2,
                        "project_count": 1,
                        "repos": [
                            {"repo_id": str(repo), "name": "backend", "count": 1}
                        ],
                    }
                ],
                "shared": 1,
            },
        )
        # Allowed reads are not recorded (Decision 0024: DENIED_ONLY).
        allowed = [key for key in self.audit_summary() if key[1] == "allow"]
        self.assertEqual(allowed, [("auth.login", "allow", "authenticated")])

    def test_the_list_answers_the_screens_memory_version(self):
        memory_id, version_id = self.seed_memory(
            "deploy day",
            freshness_policy="revalidate",
            verified_at=T0,
            revalidate_after=timedelta(days=90),
        )
        self.seed_memory("lint")

        response = self.get("/memories", params={"scope": "user", "q": "DEPLOY"})

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["truncated"])
        (entry,) = body["memories"]
        self.assertEqual(
            entry,
            {
                "memory_id": str(memory_id),
                "version_id": str(version_id),
                "version_number": 1,
                "scope": "user",
                "owner_user_id": str(self.alice),
                "project_id": None,
                "project_group_id": None,
                "repo_id": None,
                "memory_type": "note",
                "title": "deploy day",
                "content": "deploy day content",
                "importance": 50,
                "pinned": False,
                "status": "active",
                "confirmation_state": "confirmed",
                "freshness_policy": "revalidate",
                "verified_at": STAMP,
                "revalidate_after": 90 * 86_400,
                "revalidate_triggers": [],
                "expires_at": None,
                "commit_sha": None,
                "branch": None,
                "stale_since": None,
                "actor_type": "user",
                "actor_user_id": str(self.alice),
                "actor_name": "alice",
                "change_reason": None,
                "created_at": STAMP,
            },
        )

    def test_a_project_the_reader_is_not_in_is_not_found(self):
        project = self.seed_project()
        repo = self.seed_repo(project)
        self.seed_memory(
            "secret", scope="project", owner_user_id=None, project_id=project
        )
        for params in (
            {"scope": "project", "project_id": str(project)},
            {"scope": "repo", "project_id": str(project), "repo_id": str(repo)},
        ):
            response = self.get("/memories", params=params)
            self.assertEqual(response.status_code, 404, params)
            self.assertEqual(response.json()["error"]["code"], "memory_not_found")
            self.assertNotIn("secret", response.text)

    def test_a_malformed_scope_is_a_validation_error(self):
        project = str(uuid.uuid4())
        for params in (
            {},
            {"scope": "group"},
            {"scope": "project"},
            {"scope": "user", "project_id": project},
            {"scope": "repo", "project_id": project},
            {"scope": "user", "q": "x" * 201},
        ):
            response = self.get("/memories", params=params)
            self.assertEqual(response.status_code, 422, params)
            self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_the_history_graph(self):
        memory_id, first = self.seed_memory("first")
        edited = self.post(
            f"/memories/{memory_id}/edit",
            {"expected_version": 1, "content": "second", "reason": "moved"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)

        response = self.get(f"/memories/{memory_id}/history")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            [(v["version_number"], v["status"]) for v in body["versions"]],
            [(1, "superseded"), (2, "active")],
        )
        second = body["versions"][1]["version_id"]
        self.assertEqual(
            body["relations"],
            [
                {
                    "from_version_id": second,
                    "to_version_id": str(first),
                    "relation": "supersedes",
                    "reason": "content",
                }
            ],
        )
        self.assertEqual(body["related"], [])
        self.assertTrue(body["can_write"])

    def test_a_viewer_reads_the_history_but_cannot_write(self):
        project = self.seed_project()
        self.seed_member(project, self.alice, "viewer")
        memory_id, _ = self.seed_memory(
            "rule", scope="project", owner_user_id=None, project_id=project
        )
        response = self.get(f"/memories/{memory_id}/history")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["can_write"])
        response = self.post(
            f"/memories/{memory_id}/edit", {"expected_version": 1, "title": "mine now"}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "forbidden")
        self.assertEqual(len(self.versions(memory_id)), 1)

    def test_somebody_elses_memory_is_not_found(self):
        theirs, _ = self.seed_memory(
            "theirs", owner_user_id=self.bob, actor_user_id=self.bob
        )
        project = self.seed_project()
        foreign, _ = self.seed_memory(
            "foreign", scope="project", owner_user_id=None, project_id=project
        )
        for memory_id in (theirs, foreign, uuid.uuid4()):
            for response in (
                self.get(f"/memories/{memory_id}/history"),
                self.get(f"/memories/{memory_id}/versions/1/sources"),
                self.post(
                    f"/memories/{memory_id}/edit",
                    {"expected_version": 1, "title": "taken"},
                ),
                self.post(
                    f"/memories/{memory_id}/restore",
                    {"expected_version": 1, "source_version": 1},
                ),
            ):
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["error"]["code"], "memory_not_found")
        self.assertEqual([r.title for r in self.versions(theirs)], ["theirs"])
        self.assertEqual([r.title for r in self.versions(foreign)], ["foreign"])

    def test_the_sources_of_a_version(self):
        memory_id, version_id = self.seed_memory("sourced")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_sources (memory_version_id, source_type,"
                    " source_ref, created_at) VALUES (:v, 'task', 'task-7', :t)"
                ),
                {"v": version_id, "t": T0},
            )
        response = self.get(f"/memories/{memory_id}/versions/1/sources")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "sources": [
                    {
                        "source_type": "task",
                        "conversation_id": None,
                        "message_id": None,
                        "source_ref": "task-7",
                        "source_deleted_at": None,
                        "created_at": STAMP,
                    }
                ]
            },
        )
        response = self.get(f"/memories/{memory_id}/versions/0/sources")
        self.assertEqual(response.status_code, 422)

    # -- writes -----------------------------------------------------------------------

    def test_an_edit_writes_a_new_version_named_after_its_writer(self):
        memory_id, _ = self.seed_memory("first")
        response = self.post(
            f"/memories/{memory_id}/edit",
            {"expected_version": 1, "title": "second", "reason": "renamed"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            (body["version_number"], body["title"], body["status"]),
            (2, "second", "active"),
        )
        self.assertEqual(
            (body["actor_user_id"], body["actor_name"], body["change_reason"]),
            (str(self.alice), "alice", "renamed"),
        )
        self.assertEqual(
            [(r.version_number, r.status) for r in self.versions(memory_id)],
            [(1, "superseded"), (2, "active")],
        )
        # A write is audited (memory.use is REQUIRED).
        self.assertIn(("memory.use", "allow"), {k[:2] for k in self.audit_summary()})

    def test_a_committed_edit_is_answered_even_if_the_name_cannot_be_read(self):
        memory_id, _ = self.seed_memory("first")

        async def broken(*_):
            raise MemoryDatabaseError("08006")

        with patch.object(MemoryBoard, "named", broken):
            response = self.post(
                f"/memories/{memory_id}/edit", {"expected_version": 1, "title": "b"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            (response.json()["version_number"], response.json()["actor_name"]),
            (2, None),
        )

    def test_an_edit_on_an_old_version_is_a_conflict(self):
        memory_id, _ = self.seed_memory("first")
        self.assertEqual(
            self.post(
                f"/memories/{memory_id}/edit", {"expected_version": 1, "title": "b"}
            ).status_code,
            200,
        )
        response = self.post(
            f"/memories/{memory_id}/edit", {"expected_version": 1, "title": "c"}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "memory_version_conflict")
        self.assertEqual([r.title for r in self.versions(memory_id)], ["first", "b"])

    def test_an_edit_needs_a_change_and_a_well_formed_body(self):
        memory_id, _ = self.seed_memory("first")
        for body in (
            {"expected_version": 1},
            {"expected_version": 1, "reason": "only a reason"},
            {"expected_version": 0, "title": "x"},
            {"expected_version": "1", "title": "x"},
            {"expected_version": 1, "title": ""},
            {"expected_version": 1, "title": "x" * 201},
            {"expected_version": 1, "title": "x", "scope": "shared"},
        ):
            response = self.post(f"/memories/{memory_id}/edit", body)
            self.assertEqual(response.status_code, 422, (body, response.text))
            self.assertEqual(response.json()["error"]["code"], "validation_error")
        self.assertEqual(len(self.versions(memory_id)), 1)

    def test_a_restore_writes_the_old_content_as_a_new_version(self):
        memory_id, _ = self.seed_memory("first")
        self.post(f"/memories/{memory_id}/edit", {"expected_version": 1, "title": "b"})
        response = self.post(
            f"/memories/{memory_id}/restore",
            {"expected_version": 1, "source_version": 1},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "memory_version_conflict")
        response = self.post(
            f"/memories/{memory_id}/restore",
            {"expected_version": 2, "source_version": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            (response.json()["version_number"], response.json()["title"]), (3, "first")
        )
        self.assertEqual(
            [(r.version_number, r.status) for r in self.versions(memory_id)],
            [(1, "superseded"), (2, "superseded"), (3, "active")],
        )
        response = self.post(
            f"/memories/{memory_id}/restore",
            {"expected_version": 3, "source_version": 9},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "memory_state_conflict")

    def test_a_cross_origin_write_is_refused(self):
        memory_id, _ = self.seed_memory("first")
        response = self.call(
            "POST",
            f"{API}/memories/{memory_id}/edit",
            token=self.token,
            origin="https://evil.example",
            content=json.dumps({"expected_version": 1, "title": "x"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(len(self.versions(memory_id)), 1)
