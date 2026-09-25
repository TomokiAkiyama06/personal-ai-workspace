"""Reading Shared Memory: every active user, nobody else (real PostgreSQL).

Rows are seeded with SQL; the service runs with the real ``Authorizer``. Skipped
unless ``PAW_TEST_DATABASE_URL`` is set.
"""

from datetime import timedelta
from uuid import uuid4

from paw_backend.authz import Capability
from paw_backend.memory.shared import (
    AutomaticPromotionRefusedError,
    InputProblem,
    InvalidSharedMemoryInputError,
    SharedMemory,
    SharedMemoryDataError,
    SharedMemoryNotFoundError,
    SharedMemoryPermissionError,
    SharedMemoryStatus,
)

from .authz_support import uid
from .shared_memory_support import (
    AGENT_ID,
    T0,
    AsyncPostgresSharedTestCase,
    agent_for,
    requires_postgres,
)


@requires_postgres
class ListMemoriesTest(AsyncPostgresSharedTestCase):
    async def test_every_active_user_role_sees_the_same_active_memories(self):
        self.seed_memory(title="First", created_at=T0)
        self.seed_memory(title="Second", created_at=T0 + timedelta(seconds=10))
        for who in (self.user, self.other_user, self.admin, self.owner):
            with self.subTest(role=who.system_role.value):
                found = await self.service.list_memories(who)
                self.assertEqual([m.title for m in found], ["First", "Second"])

    async def test_a_listed_memory_carries_every_field_of_its_current_version(self):
        memory_id = self.seed_memory(
            title="Rule of the house",
            content="Line 1\nLine 2",
            memory_type="team_rule",
            importance=77,
            subjects=("merge.permission", "docs"),
            versions=3,
            created_at=T0,
        )
        version_id = self.versions(memory_id)[-1]["id"]

        (found,) = await self.service.list_memories(self.user)

        self.assertEqual(
            found,
            SharedMemory(
                memory_id=memory_id,
                version_id=version_id,
                version_number=3,
                memory_type="team_rule",
                title="Rule of the house",
                content="Line 1\nLine 2",
                importance=77,
                policy_subjects=("docs", "merge.permission"),
                status=SharedMemoryStatus.ACTIVE,
                created_at=T0,
                updated_at=T0 + timedelta(seconds=2),
            ),
        )

    async def test_only_active_shared_memories_are_listed(self):
        self.seed_memory(title="Visible")
        self.seed_memory(title="Deleted", status="deprecated")
        self.seed_memory(title="History only", status="history")
        self.seed_memory(title="Superseded only", status="superseded")
        for scope in ("user", "project", "project_group", "repo"):
            self.seed_memory(title=f"Private {scope}", scope=scope)
        found = await self.service.list_memories(self.owner)
        self.assertEqual([m.title for m in found], ["Visible"])

    async def test_a_memory_that_was_shared_but_is_now_private_is_not_listed(self):
        memory_id = self.seed_memory(title="Was shared", versions=1)
        self.seed_version(memory_id, 2, scope="user", title="Now private")
        self.assertEqual(await self.service.list_memories(self.user), [])

    async def test_a_memory_that_became_shared_is_listed_with_its_shared_version(self):
        memory_id = self.seed_memory(title="Was private", scope="user")
        self.seed_version(memory_id, 2, scope="shared", title="Now shared")
        (found,) = await self.service.list_memories(self.user)
        self.assertEqual((found.title, found.version_number), ("Now shared", 2))

    async def test_an_empty_store_gives_an_empty_page(self):
        self.assertEqual(await self.service.list_memories(self.user), [])

    async def test_the_order_is_oldest_first(self):
        self.seed_memory(title="C", created_at=T0 + timedelta(seconds=30))
        self.seed_memory(title="A", created_at=T0)
        self.seed_memory(title="B", created_at=T0 + timedelta(seconds=10))
        found = await self.service.list_memories(self.user)
        self.assertEqual([m.title for m in found], ["A", "B", "C"])

    async def test_memories_created_at_the_same_instant_are_ordered_by_id(self):
        ids = [self.seed_memory(title=f"m{n}", created_at=T0) for n in range(12)]
        found = await self.service.list_memories(self.user)
        self.assertEqual([m.memory_id for m in found], sorted(ids))

    async def test_pages(self):
        for n in range(5):
            self.seed_memory(title=f"m{n}", created_at=T0 + timedelta(seconds=n))

        async def titles(**page):
            return [
                m.title for m in await self.service.list_memories(self.user, **page)
            ]

        self.assertEqual(await titles(limit=2), ["m0", "m1"])
        self.assertEqual(await titles(limit=2, offset=2), ["m2", "m3"])
        self.assertEqual(await titles(limit=2, offset=4), ["m4"])
        self.assertEqual(await titles(limit=2, offset=5), [])
        self.assertEqual(await titles(limit=200), [f"m{n}" for n in range(5)])
        self.assertEqual(await titles(offset=1), ["m1", "m2", "m3", "m4"])

    async def test_the_default_page_holds_at_most_fifty(self):
        for n in range(53):
            self.seed_memory(title=f"m{n:02}", created_at=T0 + timedelta(seconds=n))
        found = await self.service.list_memories(self.user)
        self.assertEqual(len(found), 50)
        self.assertEqual(found[0].title, "m00")
        self.assertEqual(found[-1].title, "m49")

    async def test_the_page_arguments_are_validated(self):
        bad = [
            ({"limit": 0}, "limit", InputProblem.OUT_OF_RANGE),
            ({"limit": 201}, "limit", InputProblem.OUT_OF_RANGE),
            ({"limit": -1}, "limit", InputProblem.OUT_OF_RANGE),
            ({"limit": True}, "limit", InputProblem.WRONG_TYPE),
            ({"limit": "5"}, "limit", InputProblem.WRONG_TYPE),
            ({"limit": 5.0}, "limit", InputProblem.WRONG_TYPE),
            ({"limit": None}, "limit", InputProblem.REQUIRED),
            ({"offset": -1}, "offset", InputProblem.OUT_OF_RANGE),
            ({"offset": 100_001}, "offset", InputProblem.OUT_OF_RANGE),
            ({"offset": True}, "offset", InputProblem.WRONG_TYPE),
            ({"offset": "0"}, "offset", InputProblem.WRONG_TYPE),
            ({"include_deleted": 1}, "include_deleted", InputProblem.WRONG_TYPE),
            ({"include_deleted": "yes"}, "include_deleted", InputProblem.WRONG_TYPE),
            ({"include_deleted": None}, "include_deleted", InputProblem.WRONG_TYPE),
        ]
        for kwargs, field, problem in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.list_memories(self.user, **kwargs)
                self.assertEqual(
                    (caught.exception.field, caught.exception.problem), (field, problem)
                )

    async def test_the_page_boundaries_are_accepted(self):
        await self.service.list_memories(self.user, limit=1)
        await self.service.list_memories(self.user, limit=200, offset=100_000)

    async def test_arguments_are_validated_before_the_actor_is_authorized(self):
        with self.assertRaises(InvalidSharedMemoryInputError):
            await self.service.list_memories(self.system, limit=0)
        self.assertEqual(self.events(), [])

    async def test_an_actor_of_the_wrong_type_is_refused(self):
        for actor in (None, "user", {"user_id": str(uuid4())}, uuid4()):
            with self.subTest(actor=type(actor).__name__):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.list_memories(actor)
                self.assertEqual(caught.exception.field, "actor")
        self.assertEqual(self.events(), [])

    async def test_a_page_never_changes_anything(self):
        self.seed_memory(title="One", versions=2)
        before = self.snapshot()
        await self.service.list_memories(self.owner, include_deleted=True)
        self.assertEqual(self.snapshot(), before)


