"""Backup and restore of a workspace through its Recovery Repository, on PostgreSQL.

End to end (PAW-047, Decision 0054): seed a workspace, run the Memory Projection
and a backup into a clone of a bare repository (all below a temporary
directory), empty the database as a fresh installation would be, clone the
repository again and restore from it. The dry run writes nothing; the applied
restore gives back the same rows (without credentials, conversation sources or
the user in deletion); a second restore, a stale or dirty clone and a tampered
file are refused. Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import insert, text

from paw_backend.connections.models import ConnectionQuotaRow
from paw_backend.db import Database
from paw_backend.identity.models import UserRow
from paw_backend.memory.models import (
    Conversation,
    Memory,
    MemoryRelation,
    MemorySource,
    MemoryVersion,
)
from paw_backend.memory.projection import MemoryProjectionRunner
from paw_backend.projects.models import ProjectMemberRow, ProjectRow
from paw_backend.recovery import RecoveryBackupRunner, RecoveryRestorer
from paw_backend.recovery.restore import TARGET_TABLES
from paw_backend.repositories.models import RepositoryRemoteRow, RepositoryRow

from .projects_support import T0, FakeClock, PostgresProjectTestCase
from .recovery_support import RecoveryWorld, git
from .retrieval_pg_support import requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL

SECRET = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"


@requires_postgres
class RecoveryPostgresTest(PostgresProjectTestCase):
    @classmethod
    def clean_tables(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE memories, conversations, connection_quotas,"
                    " repositories, project_members, projects CASCADE"
                )
            )
            connection.execute(text("TRUNCATE users CASCADE"))

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.world = RecoveryWorld(self)
        self.clock = FakeClock(T0 + timedelta(days=400))
        with self.engine.connect() as connection:
            self.started = connection.execute(text("SELECT clock_timestamp()")).scalar()
        self.seed()

    def new_database(self, url: str | None = None) -> Database:
        database = Database(make_settings(database_url=url or TEST_DATABASE_URL))
        self.addAsyncCleanup(database.dispose)
        return database

    def backup_url(self) -> str:
        """The backup's ``PAW_DATABASE_URL`` (the app role in the grants test)."""
        return TEST_DATABASE_URL

    def seed(self) -> None:
        self.owner = uuid4()
        self.alice = uuid4()
        self.gone = uuid4()
        self.project = uuid4()
        self.repo = uuid4()
        self.shared_memory = uuid4()
        self.private_memory = uuid4()
        self.gone_memory = uuid4()
        self.v1, self.v2, self.v_private, self.v_gone = (uuid4() for _ in range(4))
        conversation = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                insert(UserRow),
                [
                    dict(
                        id=self.owner,
                        login_name="owner",
                        system_role="owner",
                        status="active",
                        passkey_required=True,
                        created_at=T0,
                        updated_at=T0,
                    ),
                    dict(
                        id=self.alice,
                        login_name="alice",
                        system_role="user",
                        status="active",
                        passkey_required=False,
                        created_at=T0,
                        updated_at=T0,
                    ),
                    dict(
                        id=self.gone,
                        login_name="gone",
                        system_role="user",
                        status="pending_deletion",
                        passkey_required=False,
                        created_at=T0,
                        updated_at=T0,
                    ),
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO password_credentials (user_id, hash, created_at,"
                    " changed_at) VALUES (:user, :hash, :at, :at)"
                ),
                {
                    "user": self.alice,
                    "hash": "$argon2id$" + "not-a-real-hash",
                    "at": T0,
                },
            )
            connection.execute(
                insert(ConnectionQuotaRow),
                [
                    dict(
                        user_id=self.alice,
                        kind="codex",
                        metric="requests",
                        period="day",
                        limit_value=10,
                        created_at=T0,
                        updated_at=T0,
                    )
                ],
            )
            connection.execute(
                insert(ProjectRow),
                [
                    dict(
                        id=self.project,
                        name="Recovery",
                        description=f"has {SECRET}",
                        status="active",
                        created_by=self.owner,
                        created_at=T0,
                        updated_at=T0,
                    )
                ],
            )
            connection.execute(
                insert(ProjectMemberRow),
                [
                    dict(
                        project_id=self.project,
                        user_id=uid,
                        role=role,
                        status="active",
                        invited_at=T0,
                        joined_at=T0,
                    )
                    for uid, role in (
                        (self.owner, "manager"),
                        (self.alice, "contributor"),
                        (self.gone, "viewer"),
                    )
                ],
            )
            connection.execute(
                insert(RepositoryRow),
                [
                    dict(
                        id=self.repo,
                        project_id=self.project,
                        name="app",
                        default_branch="main",
                        source="github_clone",
                        acl_allowed=["read"],
                        created_by=self.gone,
                        created_at=T0,
                        updated_at=T0,
                    )
                ],
            )
            connection.execute(
                insert(RepositoryRemoteRow),
                [
                    dict(
                        repository_id=self.repo,
                        project_id=self.project,
                        url="https://github.com/example/app",
                        created_at=T0,
                    )
                ],
            )
            connection.execute(
                insert(Conversation),
                [dict(id=conversation, owner_user_id=self.alice, created_at=T0)],
            )
            connection.execute(
                insert(Memory),
                [
                    dict(id=memory, created_at=T0)
                    for memory in (
                        self.shared_memory,
                        self.private_memory,
                        self.gone_memory,
                    )
                ],
            )
            # One key set for every row (an executemany uses the first row's keys).
            base = dict(
                owner_user_id=None,
                project_id=None,
                verified_at=None,
                revalidate_after=None,
                memory_type="fact",
                importance=50,
                pinned=False,
                confirmation_state="confirmed",
                freshness_policy="permanent",
                revalidate_triggers=[],
                attributes={"k": "v"},
                actor_type="system",
                created_at=T0,
            )
            connection.execute(
                insert(MemoryVersion),
                [
                    dict(
                        base,
                        id=self.v1,
                        memory_id=self.shared_memory,
                        version_number=1,
                        scope="shared",
                        title="Old",
                        content="old text",
                        status="superseded",
                    ),
                    dict(
                        base,
                        id=self.v2,
                        memory_id=self.shared_memory,
                        version_number=2,
                        scope="project",
                        project_id=self.project,
                        title="New",
                        content="new text",
                        status="active",
                        freshness_policy="revalidate",
                        verified_at=T0,
                        revalidate_after=timedelta(days=7),
                        created_at=T0 + timedelta(seconds=1),
                    ),
                    dict(
                        base,
                        id=self.v_private,
                        memory_id=self.private_memory,
                        version_number=1,
                        scope="user",
                        owner_user_id=self.alice,
                        title="Alice's",
                        content="private",
                        status="active",
                    ),
                    dict(
                        base,
                        id=self.v_gone,
                        memory_id=self.gone_memory,
                        version_number=1,
                        scope="user",
                        owner_user_id=self.gone,
                        title="Gone's",
                        content="gone private text",
                        status="active",
                    ),
                ],
            )
            connection.execute(
                insert(MemoryRelation),
                [
                    dict(
                        id=uuid4(),
                        from_version_id=self.v2,
                        to_version_id=self.v1,
                        relation_type="supersedes",
                        created_at=T0,
                    )
                ],
            )
            connection.execute(
                insert(MemorySource),
                [
                    dict(
                        id=uuid4(),
                        memory_version_id=self.v2,
                        source_type="task",
                        source_ref="task:42",
                        conversation_id=None,
                        created_at=T0,
                    ),
                    dict(
                        id=uuid4(),
                        memory_version_id=self.v_private,
                        source_type="conversation",
                        source_ref=None,
                        conversation_id=conversation,
                        created_at=T0,
                    ),
                ],
            )

    def rows(self, table: str) -> list[dict]:
        with self.engine.connect() as connection:
            result = connection.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
            return sorted(
                (dict(row._mapping) for row in result),
                key=lambda row: repr(sorted(row.items(), key=str)),
            )

    def audit(self, kind: str) -> list[tuple[str, str]]:
        with self.engine.connect() as connection:
            result = connection.execute(
                text(
                    "SELECT action, reason FROM audit_events"
                    " WHERE resource_kind = :kind AND recorded_at >= :since"
                    " ORDER BY recorded_at"
                ),
                {"kind": kind, "since": self.started},
            )
            return [tuple(row) for row in result]

    async def back_up(self) -> None:
        database = self.new_database(self.backup_url())
        projection = await MemoryProjectionRunner(
            database,
            self.world.projection,
            protected_homes=self.world.homes,
            clock=self.clock,
        ).run()
        self.assertTrue(projection.ok, projection)
        result = await RecoveryBackupRunner(
            database,
            self.world.checkout,
            self.world.projection,
            protected_homes=self.world.homes,
            clock=self.clock,
        ).run()
        self.assertTrue(result.ok, result)

    def restorer(self, checkout) -> RecoveryRestorer:
        return RecoveryRestorer(
            self.new_database(),
            checkout,
            protected_homes=self.world.homes,
            clock=self.clock,
        )

    async def test_backup_then_restore_into_a_fresh_workspace(self) -> None:
        await self.back_up()
        before = {table: self.rows(table) for table in TARGET_TABLES}
        clone = self.world.clone()
        everything = b"".join(
            path.read_bytes()
            for path in clone.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
        for forbidden in (
            b"$argon2id$",
            SECRET.encode(),
            b"gone private text",
            b'"gone"',
        ):
            self.assertNotIn(forbidden, everything)
        self.assertIn(
            f"memory/users/{self.alice}".encode(),
            b"".join(
                str(path.relative_to(clone)).encode() for path in clone.rglob("*.md")
            ),
        )

        self.clean_tables()
        dry = await self.restorer(clone).run()
        self.assertTrue(dry.ok, dry)
        self.assertFalse(dry.applied)
        self.assertEqual([], self.rows("users"))
        self.assertEqual(
            "recovery.restore.planned", self.audit("recovery_restore")[-1][0]
        )

        applied = await self.restorer(clone).run(apply=True)
        self.assertTrue(applied.ok, applied)
        self.assertTrue(applied.applied)
        self.assertEqual(
            (
                "recovery.restore.applied",
                "users=2 projects=1 repos=1 memories=2 versions=3",
            ),
            self.audit("recovery_restore")[-1],
        )
        after = {table: self.rows(table) for table in TARGET_TABLES}

        def without(rows, key, values):
            return [row for row in rows if row[key] not in values]

        self.assertEqual(without(before["users"], "id", {self.gone}), after["users"])
        self.assertEqual(before["connection_quotas"], after["connection_quotas"])
        self.assertEqual(
            without(before["project_members"], "user_id", {self.gone}),
            after["project_members"],
        )
        self.assertEqual(
            [{**row, "created_by": None} for row in before["repositories"]],
            after["repositories"],
        )
        self.assertEqual(before["repository_remotes"], after["repository_remotes"])
        self.assertEqual(
            without(before["memories"], "id", {self.gone_memory}), after["memories"]
        )
        self.assertEqual(
            without(before["memory_versions"], "id", {self.v_gone}),
            after["memory_versions"],
        )
        self.assertEqual(before["memory_relations"], after["memory_relations"])
        self.assertEqual(
            without(before["memory_sources"], "source_type", {"conversation"}),
            after["memory_sources"],
        )
        # The project's description had a credential: it comes back redacted.
        self.assertEqual(
            ["has [REDACTED]"], [row["description"] for row in after["projects"]]
        )
        self.assertEqual([], self.rows("password_credentials"))
        self.assertTrue(any("owner-recover" in step for step in applied.manual_steps))

        again = await self.restorer(clone).run(apply=True)
        self.assertEqual("target_not_empty", again.refused)
        self.assertEqual(after, {table: self.rows(table) for table in TARGET_TABLES})

    async def test_a_stale_dirty_or_tampered_clone_is_refused(self) -> None:
        await self.back_up()
        stale = self.world.clone("stale")
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET updated_at = :at WHERE id = :id"),
                {"at": T0 + timedelta(days=1), "id": self.alice},
            )
        await self.back_up()
        git("fetch", "-q", "origin", cwd=stale)
        self.clean_tables()
        self.assertEqual("not_latest", (await self.restorer(stale).run()).refused)

        dirty = self.world.clone("dirty")
        (dirty / "users" / f"{self.alice}.json").write_text("{}\n")
        self.assertEqual("not_clean", (await self.restorer(dirty).run()).refused)

        tampered = self.world.clone("tampered")
        path = tampered / "users" / f"{self.alice}.json"
        path.write_text(path.read_text().replace("alice", "mallory"))
        git("commit", "-qam", "tamper", cwd=tampered)
        git("push", "-q", "origin", "HEAD:main", cwd=tampered)
        self.assertEqual(
            "checksum_mismatch", (await self.restorer(tampered).run()).refused
        )
        self.assertEqual(
            "recovery.restore.refused", self.audit("recovery_restore")[-1][0]
        )
        self.assertEqual([], self.rows("users"))
