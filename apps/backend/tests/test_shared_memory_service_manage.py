"""Owner / Admin: create, edit, delete, restore Shared Memory (real PostgreSQL).

Edit, delete and restore go through the rule functions of ``lifecycle.py``, so
these tests fail while those are stubs. Creation, the permission checks and the
audit trail are implemented in the service and pass without them. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

from paw_backend.authz import Authorizer
from paw_backend.memory.shared import (
    InputProblem,
    InvalidSharedMemoryInputError,
    SharedMemory,
    SharedMemoryBusyError,
    SharedMemoryChanges,
    SharedMemoryNotFoundError,
    SharedMemoryPermissionError,
    SharedMemoryStateError,
    SharedMemoryStatus,
    SharedMemoryVersionConflictError,
    StateProblem,
    memory_lock_key,
)

from .authz_support import SECRET, FailingSink
from .shared_memory_support import (
    T0,
    AsyncPostgresSharedTestCase,
    draft,
    raise_unexpected,
    requires_postgres,
)


@requires_postgres
class CreateMemoryTest(AsyncPostgresSharedTestCase):
    async def test_owner_and_admin_can_create(self):
        for who in (self.owner, self.admin):
            with self.subTest(role=who.system_role.value):
                created = await self.service.create_memory(
                    who, draft(title=f"By {who.system_role.value}")
                )
                self.assertEqual(created.title, f"By {who.system_role.value}")

    async def test_the_result_describes_the_new_memory(self):
        created = await self.service.create_memory(
            self.owner,
            draft(
                memory_type="team_rule",
                title="Use tabs",
                content="Use tabs.\nAlways.",
                importance=70,
                policy_subjects=["style", "a.b"],
                reason="Initial",
            ),
        )
        (version,) = self.versions(created.memory_id)
        self.assertEqual(
            created,
            SharedMemory(
                memory_id=created.memory_id,
                version_id=version["id"],
                version_number=1,
                memory_type="team_rule",
                title="Use tabs",
                content="Use tabs.\nAlways.",
                importance=70,
                policy_subjects=("a.b", "style"),
                status=SharedMemoryStatus.ACTIVE,
                created_at=T0,
                updated_at=T0,
            ),
        )

    async def test_the_stored_rows_are_a_confirmed_permanent_shared_version_one(self):
        created = await self.service.create_memory(
            self.admin,
            draft(policy_subjects=["b", "a"], reason="Initial", importance=0),
        )
        self.assertEqual(self.count("memories"), 1)
        (version,) = self.versions(created.memory_id)
        self.assertEqual(
            {
                key: version[key]
                for key in (
                    "version_number",
                    "scope",
                    "owner_user_id",
                    "project_id",
                    "project_group_id",
                    "repo_id",
                    "memory_type",
                    "title",
                    "content",
                    "importance",
                    "status",
                    "confirmation_state",
                    "freshness_policy",
                    "attributes",
                    "actor_type",
                    "actor_user_id",
                    "change_reason",
                    "created_at",
                )
            },
            {
                "version_number": 1,
                "scope": "shared",
                "owner_user_id": None,
                "project_id": None,
                "project_group_id": None,
                "repo_id": None,
                "memory_type": "rule",
                "title": "Draft title",
                "content": "Draft content",
                "importance": 0,
                "status": "active",
                "confirmation_state": "confirmed",
                "freshness_policy": "permanent",
                "attributes": {"policy_subjects": ["a", "b"]},
                "actor_type": "user",
                "actor_user_id": self.admin.user_id,
                "change_reason": "Initial",
                "created_at": T0,
            },
        )
        self.assertEqual(self.relations(created.memory_id), [])
        self.assertEqual(self.sources(created.memory_id), [])

    async def test_without_subjects_or_a_reason_nothing_extra_is_stored(self):
        created = await self.service.create_memory(self.owner, draft())
        (version,) = self.versions(created.memory_id)
        self.assertEqual(version["attributes"], {})
        self.assertIsNone(version["change_reason"])

    async def test_the_creation_time_comes_from_the_clock(self):
        self.clock.advance(hours=5)
        created = await self.service.create_memory(self.owner, draft())
        self.assertEqual(
            (created.created_at, created.updated_at), (T0 + timedelta(hours=5),) * 2
        )

    async def test_every_user_can_read_what_an_admin_created(self):
        created = await self.service.create_memory(self.admin, draft(title="Common"))
        for who in (self.user, self.other_user):
            found = await self.service.get_memory(who, created.memory_id)
            self.assertEqual(found, created)
        listed = await self.service.list_memories(self.user)
        self.assertEqual(listed, [created])

    async def test_two_creations_are_two_memories(self):
        first = await self.service.create_memory(self.owner, draft(title="Same"))
        second = await self.service.create_memory(self.owner, draft(title="Same"))
        self.assertNotEqual(first.memory_id, second.memory_id)
        self.assertEqual(self.count("memories"), 2)

    async def test_the_draft_must_be_a_draft(self):
        for value in (None, {"title": "x"}, "x", SharedMemoryChanges(title="x")):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.create_memory(self.owner, value)
                self.assertEqual(caught.exception.field, "draft")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.count("memories"), 0)

    async def test_a_normal_user_cannot_create(self):
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.create_memory(self.user, draft())
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual(self.count("memories"), 0)

    async def test_the_decision_is_audited_once(self):
        await self.service.create_memory(self.owner, draft())
        event = self.only_event()
        self.assertEqual(
            (
                event.action,
                event.decision,
                event.actor_id,
                event.actor_role,
                event.resource_kind,
                event.resource_id,
                event.agent_id,
            ),
            (
                "shared_memory.create",
                "allow",
                self.owner.user_id,
                "owner",
                "shared_memory",
                None,
                None,
            ),
        )

    async def test_a_refusal_is_audited_and_writes_nothing(self):
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.create_memory(self.user, draft())
        event = self.only_event()
        self.assertEqual(
            (event.action, event.decision, event.reason, event.actor_id),
            (
                "shared_memory.create",
                "deny",
                "capability_not_granted",
                self.user.user_id,
            ),
        )
        self.assertEqual(self.snapshot()["memories"], [])


@requires_postgres
class EditMemoryTest(AsyncPostgresSharedTestCase):
    def seed(self, **overrides):
        values = {
            "title": "Old",
            "content": "Old body",
            "importance": 60,
            "subjects": ("x",),
            "memory_type": "rule",
        }
        values.update(overrides)
        return self.seed_memory(**values)

    async def test_an_edit_writes_a_new_version_and_supersedes_the_old_one(self):
        memory_id = self.seed()
        old_version = self.versions(memory_id)[0]
        self.clock.advance(hours=1)

        edited = await self.service.edit_memory(
            self.admin,
            memory_id,
            1,
            SharedMemoryChanges(title="New", content="New body", reason="Reword"),
        )

        old, new = self.versions(memory_id)
        self.assertEqual(
            (old["status"], old["title"], old["content"], old["version_number"]),
            ("superseded", "Old", "Old body", 1),
        )
        self.assertEqual(
            {
                key: new[key]
                for key in (
                    "version_number",
                    "scope",
                    "status",
                    "title",
                    "content",
                    "memory_type",
                    "importance",
                    "attributes",
                    "confirmation_state",
                    "freshness_policy",
                    "actor_type",
                    "actor_user_id",
                    "change_reason",
                    "created_at",
                )
            },
            {
                "version_number": 2,
                "scope": "shared",
                "status": "active",
                "title": "New",
                "content": "New body",
                "memory_type": "rule",
                "importance": 60,
                "attributes": {"policy_subjects": ["x"]},
                "confirmation_state": "confirmed",
                "freshness_policy": "permanent",
                "actor_type": "user",
                "actor_user_id": self.admin.user_id,
                "change_reason": "Reword",
                "created_at": T0 + timedelta(hours=1),
            },
        )
        self.assertEqual(
            edited,
            SharedMemory(
                memory_id=memory_id,
                version_id=new["id"],
                version_number=2,
                memory_type="rule",
                title="New",
                content="New body",
                importance=60,
                policy_subjects=("x",),
                status=SharedMemoryStatus.ACTIVE,
                created_at=T0,
                updated_at=T0 + timedelta(hours=1),
            ),
        )
        self.assertNotEqual(new["id"], old_version["id"])

    async def test_the_old_version_is_never_overwritten(self):
        memory_id = self.seed()
        before = self.versions(memory_id)[0]
        await self.service.edit_memory(
            self.owner, memory_id, 1, SharedMemoryChanges(content="New body")
        )
        after = self.versions(memory_id)[0]
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {"status"})

    async def test_the_new_version_supersedes_the_old_one_in_the_history_graph(self):
        memory_id = self.seed()
        await self.service.edit_memory(
            self.owner,
            memory_id,
            1,
            SharedMemoryChanges(title="B", importance=1, content="c"),
        )
        versions = self.versions(memory_id)
        (relation,) = self.relations(memory_id)
        self.assertEqual(
            (
                relation["from_version_id"],
                relation["to_version_id"],
                relation["relation_type"],
                relation["reason"],
            ),
            (
                versions[1]["id"],
                versions[0]["id"],
                "supersedes",
                "content, importance, title",
            ),
        )

    async def test_at_most_one_version_is_active(self):
        memory_id = self.seed()
        for number, title in ((1, "B"), (2, "C"), (3, "D")):
            await self.service.edit_memory(
                self.owner, memory_id, number, SharedMemoryChanges(title=title)
            )
        statuses = [v["status"] for v in self.versions(memory_id)]
        self.assertEqual(statuses, ["superseded", "superseded", "superseded", "active"])
        self.assertEqual(len(self.relations(memory_id)), 3)

    async def test_an_untouched_field_keeps_its_value(self):
        memory_id = self.seed()
        edited = await self.service.edit_memory(
            self.owner, memory_id, 1, SharedMemoryChanges(importance=0)
        )
        self.assertEqual(
            (
                edited.title,
                edited.content,
                edited.memory_type,
                edited.importance,
                edited.policy_subjects,
            ),
            ("Old", "Old body", "rule", 0, ("x",)),
        )

    async def test_the_subjects_can_be_replaced_and_cleared(self):
        memory_id = self.seed()
        edited = await self.service.edit_memory(
            self.owner, memory_id, 1, SharedMemoryChanges(policy_subjects=["z", "a"])
        )
        self.assertEqual(edited.policy_subjects, ("a", "z"))
        self.assertEqual(
            self.versions(memory_id)[-1]["attributes"], {"policy_subjects": ["a", "z"]}
        )
        edited = await self.service.edit_memory(
            self.owner, memory_id, 2, SharedMemoryChanges(policy_subjects=[])
        )
        self.assertEqual(edited.policy_subjects, ())
        self.assertEqual(self.versions(memory_id)[-1]["attributes"], {})

    async def test_without_a_reason_the_change_reason_is_empty(self):
        memory_id = self.seed()
        await self.service.edit_memory(
            self.owner, memory_id, 1, SharedMemoryChanges(title="B")
        )
        self.assertIsNone(self.versions(memory_id)[-1]["change_reason"])

    async def test_a_stale_version_is_a_conflict_and_nothing_is_written(self):
        memory_id = self.seed(versions=3)
        before = self.snapshot()
        for expected in (1, 2, 4):
            with self.subTest(expected=expected):
                with self.assertRaises(SharedMemoryVersionConflictError) as caught:
                    await self.service.edit_memory(
                        self.owner, memory_id, expected, SharedMemoryChanges(title="B")
                    )
                self.assertEqual(
                    (
                        caught.exception.expected_version,
                        caught.exception.current_version,
                    ),
                    (expected, 3),
                )
        self.assertEqual(self.snapshot(), before)

    async def test_the_second_of_two_editors_of_the_same_version_conflicts(self):
        memory_id = self.seed()
        await self.service.edit_memory(
            self.admin, memory_id, 1, SharedMemoryChanges(title="Mine")
        )
        with self.assertRaises(SharedMemoryVersionConflictError) as caught:
            await self.service.edit_memory(
                self.owner, memory_id, 1, SharedMemoryChanges(title="Yours")
            )
        self.assertEqual(caught.exception.current_version, 2)
        self.assertEqual(self.versions(memory_id)[-1]["title"], "Mine")

    async def test_an_edit_that_changes_nothing_writes_nothing(self):
        memory_id = self.seed()
        before = self.snapshot()
        same = await self.service.edit_memory(
            self.owner,
            memory_id,
            1,
            SharedMemoryChanges(
                title="Old", content="Old body", importance=60, reason="noop"
            ),
        )
        self.assertEqual((same.version_number, same.title), (1, "Old"))
        self.assertEqual(self.snapshot(), before)

    async def test_a_deleted_memory_cannot_be_edited(self):
        memory_id = self.seed(status="deprecated")
        before = self.snapshot()
        with self.assertRaises(SharedMemoryStateError) as caught:
            await self.service.edit_memory(
                self.owner, memory_id, 1, SharedMemoryChanges(title="B")
            )
        self.assertIs(caught.exception.problem, StateProblem.DELETED)
        self.assertEqual(self.snapshot(), before)

    async def test_an_unknown_or_private_memory_is_not_found(self):
        private = self.seed_memory(scope="user")
        for memory_id in (uuid4(), private):
            with self.subTest(memory_id=str(memory_id)[:8]):
                with self.assertRaises(SharedMemoryNotFoundError):
                    await self.service.edit_memory(
                        self.owner, memory_id, 1, SharedMemoryChanges(title="B")
                    )
        self.assertEqual(len(self.versions(private)), 1)

    async def test_the_arguments_are_validated(self):
        memory_id = self.seed()
        changes = SharedMemoryChanges(title="B")
        cases = [
            ((str(memory_id), 1, changes), "memory_id", InputProblem.WRONG_TYPE),
            ((None, 1, changes), "memory_id", InputProblem.REQUIRED),
            ((memory_id, 0, changes), "expected_version", InputProblem.OUT_OF_RANGE),
            ((memory_id, -1, changes), "expected_version", InputProblem.OUT_OF_RANGE),
            (
                (memory_id, 2**31, changes),
                "expected_version",
                InputProblem.OUT_OF_RANGE,
            ),
            ((memory_id, True, changes), "expected_version", InputProblem.WRONG_TYPE),
            ((memory_id, "1", changes), "expected_version", InputProblem.WRONG_TYPE),
            ((memory_id, 1.0, changes), "expected_version", InputProblem.WRONG_TYPE),
            ((memory_id, None, changes), "expected_version", InputProblem.REQUIRED),
            ((memory_id, 1, {"title": "B"}), "changes", InputProblem.WRONG_TYPE),
            ((memory_id, 1, None), "changes", InputProblem.REQUIRED),
        ]
        for args, field, problem in cases:
            with self.subTest(field=field, problem=problem.value):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.edit_memory(self.owner, *args)
                self.assertEqual(
                    (caught.exception.field, caught.exception.problem), (field, problem)
                )
        self.assertEqual(self.events(), [])

    async def test_the_largest_version_number_is_accepted_as_an_argument(self):
        memory_id = self.seed()
        with self.assertRaises(SharedMemoryVersionConflictError):
            await self.service.edit_memory(
                self.owner, memory_id, 2**31 - 1, SharedMemoryChanges(title="B")
            )

    async def test_the_edit_is_audited_with_the_memory_id(self):
        memory_id = self.seed()
        await self.service.edit_memory(
            self.admin, memory_id, 1, SharedMemoryChanges(title="B")
        )
        event = self.only_event()
        self.assertEqual(
            (
                event.action,
                event.decision,
                event.resource_kind,
                event.resource_id,
                event.actor_role,
            ),
            ("shared_memory.edit", "allow", "shared_memory", memory_id, "admin"),
        )

    async def test_a_normal_user_cannot_edit(self):
        memory_id = self.seed()
        before = self.snapshot()
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.edit_memory(
                self.user, memory_id, 1, SharedMemoryChanges(title="B")
            )
        self.assertEqual(self.snapshot(), before)

    async def test_a_refused_editor_learns_nothing_about_the_memory(self):
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.edit_memory(
                self.user, uuid4(), 1, SharedMemoryChanges(title="B")
            )


@requires_postgres
class DeleteAndRestoreTest(AsyncPostgresSharedTestCase):
    async def test_a_delete_keeps_every_version_and_only_changes_the_status(self):
        memory_id = self.seed_memory(title="Rule", versions=2)
        before = self.versions(memory_id)

        deleted = await self.service.delete_memory(self.owner, memory_id)

        after = self.versions(memory_id)
        self.assertEqual(len(after), 2)
        self.assertEqual([v["status"] for v in after], ["superseded", "deprecated"])
        for old, new in zip(before, after, strict=True):
            self.assertEqual(
                {k for k in old if old[k] != new[k]},
                {"status"} if old["version_number"] == 2 else set(),
            )
        self.assertEqual(
            (deleted.status, deleted.version_number, deleted.title),
            (SharedMemoryStatus.DELETED, 2, "Rule"),
        )
        self.assertEqual(self.count("memories"), 1)

    async def test_a_deleted_memory_disappears_for_normal_users_and_stays_for_managers(
        self,
    ):
        memory_id = self.seed_memory(title="Rule")
        await self.service.delete_memory(self.admin, memory_id)
        self.assertEqual(await self.service.list_memories(self.user), [])
        with self.assertRaises(SharedMemoryNotFoundError):
            await self.service.get_memory(self.user, memory_id)
        (still,) = await self.service.list_memories(self.owner, include_deleted=True)
        self.assertEqual(
            (still.title, still.status), ("Rule", SharedMemoryStatus.DELETED)
        )

    async def test_a_restore_activates_the_same_version_again(self):
        memory_id = self.seed_memory(title="Rule", versions=3, status="deprecated")
        restored = await self.service.restore_memory(self.admin, memory_id)
        self.assertEqual(
            (restored.status, restored.version_number, restored.title),
            (SharedMemoryStatus.ACTIVE, 3, "Rule"),
        )
        self.assertEqual(
            [v["status"] for v in self.versions(memory_id)],
            ["superseded", "superseded", "active"],
        )
        (listed,) = await self.service.list_memories(self.user)
        self.assertEqual(listed.memory_id, memory_id)

    async def test_a_restore_changes_no_content_and_writes_no_version(self):
        memory_id = self.seed_memory(status="deprecated")
        before = self.versions(memory_id)
        await self.service.restore_memory(self.owner, memory_id)
        after = self.versions(memory_id)
        self.assertEqual(len(after), len(before))
        self.assertEqual(
            {k for k in before[0] if before[0][k] != after[0][k]}, {"status"}
        )
        self.assertEqual(self.count("memory_relations"), 0)

    async def test_deleting_twice_is_a_state_error(self):
        memory_id = self.seed_memory()
        await self.service.delete_memory(self.owner, memory_id)
        before = self.snapshot()
        with self.assertRaises(SharedMemoryStateError) as caught:
            await self.service.delete_memory(self.owner, memory_id)
        self.assertIs(caught.exception.problem, StateProblem.ALREADY_DELETED)
        self.assertEqual(self.snapshot(), before)

    async def test_restoring_a_memory_that_is_not_deleted_is_a_state_error(self):
        memory_id = self.seed_memory()
        before = self.snapshot()
        with self.assertRaises(SharedMemoryStateError) as caught:
            await self.service.restore_memory(self.owner, memory_id)
        self.assertIs(caught.exception.problem, StateProblem.NOT_DELETED)
        self.assertEqual(self.snapshot(), before)

    async def test_unknown_and_private_memories_are_not_found(self):
        private = self.seed_memory(scope="user")
        for memory_id in (uuid4(), private):
            for method in (self.service.delete_memory, self.service.restore_memory):
                with self.subTest(memory_id=str(memory_id)[:8], method=method.__name__):
                    with self.assertRaises(SharedMemoryNotFoundError):
                        await method(self.owner, memory_id)
        self.assertEqual(self.versions(private)[0]["status"], "active")

    async def test_a_memory_whose_current_version_has_another_status_is_not_found(self):
        memory_id = self.seed_memory(status="history")
        with self.assertRaises(SharedMemoryNotFoundError):
            await self.service.delete_memory(self.owner, memory_id)
        with self.assertRaises(SharedMemoryNotFoundError):
            await self.service.restore_memory(self.owner, memory_id)

    async def test_the_id_must_be_a_uuid_object(self):
        for method in (self.service.delete_memory, self.service.restore_memory):
            for value in (str(uuid4()), None, 3):
                with self.subTest(method=method.__name__, value=value):
                    with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                        await method(self.owner, value)
                    self.assertEqual(caught.exception.field, "memory_id")

    async def test_normal_users_can_neither_delete_nor_restore(self):
        live = self.seed_memory(title="Live")
        gone = self.seed_memory(title="Gone", status="deprecated")
        before = self.snapshot()
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.delete_memory(self.user, live)
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.restore_memory(self.user, gone)
        self.assertEqual(self.snapshot(), before)

    async def test_a_deleted_memory_can_be_edited_after_it_is_restored(self):
        memory_id = self.seed_memory(title="Rule")
        await self.service.delete_memory(self.owner, memory_id)
        with self.assertRaises(SharedMemoryStateError):
            await self.service.edit_memory(
                self.owner, memory_id, 1, SharedMemoryChanges(title="B")
            )
        await self.service.restore_memory(self.owner, memory_id)
        edited = await self.service.edit_memory(
            self.owner, memory_id, 1, SharedMemoryChanges(title="B")
        )
        self.assertEqual(
            (edited.version_number, edited.title, edited.status),
            (2, "B", SharedMemoryStatus.ACTIVE),
        )

    async def test_the_whole_life_of_a_memory(self):
        created = await self.service.create_memory(self.owner, draft(title="v1"))
        self.clock.advance(minutes=1)
        edited = await self.service.edit_memory(
            self.admin, created.memory_id, 1, SharedMemoryChanges(title="v2")
        )
        await self.service.delete_memory(self.admin, created.memory_id)
        await self.service.restore_memory(self.owner, created.memory_id)
        final = await self.service.get_memory(self.user, created.memory_id)
        self.assertEqual(
            (final.title, final.version_number, final.status),
            ("v2", 2, SharedMemoryStatus.ACTIVE),
        )
        self.assertEqual(edited.version_number, 2)
        self.assertEqual(
            [v["status"] for v in self.versions(created.memory_id)],
            ["superseded", "active"],
        )

    async def test_each_operation_is_audited_once_with_the_memory_id(self):
        memory_id = self.seed_memory()
        await self.service.delete_memory(self.owner, memory_id)
        await self.service.restore_memory(self.admin, memory_id)
        events = self.events()
        self.assertEqual(
            [(e.action, e.decision, e.resource_id, e.actor_role) for e in events],
            [
                ("shared_memory.delete", "allow", memory_id, "owner"),
                ("shared_memory.restore", "allow", memory_id, "admin"),
            ],
        )


@requires_postgres
class ConcurrencyTest(AsyncPostgresSharedTestCase):
    async def test_of_several_editors_of_one_version_exactly_one_wins(self):
        memory_id = self.seed_memory(title="Start")
        services = [self.new_service() for _ in range(5)]
        results = await asyncio.gather(
            *(
                service.edit_memory(
                    self.admin, memory_id, 1, SharedMemoryChanges(title=f"Editor {n}")
                )
                for n, service in enumerate(services)
            ),
            return_exceptions=True,
        )
        raise_unexpected(results, SharedMemory, SharedMemoryVersionConflictError)
        winners = [r for r in results if isinstance(r, SharedMemory)]
        conflicts = [
            r for r in results if isinstance(r, SharedMemoryVersionConflictError)
        ]
        self.assertEqual((len(winners), len(conflicts)), (1, 4), results)
        self.assertEqual(
            {(c.expected_version, c.current_version) for c in conflicts}, {(1, 2)}
        )
        versions = self.versions(memory_id)
        self.assertEqual([v["status"] for v in versions], ["superseded", "active"])
        self.assertEqual(versions[1]["title"], winners[0].title)

    async def test_two_deletes_one_succeeds_and_one_is_a_state_error(self):
        memory_id = self.seed_memory()
        other = self.new_service()
        results = await asyncio.gather(
            self.service.delete_memory(self.owner, memory_id),
            other.delete_memory(self.admin, memory_id),
            return_exceptions=True,
        )
        raise_unexpected(results, SharedMemory, SharedMemoryStateError)
        kinds = sorted(type(r).__name__ for r in results)
        self.assertEqual(kinds, ["SharedMemory", "SharedMemoryStateError"], results)
        self.assertEqual(self.versions(memory_id)[0]["status"], "deprecated")

    async def test_two_restores_one_succeeds_and_one_is_a_state_error(self):
        memory_id = self.seed_memory(status="deprecated")
        other = self.new_service()
        results = await asyncio.gather(
            self.service.restore_memory(self.owner, memory_id),
            other.restore_memory(self.admin, memory_id),
            return_exceptions=True,
        )
        raise_unexpected(results, SharedMemory, SharedMemoryStateError)
        kinds = sorted(type(r).__name__ for r in results)
        self.assertEqual(kinds, ["SharedMemory", "SharedMemoryStateError"], results)
        self.assertEqual(self.versions(memory_id)[0]["status"], "active")

    async def test_a_delete_racing_an_edit_never_leaves_a_live_stale_version(self):
        for round_number in range(3):
            memory_id = self.seed_memory(title=f"Race {round_number}")
            other = self.new_service()
            results = await asyncio.gather(
                self.service.delete_memory(self.owner, memory_id),
                other.edit_memory(
                    self.admin, memory_id, 1, SharedMemoryChanges(title="Edited")
                ),
                return_exceptions=True,
            )
            raise_unexpected(results, SharedMemory, SharedMemoryStateError)
            statuses = [v["status"] for v in self.versions(memory_id)]
            # Whatever the order, the memory ends up deleted: no version is active.
            self.assertNotIn("active", statuses, statuses)
            self.assertEqual(statuses[-1], "deprecated")
            final = await self.service.get_memory(
                self.owner, memory_id, include_deleted=True
            )
            self.assertIs(final.status, SharedMemoryStatus.DELETED)

    async def test_a_write_that_cannot_get_the_lock_gives_up_with_a_busy_error(self):
        memory_id = self.seed_memory()
        impatient = self.new_service(lock_timeout_ms=200)
        connection, transaction = self.hold_advisory_lock(memory_lock_key(memory_id))
        before = self.snapshot()
        async with asyncio.timeout(30):  # generous outer guard
            with self.assertRaises(SharedMemoryBusyError) as caught:
                await impatient.delete_memory(self.owner, memory_id)
        self.assertEqual(str(caught.exception), "Shared memory is busy; retry later")
        self.assertEqual(self.snapshot(), before)
        transaction.rollback()
        deleted = await impatient.delete_memory(self.owner, memory_id)
        self.assertIs(deleted.status, SharedMemoryStatus.DELETED)

    async def test_the_lock_of_one_memory_does_not_block_another(self):
        held = self.seed_memory(title="Held")
        free = self.seed_memory(title="Free")
        impatient = self.new_service(lock_timeout_ms=200)
        self.hold_advisory_lock(memory_lock_key(held))
        async with asyncio.timeout(30):
            deleted = await impatient.delete_memory(self.owner, free)
        self.assertIs(deleted.status, SharedMemoryStatus.DELETED)


@requires_postgres
class AuditFailClosedTest(AsyncPostgresSharedTestCase):
    def failing_service(self):
        return self.new_service(
            authorizer=Authorizer(FailingSink(), directory=self.directory)
        )

    async def test_when_the_audit_cannot_be_written_no_management_happens(self):
        memory_id = self.seed_memory()
        gone = self.seed_memory(status="deprecated")
        service = self.failing_service()
        before = self.snapshot()
        calls = [
            service.create_memory(self.owner, draft()),
            service.edit_memory(
                self.owner, memory_id, 1, SharedMemoryChanges(title="B")
            ),
            service.delete_memory(self.owner, memory_id),
            service.restore_memory(self.owner, gone),
        ]
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            for call in calls:
                with self.assertRaises(SharedMemoryPermissionError) as caught:
                    await call
                self.assertEqual(caught.exception.reason, "audit_unavailable")
                self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    async def test_an_audit_failure_never_blocks_a_read(self):
        self.seed_memory(title="Readable")
        service = self.failing_service()
        found = await service.list_memories(self.user)
        self.assertEqual([m.title for m in found], ["Readable"])


@requires_postgres
class NoEchoTest(AsyncPostgresSharedTestCase):
    """Caller content never appears in an error or an audit event."""

    async def test_errors_and_audit_rows_do_not_echo_the_content(self):
        marker = "hunter2-marker-title"
        body = "hunter2-marker-body"
        memory_id = self.seed_memory(title=marker, content=body)
        errors = []
        for action in (
            lambda: self.service.create_memory(
                self.user, draft(title=marker, content=body)
            ),
            lambda: self.service.edit_memory(
                self.user, memory_id, 1, SharedMemoryChanges(title=marker, content=body)
            ),
            lambda: self.service.edit_memory(
                self.owner,
                memory_id,
                9,
                SharedMemoryChanges(title=marker, content=body),
            ),
            lambda: self.service.delete_memory(self.user, memory_id),
            lambda: self.service.get_memory(self.system, memory_id),
        ):
            try:
                await action()
            except Exception as error:
                errors.append(error)
        self.assertEqual(len(errors), 5)
        for error in errors:
            self.assertNotIn("hunter2", str(error))
            self.assertNotIn("hunter2", repr(error))
        for event in self.events():
            self.assertNotIn("hunter2", event.model_dump_json())