@requires_postgres
class IncludeDeletedTest(AsyncPostgresSharedTestCase):
    async def test_owner_and_admin_can_list_the_deleted_ones_too(self):
        self.seed_memory(title="Live", created_at=T0)
        self.seed_memory(
            title="Gone", status="deprecated", created_at=T0 + timedelta(seconds=5)
        )
        for who in (self.admin, self.owner):
            with self.subTest(role=who.system_role.value):
                found = await self.service.list_memories(who, include_deleted=True)
                self.assertEqual(
                    [(m.title, m.status) for m in found],
                    [
                        ("Live", SharedMemoryStatus.ACTIVE),
                        ("Gone", SharedMemoryStatus.DELETED),
                    ],
                )

    async def test_a_normal_user_may_not_ask_for_deleted_memories(self):
        self.seed_memory(title="Gone", status="deprecated")
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.list_memories(self.user, include_deleted=True)
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertNotIsInstance(caught.exception, AutomaticPromotionRefusedError)
        event = self.only_event()
        self.assertEqual(
            (event.action, event.decision, event.actor_id),
            ("shared_memory.manage", "deny", self.user.user_id),
        )

    async def test_an_agent_may_not_ask_for_deleted_memories(self):
        agent = agent_for(
            self.admin, Capability.SHARED_MEMORY_READ, Capability.SHARED_MEMORY_MANAGE
        )
        with self.assertRaises(AutomaticPromotionRefusedError):
            await self.service.list_memories(agent, include_deleted=True)

    async def test_include_deleted_false_is_the_default_behaviour(self):
        self.seed_memory(title="Gone", status="deprecated")
        self.assertEqual(
            await self.service.list_memories(self.admin, include_deleted=False), []
        )


