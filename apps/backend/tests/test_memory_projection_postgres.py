"""The Memory Markdown Projection on a real PostgreSQL (PAW-045).

PostgreSQL is the source of truth: a run writes the current version of every
memory into its audience's directory (``users/<id>``, ``projects/<id>``,
``project-groups/<id>``, ``repos/<id>``, ``shared``), leaves out ``session_only``
and older versions, follows a memory that moved to another audience, rewrites
nothing when nothing changed, and records every outcome in ``audit_events``.
Files are written below a temporary directory only. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

import os
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.db import Database
from paw_backend.memory.projection import (
    INDEX_FILE,
    MemoryProjectionRunner,
    MemoryProjectionSource,
    ProjectionAction,
    projection_status,
)

from .projection_support import TemporaryRoot, tree
from .retrieval_pg_support import T0, PostgresRetrievalTestCase, requires_postgres
from .support import make_settings


@requires_postgres
class PostgresProjectionTestCase(PostgresRetrievalTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmp = TemporaryRoot(self)
        with self.engine.connect() as connection:
            self.started = connection.execute(text("SELECT clock_timestamp()")).scalar()

    def new_database(self) -> Database:
        database = Database(make_settings(database_url=self.database_url()))
        self.addAsyncCleanup(database.dispose)
        return database

    def runner(self, root=None) -> MemoryProjectionRunner:
        return MemoryProjectionRunner(
            self.new_database(),
            root or self.tmp.root,
            protected_homes=self.tmp.homes,
            clock=self.clock,
        )

    def outcome_rows(self) -> list[tuple[str, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT action, reason FROM audit_events"
                    " WHERE resource_kind = 'memory_projection_run'"
                    " AND recorded_at >= :since ORDER BY recorded_at"
                ),
                {"since": self.started},
            )
            return [tuple(row) for row in rows]

    def after_every_recorded_run(self) -> None:
        """Move the clock past every projection row already in ``audit_events``.

        The rows of earlier tests stay (the table is append-only), and the status
        orders by the application's ``occurred_at``.
        """
        with self.engine.connect() as connection:
            latest = connection.execute(
                text(
                    "SELECT max(occurred_at) FROM audit_events"
                    " WHERE resource_kind = 'memory_projection_run'"
                )
            ).scalar()
        if latest is not None and latest >= self.clock.now:
            self.clock.now = latest + timedelta(days=1)

    def files(self) -> dict[str, bytes]:
        return tree(self.tmp.root)


class ProjectionTest(PostgresProjectionTestCase):
    async def test_every_audience_gets_its_own_directory(self):
        alice, bob = uuid4(), uuid4()
        project, group, repo = uuid4(), uuid4(), uuid4()
        a = self.seed("alice private", "alice secret plan", owner=alice, embed=False)
        b = self.seed("bob private", "bob secret plan", owner=bob, embed=False)
        p = self.seed("project fact", scope="project", project=project, embed=False)
        g = self.seed("group pref", scope="project_group", group=group, embed=False)
        r = self.seed("repo fact", scope="repo", repo=repo, embed=False)
        s = self.seed("shared rule", scope="shared", embed=False)

        result = await self.runner().run()

        self.assertTrue(result.ok)
        self.assertEqual(result.memories, 6)
        files = self.files()
        expected = {
            f"users/{alice}/{a.memory_id}.md",
            f"users/{bob}/{b.memory_id}.md",
            f"projects/{project}/{p.memory_id}.md",
            f"project-groups/{group}/{g.memory_id}.md",
            f"repos/{repo}/{r.memory_id}.md",
            f"shared/{s.memory_id}.md",
        }
        self.assertTrue(expected <= set(files))
        # Alice's private text is in Alice's directory and nowhere else.
        for path, data in files.items():
            with self.subTest(path=path):
                if b"alice secret plan" in data or str(alice).encode() in data:
                    self.assertTrue(path.startswith(f"users/{alice}/"), path)
                if b"bob secret plan" in data:
                    self.assertTrue(path.startswith(f"users/{bob}/"), path)
        self.assertEqual(
            self.outcome_rows(),
            [
                (
                    ProjectionAction.COMPLETED.value,
                    "memories=6 written=12 removed=0 redacted=0",
                )
            ],
        )

    async def test_only_the_current_version_and_no_session_only_memory(self):
        owner = uuid4()
        old = self.seed(
            "tabs", "old text v1", owner=owner, status="superseded", embed=False
        )
        self.seed(
            "tabs",
            "new text v2",
            owner=owner,
            memory_id=old.memory_id,
            version_number=2,
            embed=False,
        )
        session = self.seed(
            "scratch",
            "session only text",
            owner=owner,
            freshness="session_only",
            embed=False,
        )
        deprecated = self.seed(
            "gone", "deprecated text", owner=owner, status="deprecated", embed=False
        )

        await self.runner().run()

        files = self.files()
        data = files[f"users/{owner}/{old.memory_id}.md"]
        self.assertIn(b"new text v2", data)
        self.assertIn(b"version: 2", data)
        everything = b"".join(files.values())
        self.assertNotIn(b"old text v1", everything)
        self.assertNotIn(b"session only text", everything)
        self.assertNotIn(str(session.memory_id).encode(), everything)
        index = files[f"users/{owner}/{INDEX_FILE}"].decode()
        self.assertIn(
            f"| deprecated | note | gone | 1 | [{deprecated.memory_id}.md]", index
        )

    async def test_a_narrowed_memory_leaves_the_project_directory(self):
        project, editor = uuid4(), uuid4()
        before = self.seed(
            "plan", "the plan", scope="project", project=project, embed=False
        )
        await self.runner().run()
        self.assertIn(f"projects/{project}/{before.memory_id}.md", self.files())
        # Decision 0034 2: the project version is superseded by the editor's own
        # user version of the same memory.
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL paw.actor_type = 'system'"))
            connection.execute(
                text("UPDATE memory_versions SET status = 'superseded' WHERE id = :v"),
                {"v": before.version_id},
            )
        self.seed(
            "plan",
            "the plan",
            owner=editor,
            memory_id=before.memory_id,
            version_number=2,
            embed=False,
        )

        result = await self.runner().run()

        self.assertEqual(result.report.removed, 2)
        files = self.files()
        self.assertFalse(any(path.startswith("projects/") for path in files))
        self.assertIn(f"users/{editor}/{before.memory_id}.md", files)

    async def test_a_run_without_changes_rewrites_nothing(self):
        self.seed("one", owner=uuid4(), embed=False)
        self.seed("two", scope="shared", embed=False)
        await self.runner().run()
        mtimes = {
            path: os.stat(self.tmp.root / path).st_mtime_ns for path in self.files()
        }
        result = await self.runner().run()
        self.assertEqual(result.report.written, 0)
        self.assertEqual(
            {path: os.stat(self.tmp.root / path).st_mtime_ns for path in self.files()},
            mtimes,
        )
        self.assertEqual(
            self.outcome_rows()[-1],
            (
                ProjectionAction.COMPLETED.value,
                "memories=2 written=0 removed=0 redacted=0",
            ),
        )

    async def test_credentials_stay_in_the_database_only(self):
        token = "ghp_" + "a1B2" * 9
        seeded = self.seed("deploy key", f"use {token}", owner=uuid4(), embed=False)
        result = await self.runner().run()
        self.assertEqual(result.redactions, 1)
        self.assertNotIn(token.encode(), b"".join(self.files().values()))
        with self.engine.connect() as connection:
            content = connection.execute(
                text("SELECT content FROM memory_versions WHERE id = :v"),
                {"v": seeded.version_id},
            ).scalar()
        self.assertIn(token, content)

    async def test_the_source_reads_one_snapshot_in_memory_order(self):
        for _ in range(3):
            self.seed("x", owner=uuid4(), embed=False)
        memories = await MemoryProjectionSource(self.new_database()).current_versions()
        ids = [memory.memory_id for memory in memories]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(memories[0].created_at, T0)


class FailureTest(PostgresProjectionTestCase):
    async def test_a_refused_directory_is_audited_as_a_failure(self):
        self.seed("x", owner=uuid4(), embed=False)
        checkout = self.tmp.base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

        result = await self.runner(checkout / "memory").run()

        self.assertFalse(result.ok)
        self.assertTrue(result.audited)
        self.assertEqual(
            self.outcome_rows(),
            [(ProjectionAction.FAILED.value, "check_target:inside_git_work_tree")],
        )
        self.assertFalse((checkout / "memory").exists())

    async def test_the_status_shows_the_last_run_and_the_last_success(self):
        self.after_every_recorded_run()
        self.seed("x", owner=uuid4(), embed=False)
        await self.runner().run()
        completed_at = self.clock()
        self.clock.advance(minutes=5)
        checkout = self.tmp.base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        await self.runner(checkout / "memory").run()

        status = await projection_status(self.new_database())

        self.assertEqual(status.last_action, ProjectionAction.FAILED.value)
        self.assertEqual(status.last_reason, "check_target:inside_git_work_tree")
        self.assertEqual(status.last_run_at, self.clock())
        self.assertEqual(status.last_completed_at, completed_at)
