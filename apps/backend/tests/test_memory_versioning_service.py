"""Manual Memory versioning on a real PostgreSQL (PAW-042).

Edits, restores, deprecations and revalidations write a new version and keep the
old one; relations ``supersedes`` / ``extends`` / ``conflicts_with``; the Optimistic
Lock; who may change which scope; and that a retrieval (PAW-043) afterwards offers
only the active version.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.authz import Principal, ProjectRole, SystemRole
from paw_backend.memory.models import FreshnessPolicy, MemoryScope
from paw_backend.memory.versioning import (
    RELATION_GRAPH_LOCK_KEY,
    FreshnessSpec,
    InputProblem,
    InvalidMemoryInputError,
    ManualRelation,
    MemoryBusyError,
    MemoryChanges,
    MemoryDraft,
    MemoryNotFoundError,
    MemoryPermissionError,
    MemoryScopeNotSupportedError,
    MemoryStateError,
    MemoryVersionConflictError,
    RevalidateTrigger,
    StateProblem,
)
from paw_backend.projects import ProjectStatus

from .retrieval_pg_support import titles
from .versioning_support import T0, PostgresVersioningTestCase, requires_postgres

QUERY = "deploy backend friday"


def draft(**overrides):
    values = {
        "scope": MemoryScope.USER,
        "memory_type": "preference",
        "title": "deploy day",
        "content": "deploy backend friday",
    }
    values.update(overrides)
    return MemoryDraft(**values)


@requires_postgres
class CreateTest(PostgresVersioningTestCase):
    async def test_a_person_creates_a_confirmed_version_one(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        (row,) = self.versions(created.memory_id)
        self.assertEqual(row.version_number, 1)
        self.assertEqual(row.scope, "user")
        self.assertEqual(row.owner_user_id, me.user_id)
        self.assertEqual(row.status, "active")
        self.assertEqual(row.confirmation_state, "confirmed")
        self.assertEqual(row.actor_type, "user")
        self.assertEqual(row.actor_user_id, me.user_id)
        self.assertEqual(self.audit_actions(), [("memory.use", "allow")])

    async def test_a_revalidate_memory_is_verified_when_it_is_written(self):
        me = self.user()
        spec = FreshnessSpec.revalidate(
            timedelta(days=90), (RevalidateTrigger.MODEL_CHANGED,)
        )
        created = await self.versioning.create_memory(me, draft(freshness=spec))
        (row,) = self.versions(created.memory_id)
        self.assertEqual(row.freshness_policy, "revalidate")
        self.assertEqual(row.verified_at, T0)
        self.assertEqual(row.revalidate_after, timedelta(days=90))
        self.assertEqual(row.revalidate_triggers, ["model_changed"])
        self.assertEqual(row.on_stale, "lower_priority")

    async def test_session_only_is_not_written_as_long_term_memory(self):
        me = self.user()
        with self.assertRaises(InvalidMemoryInputError) as caught:
            await self.versioning.create_memory(
                me, draft(freshness=FreshnessSpec(FreshnessPolicy.SESSION_ONLY))
            )
        self.assertEqual(caught.exception.problem, InputProblem.NOT_ALLOWED)
        self.assertEqual(self.rows("SELECT id FROM memory_versions"), [])

    async def test_a_project_memory_needs_a_contributor_of_an_active_project(self):
        project = self.seed_project()
        team = self.seed_team(project)
        created = await self.versioning.create_memory(
            self.actor(team.contributor),
            draft(scope=MemoryScope.PROJECT, project_id=project),
        )
        (row,) = self.versions(created.memory_id)
        self.assertEqual((row.scope, row.project_id), ("project", project))
        self.assertIsNone(row.owner_user_id)
        with self.assertRaises(MemoryPermissionError):
            await self.versioning.create_memory(
                self.actor(team.viewer),
                draft(scope=MemoryScope.PROJECT, project_id=project),
            )
        with self.assertRaises(MemoryPermissionError):
            await self.versioning.create_memory(
                self.user(), draft(scope=MemoryScope.PROJECT, project_id=project)
            )
        archived = self.seed_project(ProjectStatus.ARCHIVED)
        manager = self.seed_manager(archived)
        with self.assertRaises(MemoryPermissionError) as caught:
            await self.versioning.create_memory(
                self.actor(manager),
                draft(scope=MemoryScope.PROJECT, project_id=archived),
            )
        self.assertEqual(caught.exception.reason, "project_state_forbids")

    async def test_the_roles_the_caller_claims_are_not_trusted(self):
        project = self.seed_project()
        outsider = self.user()
        claims = Principal(
            outsider.user_id, SystemRole.USER, {project: ProjectRole.MANAGER}
        )
        with self.assertRaises(MemoryPermissionError):
            await self.versioning.create_memory(
                claims, draft(scope=MemoryScope.PROJECT, project_id=project)
            )

    async def test_the_backend_identity_cannot_write_a_confirmed_memory(self):
        system = Principal(uuid4(), SystemRole.SYSTEM)
        with self.assertRaises(MemoryPermissionError):
            await self.versioning.create_memory(system, draft())


@requires_postgres
class EditTest(PostgresVersioningTestCase):
    async def test_an_edit_is_a_new_version_and_the_old_one_is_kept(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        self.clock.advance(minutes=5)
        edited = await self.versioning.edit_memory(
            me,
            created.memory_id,
            1,
            MemoryChanges(content="deploy backend thursday", reason="moved"),
        )
        self.assertEqual(edited.version_number, 2)
        old, new = self.versions(created.memory_id)
        self.assertEqual(
            (old.status, old.content), ("superseded", "deploy backend friday")
        )
        self.assertEqual(
            (new.status, new.content), ("active", "deploy backend thursday")
        )
        self.assertEqual(new.title, old.title)  # unchanged fields are carried over
        self.assertEqual(new.change_reason, "moved")
        self.assertEqual(new.confirmation_state, "confirmed")
        self.assertEqual(new.actor_user_id, me.user_id)
        self.assertEqual(new.attributes, {"edited_from_version": 1})
        self.assertEqual(self.relations(), [(new.id, old.id, "supersedes", "content")])
        (change,) = self.changes(old.id)
        self.assertEqual(
            (change.old_status, change.new_status), ("active", "superseded")
        )
        self.assertEqual(
            (change.actor_type, change.actor_user_id), ("user", me.user_id)
        )

    async def test_a_retrieval_offers_only_the_edited_version(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        await self.versioning.edit_memory(
            me,
            created.memory_id,
            1,
            MemoryChanges(title="deploy day v2", content="deploy backend friday noon"),
        )
        result = await self.retrieve(me, QUERY)
        self.assertEqual(titles(result), ["deploy day v2"])
        self.assertEqual(result.hits[0].version_number, 2)

    async def test_an_edit_that_changes_nothing_writes_nothing(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        same = await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(title="deploy day")
        )
        self.assertEqual(same.version_number, 1)
        self.assertEqual(len(self.versions(created.memory_id)), 1)
        self.assertEqual(self.relations(), [])

    async def test_an_edit_on_an_old_version_is_a_conflict(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="second")
        )
        with self.assertRaises(MemoryVersionConflictError) as caught:
            await self.versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content="stale edit")
            )
        self.assertEqual(
            (caught.exception.expected_version, caught.exception.current_version),
            (1, 2),
        )
        self.assertEqual(len(self.versions(created.memory_id)), 2)

    async def test_two_concurrent_edits_of_one_version_one_wins(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        other = self.new_versioning()
        results = await asyncio.gather(
            self.versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content="edit A")
            ),
            other.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content="edit B")
            ),
            return_exceptions=True,
        )
        conflicts = [r for r in results if isinstance(r, MemoryVersionConflictError)]
        written = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual((len(conflicts), len(written)), (1, 1), results)
        rows = self.versions(created.memory_id)
        self.assertEqual([r.status for r in rows], ["superseded", "active"])

    async def test_an_edit_of_a_worker_candidate_records_its_confirmation(self):
        me = self.user()
        seeded = self.seed(
            "inferred",
            "deploy backend friday",
            owner=me.user_id,
            confirmation="inferred",
        )
        new = await self.versioning.edit_memory(
            me, seeded.memory_id, 1, MemoryChanges(content="deploy backend friday 10am")
        )
        kinds = sorted(r[2] for r in self.relations())
        self.assertEqual(kinds, ["confirmed_from", "supersedes"])
        self.assertEqual(new.confirmation_state.value, "confirmed")

    async def test_a_freshness_change_is_a_new_version_too(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        self.clock.advance(days=1)
        new = await self.versioning.edit_memory(
            me,
            created.memory_id,
            1,
            MemoryChanges(freshness=FreshnessSpec.expiring(T0 + timedelta(days=30))),
        )
        self.assertEqual(new.freshness_policy.value, "expiring")
        self.assertEqual(new.expires_at, T0 + timedelta(days=30))
        self.assertEqual(self.relations()[0][3], "freshness")
        with self.assertRaises(InvalidMemoryInputError):
            await self.versioning.edit_memory(
                me,
                created.memory_id,
                2,
                MemoryChanges(freshness=FreshnessSpec.repo_commit("c" * 40)),
            )

    async def test_editing_a_revalidate_memory_verifies_it_again(self):
        me = self.user()
        seeded = self.seed(
            "stale fact",
            "deploy backend friday",
            owner=me.user_id,
            freshness="revalidate",
            verified_at=T0 - timedelta(days=100),
            revalidate_after=timedelta(days=90),
            stale_since=T0 - timedelta(days=10),
        )
        new = await self.versioning.edit_memory(
            me, seeded.memory_id, 1, MemoryChanges(content="deploy backend monday")
        )
        self.assertEqual(new.verified_at, T0)
        self.assertIsNone(new.stale_since)
        self.assertEqual(new.revalidate_after, timedelta(days=90))

    async def test_a_deprecated_memory_is_not_edited(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        await self.versioning.deprecate_memory(me, created.memory_id, 1)
        with self.assertRaises(MemoryStateError) as caught:
            await self.versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content="x")
            )
        self.assertEqual(caught.exception.problem, StateProblem.NOT_ACTIVE)

    async def test_an_edit_does_not_carry_a_freshness_a_person_cannot_write(self):
        me = self.user()
        session = self.seed(
            "session note",
            "deploy backend friday",
            owner=me.user_id,
            freshness="session_only",
        )
        expired = self.seed(
            "old window",
            "deploy backend friday",
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0 - timedelta(seconds=1),
        )
        for seeded, field in ((session, "freshness"), (expired, "expires_at")):
            with self.subTest(field):
                with self.assertRaises(InvalidMemoryInputError) as caught:
                    await self.versioning.edit_memory(
                        me, seeded.memory_id, 1, MemoryChanges(title="renamed")
                    )
                self.assertEqual(caught.exception.field, field)
                self.assertEqual(len(self.versions(seeded.memory_id)), 1)
                new = await self.versioning.edit_memory(
                    me,
                    seeded.memory_id,
                    1,
                    MemoryChanges(title="renamed", freshness=FreshnessSpec.permanent()),
                )
                self.assertEqual(new.freshness_policy, FreshnessPolicy.PERMANENT)


@requires_postgres
class AccessTest(PostgresVersioningTestCase):
    async def test_somebody_elses_private_memory_is_not_found(self):
        owner, other = self.user(), self.user()
        created = await self.versioning.create_memory(owner, draft())
        for call in (
            self.versioning.edit_memory(
                other, created.memory_id, 1, MemoryChanges(content="x")
            ),
            self.versioning.deprecate_memory(other, created.memory_id, 1),
            self.versioning.history(other, created.memory_id),
        ):
            with self.assertRaises(MemoryNotFoundError):
                await call
        with self.assertRaises(MemoryNotFoundError):
            await self.versioning.edit_memory(
                owner, uuid4(), 1, MemoryChanges(content="x")
            )
        self.assertEqual(len(self.versions(created.memory_id)), 1)

    async def test_even_an_owner_or_admin_cannot_edit_a_private_memory(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role), self.assertRaises(MemoryNotFoundError):
                await self.versioning.edit_memory(
                    self.user(role), created.memory_id, 1, MemoryChanges(content="x")
                )

    async def test_project_memory_follows_the_role_read_from_the_database(self):
        project = self.seed_project()
        team = self.seed_team(project)
        seeded = self.seed(
            "team rule", "deploy backend friday", scope="project", project=project
        )
        with self.assertRaises(MemoryPermissionError):
            await self.versioning.edit_memory(
                self.actor(team.viewer), seeded.memory_id, 1, MemoryChanges(content="x")
            )
        with self.assertRaises(MemoryNotFoundError):
            await self.versioning.edit_memory(
                self.user(), seeded.memory_id, 1, MemoryChanges(content="x")
            )
        # A viewer may read the history.
        history = await self.versioning.history(
            self.actor(team.viewer), seeded.memory_id
        )
        self.assertEqual([v.version_number for v in history], [1])
        new = await self.versioning.edit_memory(
            self.actor(team.contributor),
            seeded.memory_id,
            1,
            MemoryChanges(content="deploy backend thursday"),
        )
        self.assertEqual((new.scope, new.project_id), (MemoryScope.PROJECT, project))
        self.set_project(project, status="archived")
        with self.assertRaises(MemoryPermissionError):
            await self.versioning.edit_memory(
                self.actor(team.manager),
                seeded.memory_id,
                2,
                MemoryChanges(content="y"),
            )

    async def test_shared_repo_and_group_memories_are_not_changed_here(self):
        me = self.user()
        shared = self.seed("shared", scope="shared")
        with self.assertRaises(MemoryScopeNotSupportedError):
            await self.versioning.edit_memory(
                me, shared.memory_id, 1, MemoryChanges(content="x")
            )
        repo = self.seed("repo", scope="repo", repo=uuid4())
        group = self.seed("group", scope="project_group", group=uuid4())
        for seeded in (repo, group):
            with self.assertRaises(MemoryNotFoundError):
                await self.versioning.edit_memory(
                    me, seeded.memory_id, 1, MemoryChanges(content="x")
                )

    async def test_every_decision_is_audited(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="x")
        )
        with self.assertRaises(MemoryNotFoundError):
            await self.versioning.edit_memory(
                self.user(), created.memory_id, 2, MemoryChanges(content="y")
            )
        self.assertEqual(
            self.audit_actions(),
            [("memory.use", "allow"), ("memory.use", "allow"), ("memory.use", "deny")],
        )

    async def test_a_non_member_cannot_tell_a_closed_project_memory_exists(self):
        outsider = self.user()
        mine = await self.versioning.create_memory(outsider, draft())
        for status in (ProjectStatus.ARCHIVED, ProjectStatus.PENDING_DELETION):
            with self.subTest(status):
                project = self.seed_project(status)
                seeded = self.seed(
                    "team rule",
                    "deploy backend friday",
                    scope="project",
                    project=project,
                )
                memory = seeded.memory_id
                calls = (
                    self.versioning.history(outsider, memory),
                    self.versioning.edit_memory(
                        outsider, memory, 1, MemoryChanges(content="x")
                    ),
                    self.versioning.restore_version(outsider, memory, 1, 1),
                    self.versioning.deprecate_memory(outsider, memory, 1),
                    self.versioning.revalidate_memory(outsider, memory, 1),
                    self.versioning.relate_memories(
                        outsider,
                        ManualRelation.EXTENDS,
                        newer_memory_id=mine.memory_id,
                        newer_expected_version=1,
                        older_memory_id=memory,
                        older_expected_version=1,
                    ),
                )
                for call in calls:
                    with self.assertRaises(MemoryNotFoundError):
                        await call
                self.assertEqual(len(self.versions(memory)), 1)

    async def test_history_shows_an_earlier_audience_only_to_its_readers(self):
        project = self.seed_project()
        team = self.seed_team(project)
        owner = self.actor(team.contributor)
        private = self.seed(
            "private draft",
            "deploy backend friday",
            owner=owner.user_id,
            status="superseded",
        )
        # A later flow (PAW-044) widens the memory by a project-scoped version.
        self.seed(
            "team rule",
            "deploy backend friday",
            scope="project",
            project=project,
            memory_id=private.memory_id,
            version_number=2,
        )
        seen = await self.versioning.history(self.actor(team.viewer), private.memory_id)
        self.assertEqual([v.version_number for v in seen], [2])
        mine = await self.versioning.history(owner, private.memory_id)
        self.assertEqual([v.version_number for v in mine], [1, 2])

    def widened(self):
        """A private version 1 (superseded) and a project-scoped version 2."""
        project = self.seed_project()
        team = self.seed_team(project)
        owner = self.actor(team.contributor)
        private = self.seed(
            "private draft",
            "owner only secret",
            owner=owner.user_id,
            status="superseded",
        )
        self.seed(
            "team rule",
            "deploy backend friday",
            scope="project",
            project=project,
            memory_id=private.memory_id,
            version_number=2,
        )
        return team, owner, private

    async def test_the_earlier_private_version_never_leaves_the_database(self):
        # memory/acl.py: every read of memory_versions applies the ACL in SQL, so a
        # reader of the project never receives the owner's private version, not
        # even to filter it out afterwards.
        team, owner, private = self.widened()
        for reader in (team.viewer, team.manager):
            with self.subTest(reader):
                seen, values = await self.returned_values(
                    lambda service, reader=reader: service.history(
                        self.actor(reader), private.memory_id
                    )
                )
                self.assertEqual([v.version_number for v in seen], [2])
                self.assertIn("team rule", values)
                self.assertNotIn("private draft", values)
                self.assertNotIn("owner only secret", values)
        # The owner reads both, through the same filtered query.
        mine, values = await self.returned_values(
            lambda service: service.history(owner, private.memory_id)
        )
        self.assertEqual([v.version_number for v in mine], [1, 2])
        self.assertIn("owner only secret", values)

    async def test_restoring_a_private_version_does_not_read_its_content(self):
        team, _owner, private = self.widened()
        for writer in (team.contributor, team.manager):
            with self.subTest(writer):
                outcome, values = await self.returned_values(
                    lambda service, writer=writer: service.restore_version(
                        self.actor(writer), private.memory_id, 2, 1
                    )
                )
                self.assertIsInstance(outcome, MemoryStateError)
                self.assertEqual(outcome.problem, StateProblem.SCOPE_MISMATCH)
                self.assertNotIn("private draft", values)
                self.assertNotIn("owner only secret", values)
        self.assertEqual(len(self.versions(private.memory_id)), 2)

    async def test_a_denied_reader_receives_no_content_at_all(self):
        owner, other = self.user(), self.user()
        created = await self.versioning.create_memory(
            owner, draft(title="mine only", content="owner only secret")
        )
        calls = (
            lambda service: service.history(other, created.memory_id),
            lambda service: service.edit_memory(
                other, created.memory_id, 1, MemoryChanges(content="x")
            ),
            lambda service: service.restore_version(other, created.memory_id, 1, 1),
            lambda service: service.deprecate_memory(other, created.memory_id, 1),
            lambda service: service.revalidate_memory(other, created.memory_id, 1),
        )
        for call in calls:
            outcome, values = await self.returned_values(call)
            self.assertIsInstance(outcome, MemoryNotFoundError)
            self.assertNotIn("mine only", values)
            self.assertNotIn("owner only secret", values)


@requires_postgres
class RestoreDeprecateRevalidateTest(PostgresVersioningTestCase):
    async def test_a_restore_writes_a_new_version_from_the_old_content(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="v2")
        )
        restored = await self.versioning.restore_version(me, created.memory_id, 2, 1)
        self.assertEqual(restored.version_number, 3)
        v1, v2, v3 = self.versions(created.memory_id)
        self.assertEqual(
            [v.status for v in (v1, v2, v3)], ["superseded", "superseded", "active"]
        )
        self.assertEqual(v3.content, v1.content)
        self.assertEqual(v3.attributes, {"restored_from_version": 1})
        self.assertIn((v3.id, v2.id, "supersedes", "restore"), self.relations())

    async def test_a_deprecated_memory_can_be_restored_and_stays_in_history(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        deprecated = await self.versioning.deprecate_memory(me, created.memory_id, 1)
        self.assertEqual(deprecated.status.value, "deprecated")
        self.assertEqual(titles(await self.retrieve(me, QUERY)), [])
        restored = await self.versioning.restore_version(me, created.memory_id, 1, 1)
        v1, v2 = self.versions(created.memory_id)
        self.assertEqual((v1.status, v2.status), ("deprecated", "active"))
        self.assertEqual(restored.version_number, 2)
        self.assertEqual(titles(await self.retrieve(me, QUERY)), ["deploy day"])

    async def test_restoring_an_expired_version_needs_a_new_freshness(self):
        me = self.user()
        created = await self.versioning.create_memory(
            me, draft(freshness=FreshnessSpec.expiring(T0 + timedelta(days=1)))
        )
        await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="v2")
        )
        self.clock.advance(days=2)
        with self.assertRaises(InvalidMemoryInputError) as caught:
            await self.versioning.restore_version(me, created.memory_id, 2, 1)
        self.assertEqual(caught.exception.field, "expires_at")
        restored = await self.versioning.restore_version(
            me, created.memory_id, 2, 1, freshness=FreshnessSpec.permanent()
        )
        self.assertEqual(restored.freshness_policy.value, "permanent")

    async def test_a_restore_of_an_unknown_version_changes_nothing(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        with self.assertRaises(MemoryStateError) as caught:
            await self.versioning.restore_version(me, created.memory_id, 1, 7)
        self.assertEqual(caught.exception.problem, StateProblem.UNKNOWN_VERSION)

    async def test_revalidating_a_stale_candidate_writes_a_verified_version(self):
        me = self.user()
        seeded = self.seed(
            "main model",
            "deploy backend friday",
            owner=me.user_id,
            freshness="revalidate",
            verified_at=T0 - timedelta(days=100),
            revalidate_after=timedelta(days=90),
            stale_since=T0 - timedelta(days=10),
        )
        before = await self.retrieve(me, QUERY)
        self.assertEqual(before.hits[0].freshness.value, "stale")
        new = await self.versioning.revalidate_memory(me, seeded.memory_id, 1)
        self.assertEqual(new.version_number, 2)
        self.assertEqual(new.verified_at, T0)
        self.assertIsNone(new.stale_since)
        self.assertEqual(new.content, "deploy backend friday")
        kinds = sorted(r[2] for r in self.relations())
        self.assertEqual(kinds, ["revalidated_from", "supersedes"])
        after = await self.retrieve(me, QUERY)
        self.assertEqual(
            [(h.version_number, h.freshness.value) for h in after.hits], [(2, "fresh")]
        )

    async def test_only_a_revalidate_memory_is_revalidated(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        with self.assertRaises(MemoryStateError) as caught:
            await self.versioning.revalidate_memory(me, created.memory_id, 1)
        self.assertEqual(caught.exception.problem, StateProblem.NOT_REVALIDATABLE)


@requires_postgres
class RelationTest(PostgresVersioningTestCase):
    async def two(self, me, **second):
        first = await self.versioning.create_memory(
            me, draft(title="old rule", content="deploy backend friday old")
        )
        values = {"title": "new rule", "content": "deploy backend friday new"}
        values.update(second)
        newer = await self.versioning.create_memory(me, draft(**values))
        return newer, first

    async def relate(self, me, relation, newer, older, **options):
        return await self.versioning.relate_memories(
            me,
            relation,
            newer_memory_id=newer.memory_id,
            newer_expected_version=options.pop("newer_version", 1),
            older_memory_id=older.memory_id,
            older_expected_version=options.pop("older_version", 1),
            **options,
        )

    async def test_supersedes_retires_the_older_memory(self):
        me = self.user()
        newer, older = await self.two(me)
        edge = await self.relate(
            me, ManualRelation.SUPERSEDES, newer, older, reason="replaced"
        )
        self.assertEqual(
            (edge.from_version_id, edge.to_version_id, edge.relation.value),
            (newer.version_id, older.version_id, "supersedes"),
        )
        (old,) = self.versions(older.memory_id)
        self.assertEqual(old.status, "superseded")
        self.assertEqual(titles(await self.retrieve(me, QUERY)), ["new rule"])

    async def test_extends_and_conflicts_keep_both_memories_active(self):
        me = self.user()
        newer, older = await self.two(me)
        await self.relate(me, ManualRelation.EXTENDS, newer, older)
        third = await self.versioning.create_memory(
            me, draft(title="other rule", content="deploy backend friday never")
        )
        await self.relate(me, ManualRelation.CONFLICTS_WITH, third, older)
        statuses = {
            row.title: row.status
            for row in self.rows("SELECT title, status FROM memory_versions")
        }
        self.assertEqual(set(statuses.values()), {"active"})
        result = await self.retrieve(me, QUERY)
        self.assertEqual(len(result.conflicts), 1)

    async def test_supersedes_across_audiences_is_refused(self):
        project = self.seed_project()
        member = self.actor(self.seed_member(project))
        mine = await self.versioning.create_memory(member, draft())
        team = await self.versioning.create_memory(
            member, draft(scope=MemoryScope.PROJECT, project_id=project)
        )
        with self.assertRaises(MemoryStateError) as caught:
            await self.relate(member, ManualRelation.SUPERSEDES, mine, team)
        self.assertEqual(caught.exception.problem, StateProblem.SCOPE_MISMATCH)
        self.assertEqual(self.versions(team.memory_id)[0].status, "active")

    async def test_a_cycle_and_a_repeated_relation_are_refused(self):
        me = self.user()
        newer, older = await self.two(me)
        await self.relate(me, ManualRelation.EXTENDS, newer, older)
        with self.assertRaises(MemoryStateError) as caught:
            await self.relate(me, ManualRelation.EXTENDS, older, newer)
        self.assertEqual(caught.exception.problem, StateProblem.WOULD_CYCLE)
        with self.assertRaises(MemoryStateError) as caught:
            await self.relate(me, ManualRelation.EXTENDS, newer, older)
        self.assertEqual(caught.exception.problem, StateProblem.ALREADY_RELATED)
        await self.relate(me, ManualRelation.CONFLICTS_WITH, newer, older)
        with self.assertRaises(MemoryStateError) as caught:
            await self.relate(me, ManualRelation.CONFLICTS_WITH, older, newer)
        self.assertEqual(caught.exception.problem, StateProblem.ALREADY_RELATED)

    async def test_a_relation_needs_both_memories_and_their_current_versions(self):
        me, other = self.user(), self.user()
        mine = await self.versioning.create_memory(me, draft())
        theirs = await self.versioning.create_memory(other, draft())
        with self.assertRaises(MemoryNotFoundError):
            await self.relate(me, ManualRelation.CONFLICTS_WITH, mine, theirs)
        newer, older = await self.two(me)
        with self.assertRaises(MemoryVersionConflictError):
            await self.relate(me, ManualRelation.EXTENDS, newer, older, older_version=2)
        with self.assertRaises(MemoryStateError) as caught:
            await self.relate(me, ManualRelation.EXTENDS, newer, newer)
        self.assertEqual(caught.exception.problem, StateProblem.SAME_MEMORY)
        self.assertEqual(self.relations(), [])

    async def test_acyclic_relations_are_recorded_one_at_a_time(self):
        me = self.user()
        newer, older = await self.two(me)
        quick = self.new_versioning(lock_timeout_ms=50)
        with self.engine.connect() as holder, holder.begin():
            # Another relation of two other memories is checking the graph.
            holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": RELATION_GRAPH_LOCK_KEY},
            )
            for relation in (ManualRelation.EXTENDS, ManualRelation.SUPERSEDES):
                with self.subTest(relation), self.assertRaises(MemoryBusyError):
                    await quick.relate_memories(
                        me,
                        relation,
                        newer_memory_id=newer.memory_id,
                        newer_expected_version=1,
                        older_memory_id=older.memory_id,
                        older_expected_version=1,
                    )
            # A conflict is not part of the acyclic graph, nor is an edit.
            await self.relate(me, ManualRelation.CONFLICTS_WITH, newer, older)
            await quick.edit_memory(
                me, newer.memory_id, 1, MemoryChanges(content="deploy friday v2")
            )
        self.assertEqual(
            [r[2] for r in self.relations()], ["conflicts_with", "supersedes"]
        )

    async def test_relations_of_disjoint_pairs_do_not_close_a_cycle_together(self):
        me = self.user()
        a, b, c, d = [
            await self.versioning.create_memory(
                me, draft(title=f"rule {name}", content=f"deploy {name}")
            )
            for name in "abcd"
        ]
        await self.relate(me, ManualRelation.EXTENDS, a, b)
        await self.relate(me, ManualRelation.EXTENDS, c, d)
        other = self.new_versioning()
        results = await asyncio.gather(
            self.relate(me, ManualRelation.EXTENDS, b, c),
            other.relate_memories(
                me,
                ManualRelation.EXTENDS,
                newer_memory_id=d.memory_id,
                newer_expected_version=1,
                older_memory_id=a.memory_id,
                older_expected_version=1,
            ),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, MemoryStateError)]
        self.assertEqual(len(refused), 1, results)
        self.assertEqual(refused[0].problem, StateProblem.WOULD_CYCLE)
        self.assertEqual(len(self.relations()), 3)