@requires_postgres
class GetMemoryTest(AsyncPostgresSharedTestCase):
    async def test_every_active_user_can_read_one_memory(self):
        memory_id = self.seed_memory(title="Rule", content="Body", importance=10)
        for who in (self.user, self.admin, self.owner):
            with self.subTest(role=who.system_role.value):
                found = await self.service.get_memory(who, memory_id)
                self.assertEqual(
                    (found.memory_id, found.title, found.content, found.importance),
                    (memory_id, "Rule", "Body", 10),
                )

    async def test_a_missing_memory_is_not_found(self):
        with self.assertRaises(SharedMemoryNotFoundError):
            await self.service.get_memory(self.user, uuid4())

    async def test_a_deleted_memory_is_not_found_for_a_normal_user(self):
        memory_id = self.seed_memory(status="deprecated")
        with self.assertRaises(SharedMemoryNotFoundError):
            await self.service.get_memory(self.user, memory_id)

    async def test_a_deleted_memory_is_returned_to_the_admin_with_include_deleted(self):
        memory_id = self.seed_memory(title="Gone", status="deprecated")
        found = await self.service.get_memory(
            self.admin, memory_id, include_deleted=True
        )
        self.assertEqual(
            (found.title, found.status), ("Gone", SharedMemoryStatus.DELETED)
        )

    async def test_an_active_memory_is_also_returned_with_include_deleted(self):
        memory_id = self.seed_memory()
        found = await self.service.get_memory(
            self.owner, memory_id, include_deleted=True
        )
        self.assertIs(found.status, SharedMemoryStatus.ACTIVE)

    async def test_a_private_memory_is_not_found_even_for_the_owner(self):
        for scope in ("user", "project", "project_group", "repo"):
            with self.subTest(scope=scope):
                memory_id = self.seed_memory(scope=scope)
                for who in (self.owner, self.user):
                    with self.assertRaises(SharedMemoryNotFoundError):
                        await self.service.get_memory(
                            who, memory_id, include_deleted=who is self.owner
                        )

    async def test_a_memory_whose_current_version_has_another_status_is_not_found(
        self,
    ):
        for status in ("history", "superseded"):
            with self.subTest(status=status):
                memory_id = self.seed_memory(status=status)
                with self.assertRaises(SharedMemoryNotFoundError):
                    await self.service.get_memory(
                        self.owner, memory_id, include_deleted=True
                    )

    async def test_the_current_version_is_the_highest_numbered_one(self):
        memory_id = self.seed_memory(title="Newest", versions=4)
        found = await self.service.get_memory(self.user, memory_id)
        self.assertEqual((found.title, found.version_number), ("Newest", 4))

    async def test_the_id_must_be_a_uuid_object(self):
        memory_id = self.seed_memory()
        for value in (str(memory_id), None, 5, b"x"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSharedMemoryInputError) as caught:
                    await self.service.get_memory(self.user, value)
                self.assertEqual(caught.exception.field, "memory_id")

    async def test_the_permission_is_checked_before_the_database_is_read(self):
        # A normal user asking for deleted ones gets "not allowed" even for an
        # id that does not exist: existence is not disclosed to them.
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.get_memory(self.user, uuid4(), include_deleted=True)


