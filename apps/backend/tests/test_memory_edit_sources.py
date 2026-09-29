"""Sources of a person's new version, and the lookup for deletion (Issue #128).

Decision 0045: an edit (and a restore or a revalidation) copies the earlier
version's ``memory_sources`` to the new version and adds a ``user_confirmation``
source of the person; ``MemoryDerivation`` finds every version derived from a
conversation or a Task, through the copies and through the lineage of a person's
versions (``edited_from_version`` / ``revalidated_from_version`` /
``restored_from_version``), so that a version written before the copies existed is
found too.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.authz import SystemRole
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.versioning import (
    FreshnessSpec,
    InvalidMemoryInputError,
    MemoryChanges,
    MemoryDerivation,
    MemoryDraft,
    MemoryPermissionError,
    RevalidateTrigger,
    confirmation_source_ref,
)

from .versioning_support import PostgresVersioningTestCase, requires_postgres


def draft(**overrides):
    values = {
        "scope": MemoryScope.USER,
        "memory_type": "preference",
        "title": "deploy day",
        "content": "deploy backend friday",
    }
    values.update(overrides)
    return MemoryDraft(**values)


class SourcesTestCase(PostgresVersioningTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.derivation = MemoryDerivation(self._database())

    def conversation(self, owner) -> object:
        conversation_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO conversations (id, owner_user_id) VALUES (:c, :o)"),
                {"c": conversation_id, "o": owner.user_id},
            )
        return conversation_id

    def message(self, conversation_id) -> object:
        message_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, turn_id,"
                    " event_sequence, role, content)"
                    " VALUES (:m, :c, :t, 0, 'user', 'deploy on friday')"
                ),
                {"m": message_id, "c": conversation_id, "t": uuid4()},
            )
        return message_id

    def add_source(
        self,
        version_id,
        kind="conversation",
        *,
        conversation=None,
        message=None,
        ref=None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_sources (memory_version_id, source_type,"
                    " conversation_id, message_id, source_ref)"
                    " VALUES (:v, :k, :c, :m, :r)"
                ),
                {"v": version_id, "k": kind, "c": conversation, "m": message, "r": ref},
            )

    def sources(self, version_id) -> list[tuple]:
        return sorted(
            (
                (row.source_type, row.conversation_id, row.message_id, row.source_ref)
                for row in self.rows(
                    "SELECT * FROM memory_sources WHERE memory_version_id = :v",
                    v=version_id,
                )
            ),
            key=repr,
        )

    def all_sources(self) -> list:
        return self.rows("SELECT * FROM memory_sources ORDER BY id")

    @staticmethod
    def confirmation(actor) -> tuple:
        return ("user_confirmation", None, None, confirmation_source_ref(actor.user_id))


@requires_postgres
class EditCopiesSourcesTest(SourcesTestCase):
    async def test_an_edit_copies_the_sources_and_adds_the_editor(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        talk = self.conversation(me)
        said = self.message(talk)
        task = uuid4()
        self.add_source(created.version_id, conversation=talk)
        self.add_source(created.version_id, conversation=talk, message=said)
        self.add_source(created.version_id, "task", ref=str(task))
        before = self.sources(created.version_id)
        edited = await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="deploy backend monday")
        )
        self.assertEqual(
            self.sources(edited.version_id),
            sorted([*before, self.confirmation(me)], key=repr),
        )
        # The old version keeps its own sources, unchanged.
        self.assertEqual(self.sources(created.version_id), before)

    async def test_a_copied_source_keeps_when_it_was_recorded(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        self.add_source(created.version_id, conversation=self.conversation(me))
        (original,) = self.rows(
            "SELECT created_at FROM memory_sources WHERE memory_version_id = :v",
            v=created.version_id,
        )
        self.clock.advance(days=3)
        edited = await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(title="deploy weekday")
        )
        rows = {
            row.source_type: row.created_at
            for row in self.rows(
                "SELECT * FROM memory_sources WHERE memory_version_id = :v",
                v=edited.version_id,
            )
        }
        self.assertEqual(rows["conversation"], original.created_at)
        self.assertEqual(rows["user_confirmation"], self.clock())

    async def test_an_edit_that_changes_nothing_writes_no_source(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        self.add_source(created.version_id, conversation=self.conversation(me))
        before = self.all_sources()
        await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="deploy backend friday")
        )
        self.assertEqual(self.all_sources(), before)

    async def test_a_chain_of_edits_keeps_one_confirmation_per_person(self):
        project = self.seed_project()
        team = self.seed_team(project)
        alice, bob = self.actor(team.contributor), self.actor(team.manager)
        created = await self.versioning.create_memory(
            alice, draft(scope=MemoryScope.PROJECT, project_id=project)
        )
        talk = self.conversation(alice)
        self.add_source(created.version_id, conversation=talk)
        v2 = await self.versioning.edit_memory(
            alice, created.memory_id, 1, MemoryChanges(content="monday")
        )
        v3 = await self.versioning.edit_memory(
            alice, created.memory_id, 2, MemoryChanges(content="tuesday")
        )
        v4 = await self.versioning.edit_memory(
            bob, created.memory_id, 3, MemoryChanges(content="wednesday")
        )
        conversation = ("conversation", talk, None, None)
        self.assertEqual(
            self.sources(v2.version_id),
            sorted([conversation, self.confirmation(alice)], key=repr),
        )
        self.assertEqual(self.sources(v3.version_id), self.sources(v2.version_id))
        self.assertEqual(
            self.sources(v4.version_id),
            sorted(
                [conversation, self.confirmation(alice), self.confirmation(bob)],
                key=repr,
            ),
        )

    async def test_narrowing_to_the_editor_keeps_the_sources(self):
        project = self.seed_project()
        team = self.seed_team(project)
        me = self.actor(team.contributor)
        created = await self.versioning.create_memory(
            me, draft(scope=MemoryScope.PROJECT, project_id=project)
        )
        talk = self.conversation(me)
        self.add_source(created.version_id, conversation=talk)
        narrowed = await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(scope=MemoryScope.USER)
        )
        self.assertEqual(narrowed.scope, MemoryScope.USER)
        self.assertEqual(
            self.sources(narrowed.version_id),
            sorted(
                [("conversation", talk, None, None), self.confirmation(me)], key=repr
            ),
        )

    async def test_a_source_of_a_deleted_conversation_is_not_copied(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        gone, kept = self.conversation(me), self.conversation(me)
        self.add_source(created.version_id, conversation=gone)
        self.add_source(created.version_id, conversation=kept)
        with self.engine.begin() as connection:
            # What the deletion flow leaves: the ids cleared, the loss recorded.
            connection.execute(
                text(
                    "UPDATE memory_sources SET source_deleted_at = now()"
                    " WHERE conversation_id = :c"
                ),
                {"c": gone},
            )
            connection.execute(
                text("DELETE FROM conversations WHERE id = :c"), {"c": gone}
            )
        before = self.sources(created.version_id)
        self.assertIn(("conversation", None, None, None), before)
        edited = await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="monday")
        )
        self.assertEqual(
            self.sources(edited.version_id),
            sorted(
                [("conversation", kept, None, None), self.confirmation(me)], key=repr
            ),
        )
        self.assertEqual(self.sources(created.version_id), before)

    async def test_a_revalidation_copies_the_sources(self):
        me = self.user()
        spec = FreshnessSpec.revalidate(
            timedelta(days=90), (RevalidateTrigger.MODEL_CHANGED,)
        )
        created = await self.versioning.create_memory(me, draft(freshness=spec))
        talk = self.conversation(me)
        self.add_source(created.version_id, conversation=talk)
        again = await self.versioning.revalidate_memory(me, created.memory_id, 1)
        self.assertEqual(
            self.sources(again.version_id),
            sorted(
                [("conversation", talk, None, None), self.confirmation(me)], key=repr
            ),
        )

    async def test_a_restore_copies_the_sources_of_the_restored_version(self):
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        first = self.conversation(me)
        self.add_source(created.version_id, conversation=first)
        v2 = await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="monday")
        )
        second = self.conversation(me)
        self.add_source(v2.version_id, conversation=second)
        restored = await self.versioning.restore_version(me, created.memory_id, 2, 1)
        # Version 1's content, so version 1's sources (not version 2's).
        self.assertEqual(
            self.sources(restored.version_id),
            sorted(
                [("conversation", first, None, None), self.confirmation(me)], key=repr
            ),
        )

    async def test_a_person_s_version_is_never_ended_with_the_session(self):
        # Decision 0045 pt 1 (session_only): the copied sources would let
        # end_session / end_task reach a person's version only if it were
        # session_only, and a person never writes session_only (Decision 0034 pt 4).
        me = self.user()
        created = await self.versioning.create_memory(me, draft())
        talk = self.conversation(me)
        with self.engine.begin() as connection:
            # A session_only memory as a worker leaves it (not writable by a person).
            connection.execute(
                text(
                    "UPDATE memory_versions SET freshness_policy = 'session_only'"
                    " WHERE id = :v"
                ),
                {"v": created.version_id},
            )
        self.add_source(created.version_id, conversation=talk)
        with self.assertRaises(InvalidMemoryInputError):
            await self.versioning.edit_memory(
                me, created.memory_id, 1, MemoryChanges(content="monday")
            )
        edited = await self.versioning.edit_memory(
            me,
            created.memory_id,
            1,
            MemoryChanges(content="monday", freshness=FreshnessSpec.permanent()),
        )
        self.assertIn(
            ("conversation", talk, None, None), self.sources(edited.version_id)
        )
        freshness = self.new_freshness()
        self.assertEqual(await freshness.end_session(talk), 0)
        self.assertEqual(
            [
                (v.version_number, v.status, v.freshness_policy)
                for v in self.versions(created.memory_id)
            ],
            [(1, "superseded", "session_only"), (2, "active", "permanent")],
        )

    async def test_the_system_identity_writes_no_source(self):
        project = self.seed_project()
        team = self.seed_team(project)
        contributor = self.actor(team.contributor)
        system = self.actor(team.contributor, SystemRole.SYSTEM)
        spec = FreshnessSpec.revalidate(
            timedelta(days=90), (RevalidateTrigger.MODEL_CHANGED,)
        )
        created = await self.versioning.create_memory(
            contributor,
            draft(scope=MemoryScope.PROJECT, project_id=project, freshness=spec),
        )
        self.add_source(created.version_id, conversation=self.conversation(contributor))
        before = (self.all_sources(), self.versions(created.memory_id))
        self.sink.events.clear()
        for call in (
            self.versioning.edit_memory(
                system, created.memory_id, 1, MemoryChanges(content="monday")
            ),
            self.versioning.restore_version(system, created.memory_id, 1, 1),
            self.versioning.revalidate_memory(system, created.memory_id, 1),
        ):
            with self.assertRaises(MemoryPermissionError) as caught:
                await call
            self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual((self.all_sources(), self.versions(created.memory_id)), before)
        self.assertEqual(self.sink.events, [])


@requires_postgres
class DerivationTest(SourcesTestCase):
    async def test_every_version_of_an_edit_chain_is_found_from_the_conversation(self):
        me, other = self.user(), self.user()
        talk, elsewhere = self.conversation(me), self.conversation(other)
        created = await self.versioning.create_memory(me, draft())
        self.add_source(created.version_id, conversation=talk)
        chain = [created]
        for number, content in enumerate(("monday", "tuesday", "wednesday"), 1):
            chain.append(
                await self.versioning.edit_memory(
                    me, created.memory_id, number, MemoryChanges(content=content)
                )
            )
        # Another memory of the same person from another conversation, and
        # another person's memory from theirs: not found, their sources untouched.
        unrelated = await self.versioning.create_memory(me, draft(title="other"))
        self.add_source(unrelated.version_id, conversation=elsewhere)
        theirs = await self.versioning.create_memory(other, draft(title="theirs"))
        self.add_source(theirs.version_id, conversation=elsewhere)
        untouched = (
            self.sources(unrelated.version_id),
            self.sources(theirs.version_id),
        )
        found = await self.derivation.versions_from_conversation(talk)
        self.assertEqual(
            [(f.memory_id, f.version_id, f.version_number) for f in found],
            [(v.memory_id, v.version_id, v.version_number) for v in chain],
        )
        self.assertTrue(all(f.has_source for f in found))
        self.assertEqual(
            (self.sources(unrelated.version_id), self.sources(theirs.version_id)),
            untouched,
        )
        found_elsewhere = await self.derivation.versions_from_conversation(elsewhere)
        self.assertEqual(
            {f.version_id for f in found_elsewhere},
            {unrelated.version_id, theirs.version_id},
        )

    async def test_a_message_source_names_its_conversation(self):
        me = self.user()
        talk = self.conversation(me)
        created = await self.versioning.create_memory(me, draft())
        self.add_source(
            created.version_id, conversation=talk, message=self.message(talk)
        )
        found = await self.derivation.versions_from_conversation(talk)
        self.assertEqual([f.version_id for f in found], [created.version_id])

    async def test_versions_of_every_scope_are_found(self):
        project = self.seed_project()
        team = self.seed_team(project)
        me = self.actor(team.contributor)
        talk = self.conversation(me)
        mine = await self.versioning.create_memory(me, draft())
        ours = await self.versioning.create_memory(
            me, draft(scope=MemoryScope.PROJECT, project_id=project)
        )
        for created in (mine, ours):
            self.add_source(created.version_id, conversation=talk)
        narrowed = await self.versioning.edit_memory(
            me, ours.memory_id, 1, MemoryChanges(scope=MemoryScope.USER)
        )
        found = {
            f.version_id for f in await self.derivation.versions_from_conversation(talk)
        }
        self.assertEqual(found, {mine.version_id, ours.version_id, narrowed.version_id})

    async def test_a_version_written_before_the_copies_is_found_by_its_lineage(self):
        me = self.user()
        talk = self.conversation(me)
        # As PR #122 wrote them: an edited version without the copied sources.
        v1 = self.seed("deploy day", owner=me.user_id, status="superseded")
        self.add_source(v1.version_id, conversation=talk)
        v2 = self.seed(
            "deploy day",
            owner=me.user_id,
            memory_id=v1.memory_id,
            version_number=2,
            attributes={"edited_from_version": 1},
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE memory_versions SET actor_type = 'user',"
                    " actor_user_id = :u WHERE id = :v"
                ),
                {"u": me.user_id, "v": v2.version_id},
            )
        # A later edit through the service copies v2's sources (none) only.
        v3 = await self.versioning.edit_memory(
            me, v1.memory_id, 2, MemoryChanges(content="monday")
        )
        self.assertEqual(self.sources(v3.version_id), [self.confirmation(me)])
        found = await self.derivation.versions_from_conversation(talk)
        self.assertEqual(
            [(f.version_number, f.has_source) for f in found],
            [(1, True), (2, False), (3, False)],
        )

    async def test_the_lineage_of_a_worker_version_is_not_followed(self):
        me = self.user()
        talk = self.conversation(me)
        v1 = self.seed("deploy day", owner=me.user_id, status="superseded")
        self.add_source(v1.version_id, conversation=talk)
        # Written by the system actor (``seed``), not by a person.
        self.seed(
            "deploy day",
            owner=me.user_id,
            memory_id=v1.memory_id,
            version_number=2,
            attributes={"edited_from_version": 1},
        )
        found = await self.derivation.versions_from_conversation(talk)
        self.assertEqual([f.version_id for f in found], [v1.version_id])

    async def test_a_restored_version_is_found_from_the_restored_ones_source(self):
        me = self.user()
        talk = self.conversation(me)
        created = await self.versioning.create_memory(me, draft())
        self.add_source(created.version_id, conversation=talk)
        await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="monday")
        )
        restored = await self.versioning.restore_version(me, created.memory_id, 2, 1)
        found = await self.derivation.versions_from_conversation(talk)
        self.assertEqual([f.version_number for f in found], [1, 2, 3])
        self.assertIn(restored.version_id, {f.version_id for f in found})

    async def test_a_task_is_named_by_the_canonical_text_of_its_id(self):
        me = self.user()
        task = uuid4()
        created = await self.versioning.create_memory(me, draft())
        self.add_source(created.version_id, "task", ref=str(task))
        edited = await self.versioning.edit_memory(
            me, created.memory_id, 1, MemoryChanges(content="monday")
        )
        near = await self.versioning.create_memory(me, draft(title="near miss"))
        self.add_source(near.version_id, "task", ref=str(task).upper())
        analysis = await self.versioning.create_memory(me, draft(title="analysis"))
        self.add_source(analysis.version_id, "repo_analysis", ref=str(task))
        found = await self.derivation.versions_from_task(task)
        self.assertEqual(
            [f.version_id for f in found], [created.version_id, edited.version_id]
        )
        self.assertEqual(await self.derivation.versions_from_conversation(task), ())

    async def test_nothing_is_found_for_an_unknown_source(self):
        self.assertEqual(await self.derivation.versions_from_conversation(uuid4()), ())
        self.assertEqual(await self.derivation.versions_from_task(uuid4()), ())
