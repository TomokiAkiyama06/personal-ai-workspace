"""The System Security Policy takes precedence over Shared Memory (real PostgreSQL).

``effective_view`` is the Shared Memory a model may be shown: a memory whose
declared ``policy_subjects`` are covered by a policy item is suppressed and only
named. The rules are ``precedence.py``; these tests fail while those are stubs,
except the ones about authorization and about a policy that cannot be loaded
(implemented in the service). Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import logging
from datetime import timedelta

from paw_backend.authz import Capability
from paw_backend.memory.shared import (
    EffectiveSharedMemory,
    InvalidSharedMemoryInputError,
    OverriddenMemory,
    PolicySourceError,
    SharedMemoryChanges,
    SharedMemoryPermissionError,
    StaticPolicySource,
)

from .shared_memory_support import (
    T0,
    AsyncPostgresSharedTestCase,
    agent_for,
    draft,
    policy,
    requires_postgres,
)

SECRET = "postgres://policy:hunter2@db.internal/policy"


class MutableSource:
    def __init__(self, items=()):
        self.current = list(items)
        self.calls = 0

    async def items(self):
        self.calls += 1
        return list(self.current)


class RaisingSource:
    def __init__(self):
        self.calls = 0

    async def items(self):
        self.calls += 1
        raise ConnectionError(SECRET)


class SlowSource:
    async def items(self):
        await asyncio.sleep(5)
        return []


class FixedAnswer:
    def __init__(self, answer):
        self.answer = answer

    async def items(self):
        return self.answer


@requires_postgres
class PolicyWinsTest(AsyncPostgresSharedTestCase):
    def use(self, *items):
        source = MutableSource(items)
        self.source = source
        self.service = self.new_service(policies=source)
        return source

    async def test_without_any_policy_every_active_memory_is_in_the_view(self):
        a = self.seed_memory(title="A", subjects=("merge",), created_at=T0)
        b = self.seed_memory(title="B", created_at=T0 + timedelta(seconds=1))
        self.use()
        view = await self.service.effective_view(self.user)
        self.assertIsInstance(view, EffectiveSharedMemory)
        self.assertEqual([m.memory_id for m in view.memories], [a, b])
        self.assertEqual((view.overridden, view.applied_policies), ((), ()))

    async def test_a_memory_that_declares_a_governed_subject_is_suppressed(self):
        secret_body = "SECRET-BODY-OF-THE-OVERRIDDEN-MEMORY"
        a = self.seed_memory(
            title="Allow merging",
            content=secret_body,
            subjects=("merge.permission",),
            created_at=T0,
        )
        b = self.seed_memory(
            title="Docs style", subjects=("docs",), created_at=T0 + timedelta(seconds=1)
        )
        c = self.seed_memory(title="Plain", created_at=T0 + timedelta(seconds=2))
        item = policy("no-auto-merge", "merge", "Merging needs a human.")
        self.use(item, policy("deploy-rule", "deploy"))
        version_a = self.versions(a)[-1]["id"]

        view = await self.service.effective_view(self.user)

        self.assertEqual([m.memory_id for m in view.memories], [b, c])
        self.assertEqual(
            view.overridden, (OverriddenMemory(a, version_a, ("no-auto-merge",)),)
        )
        self.assertEqual(view.applied_policies, (item,))
        self.assertNotIn(secret_body, repr(view))
        self.assertNotIn("Allow merging", repr(view))

    async def test_the_stored_memory_itself_is_not_changed_by_the_precedence(self):
        a = self.seed_memory(title="Allow merging", subjects=("merge",))
        self.use(policy("p", "merge"))
        before = self.snapshot()
        await self.service.effective_view(self.owner)
        self.assertEqual(self.snapshot(), before)
        # The plain reads return the stored memory as it is; only the view
        # applies the policy.
        (raw,) = await self.service.list_memories(self.user)
        self.assertEqual(raw.memory_id, a)
        self.assertEqual(
            (await self.service.get_memory(self.user, a)).title, "Allow merging"
        )
        self.assertEqual(self.source.calls, 1)

    async def test_a_new_policy_takes_effect_at_once_and_a_removed_one_lifts(self):
        a = self.seed_memory(title="A", subjects=("merge.permission",))
        source = self.use()
        self.assertEqual(
            [
                m.memory_id
                for m in (await self.service.effective_view(self.user)).memories
            ],
            [a],
        )
        source.current = [policy("p1", "merge")]
        view = await self.service.effective_view(self.user)
        self.assertEqual((view.memories, len(view.overridden)), ((), 1))
        source.current = []
        self.assertEqual(
            [
                m.memory_id
                for m in (await self.service.effective_view(self.user)).memories
            ],
            [a],
        )
        self.assertEqual(source.calls, 3)

    async def test_deleted_memories_are_not_in_the_view_at_all(self):
        self.seed_memory(title="Gone", subjects=("merge",), status="deprecated")
        self.seed_memory(title="Gone plain", status="deprecated")
        self.use(policy("p", "merge"))
        view = await self.service.effective_view(self.user)
        self.assertEqual(
            (view.memories, view.overridden, view.applied_policies), ((), (), ())
        )

    async def test_the_page_arguments_select_the_memories_that_are_resolved(self):
        for n in range(4):
            self.seed_memory(
                title=f"m{n}",
                subjects=("merge",) if n % 2 else (),
                created_at=T0 + timedelta(seconds=n),
            )
        self.use(policy("p", "merge"))
        first = await self.service.effective_view(self.user, limit=2)
        self.assertEqual([m.title for m in first.memories], ["m0"])
        self.assertEqual(len(first.overridden), 1)
        second = await self.service.effective_view(self.user, limit=2, offset=2)
        self.assertEqual([m.title for m in second.memories], ["m2"])
        self.assertEqual(len(second.overridden), 1)
        empty = await self.service.effective_view(self.user, offset=4)
        self.assertEqual((empty.memories, empty.overridden), ((), ()))

    async def test_the_page_arguments_are_validated(self):
        self.use()
        for kwargs in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidSharedMemoryInputError):
                    await self.service.effective_view(self.user, **kwargs)
        self.assertEqual(self.source.calls, 0)

    async def test_the_precedence_holds_for_created_and_edited_memories(self):
        self.use(policy("p", "merge"))
        created = await self.service.create_memory(
            self.admin, draft(title="Merging", policy_subjects=["merge.permission"])
        )
        service = self.service
        view = await service.effective_view(self.user)
        self.assertEqual(
            (view.memories, [o.memory_id for o in view.overridden]),
            ((), [created.memory_id]),
        )

        edited = await service.edit_memory(
            self.admin, created.memory_id, 1, SharedMemoryChanges(policy_subjects=[])
        )
        self.assertEqual(edited.policy_subjects, ())
        view = await service.effective_view(self.user)
        self.assertEqual([m.memory_id for m in view.memories], [created.memory_id])
        self.assertEqual(view.overridden, ())

        edited = await service.edit_memory(
            self.admin,
            created.memory_id,
            2,
            SharedMemoryChanges(policy_subjects=["merge"]),
        )
        view = await service.effective_view(self.user)
        self.assertEqual((view.memories, len(view.overridden)), ((), 1))

    async def test_an_approved_candidate_that_declares_a_governed_subject_is_suppressed(
        self,
    ):
        candidate_id = self.seed_candidate(policy_subjects=["merge"])
        self.use(policy("p", "merge"))
        decision = await self.service.approve_candidate(self.admin, candidate_id)
        view = await self.service.effective_view(self.user)
        self.assertEqual(view.memories, ())
        self.assertEqual(view.overridden[0].memory_id, decision.memory.memory_id)


@requires_postgres
class ViewPermissionTest(AsyncPostgresSharedTestCase):
    async def test_every_active_user_role_may_read_the_view(self):
        self.seed_memory(title="Common")
        for who in (self.user, self.other_user, self.admin, self.owner):
            with self.subTest(role=who.system_role.value):
                view = await self.service.effective_view(who)
                self.assertEqual([m.title for m in view.memories], ["Common"])
        self.assertEqual(self.events(), [])

    async def test_the_backend_own_identity_may_not(self):
        source = MutableSource([policy("p", "merge")])
        service = self.new_service(policies=source)
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await service.effective_view(self.system)
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual(source.calls, 0)  # the policy is not even loaded
        event = self.only_event()
        self.assertEqual((event.action, event.decision), ("shared_memory.read", "deny"))

    async def test_an_agent_with_the_read_grant_may_read_the_view(self):
        self.seed_memory(title="Common")
        agent = agent_for(self.user, Capability.SHARED_MEMORY_READ)
        view = await self.service.effective_view(agent)
        self.assertEqual([m.title for m in view.memories], ["Common"])

    async def test_an_agent_without_the_read_grant_may_not(self):
        agent = agent_for(self.user, Capability.MEMORY_USE)
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.effective_view(agent)


@requires_postgres
class PolicyUnavailableTest(AsyncPostgresSharedTestCase):
    """Without the policy there is no view: never Shared Memory without the policy."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.seed_memory(title="Must not leak without the policy")

    async def test_a_source_that_fails_gives_no_memory_and_no_secret(self):
        source = RaisingSource()
        service = self.new_service(policies=source)
        with self.assertLogs(
            "paw_backend.memory.shared.policy", logging.WARNING
        ) as logs:
            with self.assertRaises(PolicySourceError) as caught:
                await service.effective_view(self.user)
        self.assertEqual(source.calls, 1)
        self.assertEqual(str(caught.exception), "System policy is unavailable")
        self.assertNotIn("hunter2", repr(caught.exception))
        self.assertNotIn("hunter2", " ".join(logs.output))
        self.assertIsNone(caught.exception.__cause__)

    async def test_a_source_that_is_too_slow_gives_no_memory(self):
        service = self.new_service(policies=SlowSource(), policy_timeout_seconds=0.1)
        async with asyncio.timeout(20):  # generous outer guard; the source sleeps 5 s
            with self.assertLogs("paw_backend.memory.shared.policy", logging.WARNING):
                with self.assertRaises(PolicySourceError):
                    await service.effective_view(self.user)

    async def test_an_answer_that_breaks_the_contract_gives_no_memory(self):
        item = policy("a", "merge")
        for answer in (
            None,
            "abc",
            {"a": item},
            [item, item],
            [item, "x"],
            iter([item]),
        ):
            with self.subTest(answer=type(answer).__name__):
                service = self.new_service(policies=FixedAnswer(answer))
                with self.assertRaises(PolicySourceError):
                    await service.effective_view(self.user)

    async def test_the_plain_reads_do_not_depend_on_the_policy_source(self):
        service = self.new_service(policies=RaisingSource())
        found = await service.list_memories(self.user)
        self.assertEqual([m.title for m in found], ["Must not leak without the policy"])

    async def test_a_service_needs_an_explicit_policy_source(self):
        service = self.new_service(policies=StaticPolicySource(()))
        view = await service.effective_view(self.user)
        self.assertEqual(len(view.memories), 1)