@requires_postgres
class ReaderPermissionTest(AsyncPostgresSharedTestCase):
    async def test_the_backend_own_identity_cannot_read(self):
        self.seed_memory()
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.list_memories(self.system)
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.get_memory(self.system, uuid4())

    async def test_an_allowed_read_is_not_audited(self):
        self.seed_memory()
        await self.service.list_memories(self.user)
        await self.service.get_memory(self.user, self.seed_memory())
        self.assertEqual(self.events(), [])

    async def test_a_denied_read_is_audited_without_content(self):
        memory_id = self.seed_memory(title="Secret title", content="Secret body")
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.get_memory(self.system, memory_id)
        event = self.only_event()
        self.assertEqual(
            (
                event.action,
                event.decision,
                event.reason,
                event.resource_kind,
                event.resource_id,
            ),
            (
                "shared_memory.read",
                "deny",
                "capability_not_granted",
                "shared_memory",
                memory_id,
            ),
        )
        self.assertNotIn("Secret", event.model_dump_json())

    async def test_the_error_of_a_denied_read_names_only_the_reason(self):
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.list_memories(self.system)
        self.assertEqual(str(caught.exception), "Not allowed: capability_not_granted")


@requires_postgres
class AgentReadTest(AsyncPostgresSharedTestCase):
    async def test_an_agent_with_the_read_grant_reads_for_an_active_user(self):
        self.seed_memory(title="Shared knowledge")
        agent = agent_for(self.user, Capability.SHARED_MEMORY_READ)
        found = await self.service.list_memories(agent)
        self.assertEqual([m.title for m in found], ["Shared knowledge"])
        event = self.only_event()  # an agent's decision is always recorded
        self.assertEqual(
            (event.action, event.decision, event.agent_id, event.actor_id),
            ("shared_memory.read", "allow", AGENT_ID, self.user.user_id),
        )

    async def test_an_agent_reads_one_memory(self):
        memory_id = self.seed_memory(title="One")
        agent = agent_for(self.user, Capability.SHARED_MEMORY_READ)
        found = await self.service.get_memory(agent, memory_id)
        self.assertEqual(found.title, "One")

    async def test_an_agent_without_the_grant_is_refused(self):
        agent = agent_for(self.user, Capability.MEMORY_USE)
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.list_memories(agent)
        self.assertEqual(caught.exception.reason, "agent_capability_not_granted")

    async def test_an_agent_limited_to_some_projects_is_refused(self):
        agent = agent_for(
            self.user, Capability.SHARED_MEMORY_READ, projects=frozenset({uid(101)})
        )
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.list_memories(agent)
        self.assertEqual(caught.exception.reason, "agent_project_not_granted")

    async def test_an_agent_of_a_user_who_is_not_active_is_refused(self):
        agent = agent_for(self.user, Capability.SHARED_MEMORY_READ)
        del self.directory.principals[self.user.user_id]
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.list_memories(agent)
        self.assertEqual(caught.exception.reason, "delegator_not_active")

    async def test_the_agent_of_the_backend_identity_is_refused(self):
        agent = agent_for(self.system, Capability.SHARED_MEMORY_READ)
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.list_memories(agent)


@requires_postgres
class MalformedStoredSubjectsTest(AsyncPostgresSharedTestCase):
    async def test_absent_or_unrelated_attributes_mean_no_subjects(self):
        for attributes in ({}, {"other": 1}, {"policy_subjects": []}):
            with self.subTest(attributes=attributes):
                memory_id = self.seed_memory(attributes=attributes)
                found = await self.service.get_memory(self.user, memory_id)
                self.assertEqual(found.policy_subjects, ())

    async def test_stored_subjects_are_returned_sorted_and_unique(self):
        memory_id = self.seed_memory(attributes={"policy_subjects": ["b", "a", "b"]})
        found = await self.service.get_memory(self.user, memory_id)
        self.assertEqual(found.policy_subjects, ("a", "b"))

    async def test_malformed_subjects_raise_a_data_error_without_the_content(self):
        bad = [
            "merge",
            5,
            {"a": 1},
            [1],
            ["Not Valid"],
            ["a.b.c.d.e.f"],
            [f"s{n}" for n in range(21)],
        ]
        for value in bad:
            with self.subTest(value=str(value)[:30]):
                memory_id = self.seed_memory(
                    title="Secret title", attributes={"policy_subjects": value}
                )
                with self.assertRaises(SharedMemoryDataError) as caught:
                    await self.service.get_memory(self.user, memory_id)
                self.assertEqual(
                    str(caught.exception), "A stored shared memory is malformed"
                )
                self.assertIsNone(caught.exception.__cause__)

    async def test_one_malformed_row_makes_the_page_fail_closed(self):
        self.seed_memory(title="Fine")
        self.seed_memory(title="Broken", attributes={"policy_subjects": "oops"})
        with self.assertRaises(SharedMemoryDataError):
            await self.service.list_memories(self.user)
