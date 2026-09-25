"""The System Security Policy takes precedence over Shared Memory (real PostgreSQL).

``effective_view`` is the Shared Memory a model may be shown: a memory whose
declared ``policy_subjects`` are covered by a policy item is suppressed and only
named. The rules are ``precedence.py``; these tests fail while those are stubs,
except the ones about authorization and about a policy that cannot be loaded
(implemented in the service). Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import dataclasses
import json
import logging
from datetime import timedelta

from paw_backend.authz import Capability
from paw_backend.memory.shared import (
    EffectiveSharedMemory,
    InternalEffectiveView,
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
WORDING = "POLICY-WORDING-Merging-needs-a-human-approval"


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
        self.assertEqual(view.overridden, ())
        # The policies are asked of the internal path (Decision 0009, section 10:
        # the public view has no ``applied_policies``).
        internal = await self.service.internal_effective_view(self.user)
        self.assertEqual(internal.applied_policies, ())

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
        item = policy("no-auto-merge", "merge", WORDING)
        self.use(item, policy("deploy-rule", "deploy"))
        version_a = self.versions(a)[-1]["id"]

        view = await self.service.effective_view(self.user)

        self.assertEqual([m.memory_id for m in view.memories], [b, c])
        self.assertEqual(
            view.overridden, (OverriddenMemory(a, version_a, ("no-auto-merge",)),)
        )
        # Was ``view.applied_policies == (item,)``. Since Decision 0009 was
        # approved (section 10) the public view does not carry the policy items;
        # they come from the internal path, which sees the same memories.
        internal = await self.service.internal_effective_view(self.user)
        self.assertEqual(internal.applied_policies, (item,))
        self.assertEqual(
            (internal.memories, internal.overridden), (view.memories, view.overridden)
        )
        self.assertNotIn(secret_body, repr(view))
        self.assertNotIn("Allow merging", repr(view))
        self.assertNotIn(secret_body, repr(internal))

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
        self.assertEqual((view.memories, view.overridden), ((), ()))
        internal = await self.service.internal_effective_view(self.user)
        self.assertEqual(
            (internal.memories, internal.overridden, internal.applied_policies),
            ((), (), ()),
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
class PolicyWordingStaysInternalTest(AsyncPostgresSharedTestCase):
    """Users and agents never get the wording of a System Policy (Decision 0009, s.10).

    ``effective_view`` is the only view a user or an agent may get; the wording of
    the policies that overrode a memory is on ``internal_effective_view`` only,
    for the backend's own context assembly.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.overridden_id = self.seed_memory(
            title="Allow merging", subjects=("merge.permission",), created_at=T0
        )
        self.seed_memory(title="Plain", created_at=T0 + timedelta(seconds=1))
        self.service = self.new_service(
            policies=StaticPolicySource([policy("no-auto-merge", "merge", WORDING)])
        )

    def actors(self):
        return {
            "user": self.user,
            "admin": self.admin,
            "owner": self.owner,
            "agent": agent_for(self.user, Capability.SHARED_MEMORY_READ),
        }

    async def test_no_reading_of_the_public_view_carries_the_wording(self):
        for name, actor in self.actors().items():
            with self.subTest(actor=name):
                view = await self.service.effective_view(actor)
                self.assertIsInstance(view, EffectiveSharedMemory)
                self.assertEqual([m.title for m in view.memories], ["Plain"])
                self.assertEqual(
                    [(o.memory_id, o.policy_ids) for o in view.overridden],
                    [(self.overridden_id, ("no-auto-merge",))],
                )
                readings = (
                    repr(view),
                    str(view),
                    repr(dataclasses.asdict(view)),
                    json.dumps(dataclasses.asdict(view), default=str),
                    repr(dataclasses.astuple(view)),
                )
                for text in readings:
                    self.assertNotIn(WORDING, text)
                self.assertFalse(hasattr(view, "applied_policies"))
                self.assertEqual(
                    [f.name for f in dataclasses.fields(view)],
                    ["memories", "overridden"],
                )

    async def test_the_wording_is_only_on_the_internal_path(self):
        internal = await self.service.internal_effective_view(self.user)
        self.assertIsInstance(internal, InternalEffectiveView)
        self.assertEqual(
            [(p.policy_id, p.statement) for p in internal.applied_policies],
            [("no-auto-merge", WORDING)],
        )
        # An accidental log line made from the internal view stays clean.
        self.assertNotIn(WORDING, repr(internal))
        self.assertNotIn(WORDING, str(internal))
        # ``public()`` of it is exactly what ``effective_view`` returns.
        self.assertEqual(
            internal.public(), await self.service.effective_view(self.user)
        )

    async def test_the_public_method_has_no_argument_that_turns_the_wording_on(self):
        with self.assertRaises(TypeError):
            await self.service.effective_view(self.user, include_policies=True)
        with self.assertRaises(TypeError):
            await self.service.effective_view(self.user, internal=True)

    async def test_no_error_or_audit_row_of_the_view_carries_the_wording(self):
        for call in (
            self.service.effective_view,
            self.service.internal_effective_view,
        ):
            await call(self.user)
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await self.service.effective_view(self.system)
        self.assertNotIn(WORDING, repr(caught.exception))
        self.assertNotIn(WORDING, repr(self.events()))


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

    async def test_the_internal_path_needs_the_same_capability_as_the_public_one(self):
        # ``internal_effective_view`` is backend-internal by where it is called
        # from (Decision 0009, section 10), and it is no way around the checks:
        # the same read capability, the same refusals, the same audit action.
        self.seed_memory(title="Common")
        for who in (self.user, self.admin, self.owner):
            with self.subTest(role=who.system_role.value):
                view = await self.service.internal_effective_view(who)
                self.assertEqual([m.title for m in view.memories], ["Common"])
        self.assertEqual(self.events(), [])
        agent = agent_for(self.user, Capability.SHARED_MEMORY_READ)
        view = await self.service.internal_effective_view(agent)
        self.assertEqual([m.title for m in view.memories], ["Common"])

        source = MutableSource([policy("p", "merge")])
        service = self.new_service(policies=source)
        with self.assertRaises(SharedMemoryPermissionError) as caught:
            await service.internal_effective_view(self.system)
        self.assertEqual(caught.exception.reason, "capability_not_granted")
        self.assertEqual(source.calls, 0)
        with self.assertRaises(SharedMemoryPermissionError):
            await self.service.internal_effective_view(
                agent_for(self.user, Capability.MEMORY_USE)
            )
        # An agent's use is always audited (the first row); a refusal is too.
        self.assertEqual(
            [(e.action, e.decision) for e in self.events()],
            [
                ("shared_memory.read", "allow"),
                ("shared_memory.read", "deny"),
                ("shared_memory.read", "deny"),
            ],
        )


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

    async def test_the_internal_path_fails_closed_as_well(self):
        source = RaisingSource()
        service = self.new_service(policies=source)
        with self.assertLogs("paw_backend.memory.shared.policy", logging.WARNING):
            with self.assertRaises(PolicySourceError) as caught:
                await service.internal_effective_view(self.user)
        self.assertEqual(source.calls, 1)
        self.assertNotIn("hunter2", repr(caught.exception))

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
