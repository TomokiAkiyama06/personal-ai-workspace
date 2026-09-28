"""The backend's own ``system`` identity never uses the versioning service (PAW-042).

Decision 0034 point 4: only a person edits memory manually, and every version this
service writes is that person's ``confirmed`` change. A ``Principal`` with
``SystemRole.SYSTEM`` is refused by every public method BEFORE the Authorizer and
the database (as ``ProjectService`` and ``RepositoryService`` refuse it), even when
it carries the id of an active Contributor of an active project: the service reads
the project role from the database, so without this check that id alone would let
the system identity write a project memory as the Contributor (Codex P2 on #122).

The refusal is an identity check, not a policy decision, so like those services it
writes no audit event. The freshness jobs, which run as the ``system`` actor
without a ``Principal``, are not affected.
"""

from datetime import timedelta
from uuid import uuid4

from paw_backend.authz import SystemRole
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.versioning import (
    FreshnessSpec,
    ManualRelation,
    MemoryChanges,
    MemoryDraft,
    MemoryPermissionError,
    RevalidateTrigger,
)

from .versioning_support import T0, PostgresVersioningTestCase, requires_postgres


def draft(**overrides):
    values = {
        "scope": MemoryScope.PROJECT,
        "memory_type": "preference",
        "title": "deploy day",
        "content": "deploy backend friday",
    }
    values.update(overrides)
    return MemoryDraft(**values)


@requires_postgres
class SystemPrincipalTest(PostgresVersioningTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.project = self.seed_project()
        self.team = self.seed_team(self.project)
        self.contributor = self.actor(self.team.contributor)
        # The backend's identity with the id of an active Contributor.
        self.system = self.actor(self.team.contributor, SystemRole.SYSTEM)
        spec = FreshnessSpec.revalidate(
            timedelta(days=90), (RevalidateTrigger.MODEL_CHANGED,)
        )
        self.newer = await self.versioning.create_memory(
            self.contributor,
            draft(project_id=self.project, title="new rule", freshness=spec),
        )
        self.older = await self.versioning.create_memory(
            self.contributor, draft(project_id=self.project, title="old rule")
        )
        self.sink.events.clear()

    def calls(self, actor):
        v = self.versioning
        newer, older = self.newer.memory_id, self.older.memory_id
        return {
            "create_memory": lambda: v.create_memory(
                actor, draft(project_id=self.project)
            ),
            "edit_memory": lambda: v.edit_memory(
                actor, newer, 1, MemoryChanges(content="deploy backend monday")
            ),
            "restore_version": lambda: v.restore_version(actor, newer, 1, 1),
            "deprecate_memory": lambda: v.deprecate_memory(actor, newer, 1),
            "revalidate_memory": lambda: v.revalidate_memory(actor, newer, 1),
            "relate_memories": lambda: v.relate_memories(
                actor,
                ManualRelation.EXTENDS,
                newer_memory_id=newer,
                newer_expected_version=1,
                older_memory_id=older,
                older_expected_version=1,
            ),
            "history": lambda: v.history(actor, newer),
        }

    def snapshot(self):
        return (
            self.rows("SELECT * FROM memory_versions ORDER BY id"),
            self.rows("SELECT * FROM memory_relations ORDER BY id"),
            self.rows("SELECT * FROM memory_metadata_changes ORDER BY id"),
        )

    async def test_every_method_refuses_the_system_identity_of_a_contributor(self):
        before = self.snapshot()
        for name, call in self.calls(self.system).items():
            with self.subTest(method=name):
                with self.assertRaises(MemoryPermissionError) as caught:
                    await call()
                self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual(self.snapshot(), before)
        # Refused before the Authorizer, as ProjectService / RepositoryService do.
        self.assertEqual(self.sink.events, [])

    async def test_the_system_identity_is_refused_for_its_own_private_memory(self):
        system = self.actor(self.seed_user(), SystemRole.SYSTEM)
        seeded = self.seed("its own", "deploy backend friday", owner=system.user_id)
        before = self.snapshot()
        for call in (
            self.versioning.create_memory(system, draft(scope=MemoryScope.USER)),
            self.versioning.edit_memory(
                system, seeded.memory_id, 1, MemoryChanges(content="x")
            ),
            self.versioning.history(system, seeded.memory_id),
        ):
            with self.assertRaises(MemoryPermissionError) as caught:
                await call
            self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sink.events, [])

    async def test_the_identity_is_checked_before_the_arguments(self):
        with self.assertRaises(MemoryPermissionError):
            await self.versioning.edit_memory(self.system, uuid4(), 0, None)
        self.assertEqual(self.sink.events, [])

    async def test_the_same_calls_are_allowed_to_the_contributor_and_audited(self):
        me, newer = self.contributor, self.newer.memory_id
        await self.versioning.history(me, newer)
        await self.versioning.relate_memories(
            me,
            ManualRelation.EXTENDS,
            newer_memory_id=newer,
            newer_expected_version=1,
            older_memory_id=self.older.memory_id,
            older_expected_version=1,
        )
        edited = await self.versioning.edit_memory(
            me, newer, 1, MemoryChanges(content="deploy backend monday")
        )
        self.assertEqual((edited.version_number, edited.actor_user_id), (2, me.user_id))
        self.assertTrue(self.sink.events)
        self.assertTrue(all(e.decision == "allow" for e in self.sink.events))

    async def test_the_freshness_jobs_still_run_as_the_system_actor(self):
        self.clock.advance(days=90)
        self.assertEqual(await self.freshness.mark_revalidation_due(), 1)
        (row,) = self.versions(self.newer.memory_id)
        self.assertEqual(row.stale_since, T0 + timedelta(days=90))
        change = self.changes(row.id)[-1]
        self.assertEqual((change.actor_type, change.actor_user_id), ("system", None))
