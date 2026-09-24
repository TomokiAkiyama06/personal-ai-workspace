import asyncio
import contextlib
import dataclasses
import hashlib
import unittest
import uuid
from datetime import timedelta

from paw_backend.authz import (
    Capability,
    InMemoryAuditSink,
    ProjectRole,
    SystemRole,
)
from paw_backend.tasks import TaskState
from paw_backend.tools import (
    ApprovalEventKind,
    ApprovalLevel,
    ApprovalOutcome,
    ApprovalRevocationError,
    ApprovalService,
    ApprovalStatus,
    ArgumentKind,
    ArgumentSpec,
    BrokerReason,
    BudgetStatus,
    Environment,
    FailClosedStepUp,
    FailClosedTaskActivity,
    InMemoryApprovalStore,
    SummaryItem,
    TaskActivity,
    TaskScope,
    ToolCapability,
    ToolRegistry,
    ToolSpec,
    Verdict,
)
from paw_backend.tools.scope import Target, TargetKind

from .authz_support import AGENT, SECRET, FailingSink, StaticDirectory, principal
from .tools_store_contract import LIMITS, StoreContract, new_approval
from .tools_support import (
    NOW,
    P1,
    REPO,
    ROOT,
    TASK,
    U1,
    U2,
    FakeTaskActivity,
    Harness,
    StepUp,
    make_call,
    make_context,
    make_grant,
    sample_registry,
)

R = BrokerReason
GITHUB_TOKEN = "ghp_" + "a1B2" * 9
U3 = uuid.UUID(int=3)
DELETE = {"path": f"{ROOT}/build"}
MERGE = {
    "remote": "https://github.com/org/repo.git",
    "repository": str(REPO),
    "credential": "cred_" + "a1" * 16,
    "pull_request": 7,
}
HOUR = timedelta(hours=1)


class InMemoryStoreTest(StoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemoryApprovalStore()

    async def test_the_test_double_is_bounded(self):
        small = InMemoryApprovalStore(max_records=2)
        await small.open_request(new_approval(), now=NOW, limits=LIMITS)
        await small.open_request(new_approval(), now=NOW, limits=LIMITS)
        with self.assertRaises(RuntimeError):
            await small.open_request(new_approval(), now=NOW, limits=LIMITS)


class NewApprovalTest(unittest.TestCase):
    def test_only_human_approval_levels_are_stored(self):
        new_approval(level=ApprovalLevel.APPROVAL)
        new_approval(level=ApprovalLevel.STRONG_APPROVAL)
        for level in (
            ApprovalLevel.AUTO,
            ApprovalLevel.SCOPED_AUTO,
            ApprovalLevel.DENY,
        ):
            with self.subTest(level=level):
                with self.assertRaises(ValueError):
                    new_approval(level=level)

    def test_fields_are_validated(self):
        for overrides in (
            {"call_hash": "a" * 63},
            {"call_hash": "A" * 64},
            {"call_hash": "g" * 64},
            {"call_hash": ""},
            {"expires_at": NOW.replace(tzinfo=None)},
            {"agent_id": U1},  # an agent cannot be the user it acts for
            {"tool": ""},
            {"tool": "x" * 65},
        ):
            with self.subTest(overrides=str(overrides)[:40]):
                with self.assertRaises(ValueError):
                    new_approval(**overrides)
        for overrides in ({"task_id": str(TASK)}, {"approval_id": 5}):
            with self.subTest(overrides=str(overrides)[:40]):
                with self.assertRaises(TypeError):
                    new_approval(**overrides)


class ApprovalFlowTest(unittest.IsolatedAsyncioTestCase):
    """The broker and the approval service together, on the in-memory store."""

    async def asyncSetUp(self):
        self.h = Harness()
        self.user = principal(SystemRole.USER, U1)

    def call(self, tool="repo.delete_tree", arguments=None, **kw):
        return make_call(tool, DELETE if arguments is None else arguments, **kw)

    async def open(self, **kw):
        decision = await self.h.broker.request(self.call(**kw))
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL, decision)
        return decision

    async def approve(self, decision, who=None):
        result = await self.h.service.approve(decision.approval_id, who or self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED)
        return result

    async def kinds(self, approval_id):
        return [e.kind for e in await self.h.approvals.history(approval_id)]

    # -- creating the request --

    async def test_a_high_risk_call_opens_a_pending_request_and_emits_an_event(self):
        decision = await self.open()
        record = await self.h.approvals.get(decision.approval_id)
        self.assertEqual(
            (
                record.status,
                record.level,
                record.tool,
                record.task_id,
                record.project_id,
                record.agent_id,
                record.requester_user_id,
                record.created_at,
                record.expires_at,
                record.targets,
                record.call_hash,
            ),
            (
                ApprovalStatus.PENDING,
                ApprovalLevel.APPROVAL,
                "repo.delete_tree",
                TASK,
                P1,
                AGENT,
                U1,
                NOW,
                NOW + HOUR,
                (Target(TargetKind.PATH, f"{ROOT}/build"),),
                decision.call_hash,
            ),
        )
        self.assertEqual(
            [
                (e.kind, e.approval_id, e.task_id, e.tool, e.level, e.occurred_at)
                for e in self.h.events
            ],
            [
                (
                    ApprovalEventKind.REQUESTED,
                    decision.approval_id,
                    TASK,
                    "repo.delete_tree",
                    ApprovalLevel.APPROVAL,
                    NOW,
                )
            ],
        )
        self.assertEqual(
            await self.kinds(decision.approval_id), [ApprovalEventKind.REQUESTED]
        )
        row = self.h.tool_events()[-1]
        self.assertEqual(
            (row.action, row.decision, row.reason),
            ("tool.repo.delete_tree", "deny", "approval_required"),
        )

    async def test_a_repeated_request_finds_the_open_approval(self):
        first = await self.open()
        again = await self.open()
        self.assertEqual(again.approval_id, first.approval_id)
        self.assertEqual(again.reason, R.APPROVAL_PENDING)
        self.assertEqual(len(self.h.approvals._records), 1)
        self.assertEqual(len(self.h.events), 1)  # no second "requested" event

    async def test_the_approval_lifetime_is_configurable(self):
        h = Harness(approval_ttl=timedelta(minutes=5))
        decision = await h.broker.request(self.call())
        record = await h.approvals.get(decision.approval_id)
        self.assertEqual(record.expires_at, NOW + timedelta(minutes=5))

    async def test_the_agent_side_cannot_approve(self):
        for name in ("approve", "reject", "decide", "grant", "approvals", "service"):
            self.assertFalse(hasattr(self.h.broker, name), name)
        self.assertFalse(hasattr(self.h.runner, "approve"))

    # -- approve, then use once --

    async def test_an_approved_call_runs_once(self):
        pending = await self.open()
        self.h.events.clear()
        await self.approve(pending)
        used = await self.h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertEqual(
            (used.verdict, used.reason, used.level, used.approval_id),
            (
                Verdict.ALLOW,
                R.APPROVAL_CONSUMED,
                ApprovalLevel.APPROVAL,
                pending.approval_id,
            ),
        )
        self.assertEqual(used.invocation.call_hash, pending.call_hash)
        self.assertEqual(
            [e.kind for e in self.h.events],
            [ApprovalEventKind.APPROVED, ApprovalEventKind.CONSUMED],
        )
        self.assertEqual(
            await self.kinds(pending.approval_id),
            [
                ApprovalEventKind.REQUESTED,
                ApprovalEventKind.APPROVED,
                ApprovalEventKind.CONSUMED,
            ],
        )
        row = self.h.tool_events()[-1]
        self.assertEqual((row.decision, row.reason), ("allow", "approval_consumed"))

    async def test_a_replayed_approval_is_denied_and_a_new_request_needs_a_new_approval(
        self,
    ):
        pending = await self.open()
        await self.approve(pending)
        first = await self.h.broker.request(
            self.call(), approval_id=pending.approval_id
        )
        replay = await self.h.broker.request(
            self.call(), approval_id=pending.approval_id
        )
        self.assertTrue(first.allowed)
        self.assertEqual(
            (replay.verdict, replay.reason, replay.invocation),
            (Verdict.DENY, R.APPROVAL_ALREADY_USED, None),
        )
        fresh = await self.h.broker.request(self.call())
        self.assertEqual(
            (fresh.verdict, fresh.reason), (Verdict.NEEDS_APPROVAL, R.APPROVAL_REQUIRED)
        )
        self.assertNotEqual(fresh.approval_id, pending.approval_id)

    async def test_an_approval_that_is_still_pending_is_not_consumed(self):
        pending = await self.open()
        again = await self.h.broker.request(
            self.call(), approval_id=pending.approval_id
        )
        self.assertEqual(
            (again.verdict, again.reason, again.approval_id),
            (Verdict.NEEDS_APPROVAL, R.APPROVAL_PENDING, pending.approval_id),
        )
        await self.approve(pending)
        used = await self.h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertTrue(used.allowed)

    async def test_a_rejected_call_is_denied(self):
        pending = await self.open()
        result = await self.h.service.reject(pending.approval_id, self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.REJECTED)
        used = await self.h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_REJECTED)
        )

    async def test_an_approval_that_does_not_exist_is_denied(self):
        used = await self.h.broker.request(self.call(), approval_id=uuid.uuid4())
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_NOT_FOUND)
        )

    # -- bound to the exact call --

    async def test_a_modified_call_cannot_use_the_approval(self):
        pending = await self.open()
        await self.approve(pending)
        modified = self.call(arguments={"path": f"{ROOT}/other"})
        used = await self.h.broker.request(modified, approval_id=pending.approval_id)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_MISMATCH)
        )
        # ... and the refusal did not spend it
        original = await self.h.broker.request(
            self.call(), approval_id=pending.approval_id
        )
        self.assertTrue(original.allowed)

    async def test_another_tool_task_agent_or_user_cannot_use_the_approval(self):
        directory = StaticDirectory(
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
            principal(SystemRole.USER, U2, {P1: ProjectRole.CONTRIBUTOR}),
        )
        h = Harness(directory=directory)
        pending = await h.broker.request(self.call())
        await h.service.approve(pending.approval_id, self.user)
        other_agent = uuid.UUID(int=999)
        variants = {
            "task": make_context(task_id=uuid.UUID(int=502)),
            "agent": make_context(
                grant=dataclasses.replace(make_grant(), agent_id=other_agent)
            ),
            "user": make_context(delegator_id=U2),
        }
        for label, context in variants.items():
            with self.subTest(differs=label):
                used = await h.broker.request(
                    self.call(context=context), approval_id=pending.approval_id
                )
                self.assertEqual(
                    (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_MISMATCH)
                )
        install = await h.broker.request(
            make_call("host.install_package", {"package": "ripgrep"}),
            approval_id=pending.approval_id,
        )
        self.assertEqual(
            (install.verdict, install.reason), (Verdict.DENY, R.APPROVAL_MISMATCH)
        )
        self.assertTrue(
            (
                await h.broker.request(self.call(), approval_id=pending.approval_id)
            ).allowed
        )

    async def test_an_approval_of_one_level_cannot_serve_another(self):
        pending = await self.open()
        await self.approve(pending)
        strict = ToolRegistry(
            dataclasses.replace(spec, min_level=ApprovalLevel.STRONG_APPROVAL)
            if spec.name == "repo.delete_tree"
            else spec
            for spec in (
                sample_registry().get(n) for n in sorted(sample_registry().names())
            )
        )
        stricter = Harness(
            registry=strict, approvals=self.h.approvals, sink=self.h.sink
        )
        used = await stricter.broker.request(
            self.call(), approval_id=pending.approval_id
        )
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_MISMATCH)
        )
        self.assertEqual(used.level, ApprovalLevel.STRONG_APPROVAL)

    # -- expiry --

    async def test_an_expired_approval_is_denied(self):
        pending = await self.open()
        await self.approve(pending)
        self.h.clock.advance(hours=1)  # exactly at expiry counts as expired
        used = await self.h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_EXPIRED)
        )
        self.assertEqual(
            (await self.h.approvals.get(pending.approval_id)).status,
            ApprovalStatus.EXPIRED,
        )
        again = await self.h.broker.request(
            self.call(), approval_id=pending.approval_id
        )
        self.assertEqual(again.reason, R.APPROVAL_EXPIRED)

    async def test_an_approval_used_just_before_it_expires_still_works(self):
        pending = await self.open()
        await self.approve(pending)
        self.h.clock.advance(minutes=59, seconds=59)
        used = await self.h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertTrue(used.allowed)

    async def test_a_request_nobody_answered_expires_and_a_new_one_is_opened(self):
        pending = await self.open()
        self.h.clock.advance(hours=1, minutes=1)
        result = await self.h.service.approve(pending.approval_id, self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.EXPIRED)
        fresh = await self.open()
        self.assertNotEqual(fresh.approval_id, pending.approval_id)
        self.assertEqual(fresh.reason, R.APPROVAL_REQUIRED)

    # -- who may approve --

    async def test_the_requesting_agent_cannot_approve_its_own_request(self):
        pending = await self.open()
        as_agent = principal(SystemRole.USER, AGENT)
        result = await self.h.service.approve(pending.approval_id, as_agent)
        self.assertEqual(result.outcome, ApprovalOutcome.SELF_APPROVAL)
        as_owner_agent = principal(SystemRole.OWNER, AGENT)
        result = await self.h.service.approve(pending.approval_id, as_owner_agent)
        self.assertEqual(result.outcome, ApprovalOutcome.SELF_APPROVAL)
        self.assertEqual(
            (await self.h.approvals.get(pending.approval_id)).status,
            ApprovalStatus.PENDING,
        )
        used = await self.h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.NEEDS_APPROVAL, R.APPROVAL_PENDING)
        )

    async def test_only_the_user_the_agent_works_for_can_approve_or_reject(self):
        pending = await self.open()
        for who in (
            principal(SystemRole.USER, U2),
            principal(SystemRole.ADMIN, U3),
            principal(SystemRole.OWNER, U3),
        ):
            for method in (self.h.service.approve, self.h.service.reject):
                with self.subTest(who=who.system_role, method=method.__name__):
                    result = await method(pending.approval_id, who)
                    # told "not found": no oracle for other users' approvals
                    self.assertEqual(result.outcome, ApprovalOutcome.NOT_FOUND)
        rows = [
            (e.action, e.decision, e.reason)
            for e in self.h.sink.events
            if e.action.startswith("tool.approval.")
        ]
        self.assertEqual(len(rows), 6)
        self.assertEqual({r[1:] for r in rows}, {("deny", "not_authorised")})
        self.assertEqual(
            (await self.h.approvals.get(pending.approval_id)).status,
            ApprovalStatus.PENDING,
        )

    async def test_invalid_input_to_the_approval_service_is_refused(self):
        pending = await self.open()
        for approval_id, who in (
            ("not-a-uuid", self.user),
            (pending.approval_id, U1),
            (pending.approval_id, str(U1)),
            (pending.approval_id, None),
            (None, self.user),
        ):
            with self.subTest(approval_id=str(approval_id)[:8], who=repr(who)[:8]):
                result = await self.h.service.approve(approval_id, who)
                self.assertEqual(result.outcome, ApprovalOutcome.INVALID)
                self.assertFalse(result)
        missing = await self.h.service.approve(uuid.uuid4(), self.user)
        self.assertEqual(missing.outcome, ApprovalOutcome.NOT_FOUND)

    async def test_deciding_twice_is_refused(self):
        pending = await self.open()
        await self.approve(pending)
        again = await self.h.service.approve(pending.approval_id, self.user)
        reject = await self.h.service.reject(pending.approval_id, self.user)
        self.assertEqual(
            (again.outcome, reject.outcome),
            (ApprovalOutcome.NOT_PENDING, ApprovalOutcome.NOT_PENDING),
        )

    # -- strong approval and step-up ---------------------------------------------------

    async def open_merge(self, h=None):
        h = h or self.h
        decision = await h.broker.request(make_call("git.merge", MERGE))
        self.assertEqual(decision.reason, R.STRONG_APPROVAL_REQUIRED)
        return decision

    async def test_a_strong_approval_fails_closed_without_a_step_up_verifier(self):
        pending = await self.open_merge()
        service = ApprovalService(self.h.approvals, self.h.sink, clock=self.h.clock)
        self.assertIsInstance(service._step_up, FailClosedStepUp)
        result = await service.approve(pending.approval_id, self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.STEP_UP_REQUIRED)
        self.assertFalse(result)
        self.assertEqual(
            (await self.h.approvals.get(pending.approval_id)).status,
            ApprovalStatus.PENDING,
        )
        used = await self.h.broker.request(
            make_call("git.merge", MERGE), approval_id=pending.approval_id
        )
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.NEEDS_APPROVAL, R.APPROVAL_PENDING)
        )

    async def test_a_strong_approval_needs_a_confirmed_step_up(self):
        pending = await self.open_merge()
        step_up = StepUp(True)
        service = ApprovalService(
            self.h.approvals, self.h.sink, step_up=step_up, clock=self.h.clock
        )
        result = await service.approve(pending.approval_id, self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED)
        self.assertEqual(step_up.calls, [(U1, pending.approval_id)])
        used = await self.h.broker.request(
            make_call("git.merge", MERGE), approval_id=pending.approval_id
        )
        self.assertEqual(
            (used.verdict, used.reason, used.level),
            (Verdict.ALLOW, R.APPROVAL_CONSUMED, ApprovalLevel.STRONG_APPROVAL),
        )

    async def test_anything_but_an_explicit_yes_is_no_step_up(self):
        answers = [False, None, "yes", 1, "True", [True], RuntimeError(SECRET)]
        for answer in answers:
            with self.subTest(answer=repr(answer)[:20]):
                pending = await self.h.broker.request(
                    make_call("git.merge", {**MERGE, "pull_request": len(repr(answer))})
                )
                service = ApprovalService(
                    self.h.approvals,
                    self.h.sink,
                    step_up=StepUp(answer),
                    clock=self.h.clock,
                )
                with (
                    self.assertLogs(level="WARNING")
                    if isinstance(answer, Exception)
                    else contextlib.nullcontext()
                ):
                    result = await service.approve(pending.approval_id, self.user)
                self.assertEqual(result.outcome, ApprovalOutcome.STEP_UP_REQUIRED)

    async def test_a_step_up_that_never_answers_is_no_step_up(self):
        class Stuck:
            async def verify(self, user_id, approval_id):
                await asyncio.Event().wait()

        pending = await self.open_merge()
        service = ApprovalService(
            self.h.approvals,
            self.h.sink,
            step_up=Stuck(),
            clock=self.h.clock,
            timeout_seconds=0.05,
        )
        result = await service.approve(pending.approval_id, self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.STEP_UP_REQUIRED)

    async def test_step_up_failure_messages_are_not_logged(self):
        pending = await self.open_merge()
        service = ApprovalService(
            self.h.approvals,
            self.h.sink,
            step_up=StepUp(RuntimeError(SECRET)),
            clock=self.h.clock,
        )
        with self.assertLogs(level="WARNING") as logs:
            await service.approve(pending.approval_id, self.user)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))

    async def test_step_up_is_not_asked_for_what_needs_none(self):
        step_up = StepUp(False)
        service = ApprovalService(
            self.h.approvals, self.h.sink, step_up=step_up, clock=self.h.clock
        )
        normal = await self.open()
        self.assertEqual(
            (await service.approve(normal.approval_id, self.user)).outcome,
            ApprovalOutcome.APPROVED,
        )
        strong = await self.open_merge()
        self.assertEqual(
            (await service.reject(strong.approval_id, self.user)).outcome,
            ApprovalOutcome.REJECTED,
        )
        again = await service.approve(strong.approval_id, self.user)  # already decided
        self.assertEqual(again.outcome, ApprovalOutcome.NOT_PENDING)
        other_merge = await self.h.broker.request(
            make_call("git.merge", {**MERGE, "pull_request": 8})
        )
        stranger = await service.approve(
            other_merge.approval_id, principal(SystemRole.OWNER, U3)
        )
        self.assertEqual(stranger.outcome, ApprovalOutcome.NOT_FOUND)
        self.assertEqual(step_up.calls, [])

    # -- the approval never widens anything --

    async def test_the_authorization_is_checked_again_when_the_approval_is_used(self):
        directory = StaticDirectory(
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR})
        )
        h = Harness(directory=directory)
        pending = await h.broker.request(self.call())
        await h.service.approve(pending.approval_id, self.user)
        directory.principals[U1] = principal(
            SystemRole.USER, U1, {P1: ProjectRole.VIEWER}
        )
        used = await h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertEqual((used.verdict, used.reason), (Verdict.DENY, R.AUTHZ_DENIED))
        # the denied use did not spend the approval
        self.assertEqual(
            (await h.approvals.get(pending.approval_id)).status, ApprovalStatus.APPROVED
        )
        directory.principals[U1] = principal(
            SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}
        )
        self.assertTrue(
            (
                await h.broker.request(self.call(), approval_id=pending.approval_id)
            ).allowed
        )

    async def test_the_budget_is_checked_again_when_the_approval_is_used(self):
        h = Harness()
        pending = await h.broker.request(self.call())
        await h.service.approve(pending.approval_id, self.user)
        h.budget.status = BudgetStatus.EXCEEDED
        used = await h.broker.request(self.call(), approval_id=pending.approval_id)
        self.assertEqual((used.verdict, used.reason), (Verdict.DENY, R.BUDGET_EXCEEDED))
        self.assertEqual(
            (await h.approvals.get(pending.approval_id)).status, ApprovalStatus.APPROVED
        )

    async def test_the_scope_is_checked_again_when_the_approval_is_used(self):
        pending = await self.open()
        await self.approve(pending)
        narrow = make_context(
            scope=TaskScope(
                path_roots=["/srv/elsewhere"], hosts=[], projects={P1: "active"}
            )
        )
        used = await self.h.broker.request(
            self.call(context=narrow), approval_id=pending.approval_id
        )
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.PATH_OUT_OF_SCOPE)
        )

    # -- failing adapters --

    async def test_a_failing_approval_store_denies_without_its_message(self):
        class Failing(InMemoryApprovalStore):
            async def open_request(self, new, *, now):
                raise ConnectionError(SECRET)

            async def consume(
                self, approval_id, binding, *, now, require_active_task=False
            ):
                raise ConnectionError(SECRET)

        h = Harness(approvals=Failing())
        with self.assertLogs(level="ERROR") as logs:
            opened = await h.broker.request(self.call())
            used = await h.broker.request(self.call(), approval_id=uuid.uuid4())
        for decision in (opened, used):
            self.assertEqual(
                (decision.verdict, decision.reason),
                (Verdict.DENY, R.APPROVAL_UNAVAILABLE),
            )
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn(SECRET, "".join(e.model_dump_json() for e in h.sink.events))

    async def test_a_store_that_answers_nonsense_denies(self):
        class Nonsense(InMemoryApprovalStore):
            async def open_request(self, new, *, now):
                return "created"

            async def consume(
                self, approval_id, binding, *, now, require_active_task=False
            ):
                return "consumed"

        h = Harness(approvals=Nonsense())
        with self.assertLogs(level="ERROR"):
            opened = await h.broker.request(self.call())
        used = await h.broker.request(self.call(), approval_id=uuid.uuid4())
        self.assertEqual(opened.reason, R.APPROVAL_UNAVAILABLE)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_UNAVAILABLE)
        )

    async def test_a_store_that_returns_another_calls_request_denies(self):
        class Wrong(InMemoryApprovalStore):
            async def open_request(self, new, *, now):
                other = dataclasses.replace(new, call_hash="0" * 64)
                return await super().open_request(other, now=now)

        h = Harness(approvals=Wrong())
        with self.assertLogs(level="ERROR"):
            decision = await h.broker.request(self.call())
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.APPROVAL_UNAVAILABLE)
        )

    async def test_a_failing_or_slow_listener_does_not_disturb_the_request(self):
        async def slow(event):
            await asyncio.Event().wait()

        def failing(event):
            raise RuntimeError(SECRET)

        h = Harness(listeners=[failing, slow], timeout_seconds=0.05)
        with self.assertLogs(level="WARNING") as logs:
            decision = await h.broker.request(self.call())
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(
            len([line for line in logs.output if "Approval listener failed" in line]), 2
        )

    async def test_a_failing_audit_sink_does_not_undo_an_approval(self):
        pending = await self.open()
        service = ApprovalService(
            self.h.approvals, FailingSink(), step_up=StepUp(True), clock=self.h.clock
        )
        with self.assertLogs(level="ERROR") as logs:
            result = await service.approve(pending.approval_id, self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(
            (await self.h.approvals.get(pending.approval_id)).status,
            ApprovalStatus.APPROVED,
        )

    # -- concurrency --

    async def test_of_many_simultaneous_uses_exactly_one_call_runs(self):
        pending = await self.open()
        await self.approve(pending)
        decisions = await asyncio.gather(
            *(
                self.h.broker.request(self.call(), approval_id=pending.approval_id)
                for _ in range(15)
            )
        )
        self.assertEqual(sum(d.allowed for d in decisions), 1)
        self.assertEqual(
            sorted(d.reason.value for d in decisions if not d.allowed),
            [R.APPROVAL_ALREADY_USED.value] * 14,
        )

    # -- the audit trail of the approval itself --

    async def test_approving_and_rejecting_are_audited(self):
        pending = await self.open()
        await self.approve(pending)
        other = await self.open(arguments={"path": f"{ROOT}/other"})
        await self.h.service.reject(other.approval_id, self.user)
        await self.h.service.approve(
            other.approval_id, principal(SystemRole.USER, AGENT)
        )
        rows = [e for e in self.h.sink.events if e.action.startswith("tool.approval.")]
        self.assertEqual(
            [(e.action, e.decision, e.reason, e.resource_id) for e in rows],
            [
                ("tool.approval.approve", "allow", "approved", pending.approval_id),
                ("tool.approval.reject", "allow", "rejected", other.approval_id),
                ("tool.approval.approve", "deny", "self_approval", other.approval_id),
            ],
        )
        first = rows[0]
        self.assertEqual(
            (
                first.actor_id,
                first.actor_role,
                first.agent_id,
                first.resource_kind,
                first.project_id,
            ),
            (U1, "user", None, "tool_approval", P1),
        )


class ApprovalSummaryTest(unittest.IsolatedAsyncioTestCase):
    """What the approver is shown: every argument, bounded and redacted."""

    async def asyncSetUp(self):
        self.h = Harness()

    async def open(self, tool, arguments, harness=None):
        h = harness or self.h
        decision = await h.broker.request(make_call(tool, arguments))
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL, decision)
        return await h.approvals.get(decision.approval_id)

    async def test_a_text_argument_is_shown_although_it_is_no_target(self):
        record = await self.open(
            "host.install_package", {"package": "evil-backdoor==1.0"}
        )
        self.assertEqual(record.targets, ())  # nothing typed to show ...
        self.assertEqual(  # ... yet the approver sees what is installed
            record.summary, (SummaryItem("package", "text", "evil-backdoor==1.0"),)
        )

    async def test_the_path_and_query_of_an_external_url_are_shown(self):
        record = await self.open(
            "issues.create",
            {
                "url": "https://example.org/issues?data=SECRETBYTES",
                "repository": str(REPO),
                "title": "hello",
            },
        )
        self.assertEqual(
            record.summary,
            (
                SummaryItem(
                    "url", "url", "https://example.org/issues?data=SECRETBYTES"
                ),
                SummaryItem("repository", "repository", str(REPO)),
                SummaryItem("title", "text", "hello"),
            ),
        )
        self.assertEqual(
            record.targets,
            (
                Target(TargetKind.HOST, "example.org"),
                Target(TargetKind.REPOSITORY, str(REPO)),
            ),
        )

    async def test_every_kind_of_argument_appears_in_declared_order(self):
        spec = ToolSpec(
            "db.drop_data",
            frozenset({ToolCapability.DESTRUCTIVE}),
            # Not a repository write (that must name its repository): what is
            # tested here is only how the arguments are shown.
            Capability.PROJECT_TASK_RUN,
            {
                "project": ArgumentSpec(ArgumentKind.PROJECT),
                "confirm": ArgumentSpec(ArgumentKind.BOOLEAN),
                "rows": ArgumentSpec(ArgumentKind.INTEGER, minimum=0, maximum=99),
                "note": ArgumentSpec(ArgumentKind.TEXT, required=False),
            },
        )
        h = Harness(registry=ToolRegistry([spec]))
        record = await self.open(
            "db.drop_data", {"rows": 7, "confirm": True, "project": str(P1)}, h
        )
        self.assertEqual(
            [(i.name, i.kind, i.value) for i in record.summary],
            [
                ("project", "project", str(P1)),
                ("confirm", "boolean", "true"),
                ("rows", "integer", "7"),
            ],
        )

    async def test_a_long_value_is_cut_and_carries_its_length_and_hash(self):
        spec = ToolSpec(
            "db.migrate",
            frozenset({ToolCapability.DESTRUCTIVE}),
            # Not a repository write (that must name its repository): what is
            # tested here is only how the arguments are shown.
            Capability.PROJECT_TASK_RUN,
            {
                "project": ArgumentSpec(ArgumentKind.PROJECT),
                "sql": ArgumentSpec(ArgumentKind.TEXT, max_length=2000),
            },
        )
        h = Harness(registry=ToolRegistry([spec]))
        sql = "DROP TABLE users; " + "x" * 981  # 999 chars
        record = await self.open("db.migrate", {"project": str(P1), "sql": sql}, h)
        shown = record.summary[1].value
        digest = hashlib.sha256(sql.encode()).hexdigest()[:12]
        self.assertEqual(shown, sql[:256] + f"...[999 chars, sha256:{digest}]")
        self.assertLessEqual(len(shown), 320)
        self.assertTrue(shown.startswith("DROP TABLE users;"))

    async def test_the_summary_never_holds_credential_plaintext(self):
        record = await self.open(
            "host.install_package", {"package": "tool --password hunter2hunter2"}
        )
        (item,) = record.summary
        self.assertEqual(item.value, "tool --password [REDACTED]")
        self.assertNotIn("hunter2", repr(record.summary))
        # what would hold a token never gets this far (the call is denied) ...
        denied = await self.h.broker.request(
            make_call("host.install_package", {"package": "tool " + GITHUB_TOKEN})
        )
        self.assertEqual(denied.reason, R.CREDENTIAL_PLAINTEXT_IN_ARGUMENTS)
        # ... and a summary that did hold one cannot be built at all
        with self.assertRaises(ValueError):
            SummaryItem("package", "text", GITHUB_TOKEN)
        with self.assertRaises(ValueError):
            new_approval(summary=())

    async def test_control_and_direction_characters_are_shown_as_escapes(self):
        record = await self.open(
            "host.install_package", {"package": "a\nb\u202eevil\u200bc"}
        )
        (item,) = record.summary
        self.assertEqual(item.value, "a\\u000ab\\u202eevil\\u200bc")

    async def test_the_summary_is_kept_with_the_request_in_the_history(self):
        record = await self.open("host.install_package", {"package": "ripgrep"})
        (first,) = await self.h.approvals.history(record.approval_id)
        self.assertEqual(first.summary, record.summary)

    async def test_an_approval_nobody_can_read_is_refused(self):
        spec = ToolSpec(
            "host.reboot",
            frozenset({ToolCapability.EXECUTE}),
            Capability.PROJECT_TASK_RUN,
            {"reason": ArgumentSpec(ArgumentKind.TEXT, required=False)},
            environment=Environment.HOST,
        )
        h = Harness(registry=ToolRegistry([spec]))
        decision = await h.broker.request(make_call("host.reboot", {}))
        self.assertEqual(
            (decision.verdict, decision.reason, decision.level),
            (Verdict.DENY, R.APPROVAL_NOT_DISPLAYABLE, ApprovalLevel.APPROVAL),
        )
        self.assertEqual(len(h.approvals._records), 0)
        self.assertEqual(h.events, [])
        # with an argument it can be shown, so it can be asked
        shown = await h.broker.request(make_call("host.reboot", {"reason": "kernel"}))
        self.assertEqual(shown.verdict, Verdict.NEEDS_APPROVAL)


class ApprovalLimitsTest(unittest.IsolatedAsyncioTestCase):
    """Pending approvals are bounded and a rejection is not forgotten at once."""

    def delete(self, n: int, **kw):
        return make_call("repo.delete_tree", {"path": f"{ROOT}/build{n}"}, **kw)

    async def test_pending_approvals_are_capped_per_task_and_user(self):
        h = Harness(max_pending_approvals=3)
        decisions = [await h.broker.request(self.delete(n)) for n in range(6)]
        self.assertEqual(
            [(d.verdict, d.reason) for d in decisions],
            [(Verdict.NEEDS_APPROVAL, R.APPROVAL_REQUIRED)] * 3
            + [(Verdict.DENY, R.APPROVAL_LIMIT_REACHED)] * 3,
        )
        self.assertEqual(len(h.approvals._records), 3)
        self.assertEqual(len(h.events), 3)  # no event for a refused request
        self.assertTrue(all(d.approval_id is None for d in decisions[3:]))
        # the same call is still found, not refused
        again = await h.broker.request(self.delete(0))
        self.assertEqual(
            (again.verdict, again.reason), (Verdict.NEEDS_APPROVAL, R.APPROVAL_PENDING)
        )
        # another task is not affected
        other = make_context(task_id=uuid.UUID(int=502))
        ok = await h.broker.request(self.delete(0, context=other))
        self.assertEqual(ok.verdict, Verdict.NEEDS_APPROVAL)

    async def test_three_hundred_distinct_calls_open_only_the_default_cap(self):
        h = Harness()
        decisions = [await h.broker.request(self.delete(n)) for n in range(300)]
        self.assertEqual(
            sum(d.verdict is Verdict.NEEDS_APPROVAL for d in decisions), 10
        )
        self.assertEqual(
            {d.reason for d in decisions if d.verdict is Verdict.DENY},
            {R.APPROVAL_LIMIT_REACHED},
        )
        self.assertEqual(len(h.approvals._records), 10)
        self.assertEqual(len(h.events), 10)

    async def test_a_decided_or_revoked_approval_frees_its_place(self):
        h = Harness(max_pending_approvals=2)
        user = principal(SystemRole.USER, U1)
        first = await h.broker.request(self.delete(0))
        second = await h.broker.request(self.delete(1))
        self.assertEqual(
            (await h.broker.request(self.delete(2))).reason, R.APPROVAL_LIMIT_REACHED
        )
        await h.service.revoke(first.approval_id, user)
        self.assertEqual(
            (await h.broker.request(self.delete(2))).verdict, Verdict.NEEDS_APPROVAL
        )
        self.assertEqual(
            (await h.broker.request(self.delete(3))).reason, R.APPROVAL_LIMIT_REACHED
        )
        await h.service.reject(second.approval_id, user)
        self.assertEqual(
            (await h.broker.request(self.delete(3))).verdict, Verdict.NEEDS_APPROVAL
        )

    async def test_a_rejected_call_is_not_asked_again_at_once(self):
        h = Harness()
        user = principal(SystemRole.USER, U1)
        for round_ in range(5):  # the reviewer's loop: reject, ask again, repeat
            asked = await h.broker.request(self.delete(0))
            if round_ == 0:
                self.assertEqual(asked.reason, R.APPROVAL_REQUIRED)
                await h.service.reject(asked.approval_id, user)
            else:
                self.assertEqual(
                    (asked.verdict, asked.reason, asked.approval_id),
                    (Verdict.DENY, R.APPROVAL_COOLDOWN, None),
                )
        self.assertEqual(len(h.approvals._records), 1)
        self.assertEqual(len(h.events), 2)  # requested, rejected: nothing more
        h.clock.advance(minutes=4, seconds=59)
        self.assertEqual(
            (await h.broker.request(self.delete(0))).reason, R.APPROVAL_COOLDOWN
        )
        h.clock.advance(seconds=2)
        fresh = await h.broker.request(self.delete(0))
        self.assertEqual(
            (fresh.verdict, fresh.reason), (Verdict.NEEDS_APPROVAL, R.APPROVAL_REQUIRED)
        )

    async def test_the_cooldown_only_holds_back_the_rejected_call(self):
        h = Harness()
        asked = await h.broker.request(self.delete(0))
        await h.service.reject(asked.approval_id, principal(SystemRole.USER, U1))
        other = await h.broker.request(self.delete(1))
        self.assertEqual(other.verdict, Verdict.NEEDS_APPROVAL)

    async def test_limits_are_configurable_within_bounds(self):
        from datetime import timedelta

        h = Harness(rejection_cooldown=timedelta(hours=2))
        asked = await h.broker.request(self.delete(0))
        await h.service.reject(asked.approval_id, principal(SystemRole.USER, U1))
        h.clock.advance(hours=1, minutes=59)
        self.assertEqual(
            (await h.broker.request(self.delete(0))).reason, R.APPROVAL_COOLDOWN
        )
        for kwargs in (
            {"max_pending_approvals": 0},
            {"max_pending_approvals": 101},
            {"max_pending_approvals": True},
            {"max_pending_approvals": "10"},
            {"rejection_cooldown": timedelta(seconds=59)},
            {"rejection_cooldown": timedelta(hours=25)},
            {"rejection_cooldown": 300},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Harness(**kwargs)
        Harness(max_pending_approvals=1, rejection_cooldown=timedelta(minutes=1))
        Harness(max_pending_approvals=100, rejection_cooldown=timedelta(hours=24))

    async def test_concurrent_requests_cannot_exceed_the_cap(self):
        h = Harness(max_pending_approvals=4)
        decisions = await asyncio.gather(
            *(h.broker.request(self.delete(n)) for n in range(25))
        )
        self.assertEqual(sum(d.verdict is Verdict.NEEDS_APPROVAL for d in decisions), 4)
        self.assertEqual(len(h.approvals._records), 4)


class ApprovalRevocationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        self.user = principal(SystemRole.USER, U1)
        self.call = make_call("repo.delete_tree", DELETE)

    async def pending(self):
        decision = await self.h.broker.request(self.call)
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)
        return decision

    async def test_the_user_can_withdraw_a_pending_or_an_approved_request(self):
        for approve_first in (False, True):
            with self.subTest(approved=approve_first):
                h = Harness()
                pending = await h.broker.request(self.call)
                if approve_first:
                    await h.service.approve(pending.approval_id, self.user)
                result = await h.service.revoke(pending.approval_id, self.user)
                self.assertEqual(result.outcome, ApprovalOutcome.REVOKED)
                self.assertTrue(result)
                used = await h.broker.request(
                    self.call, approval_id=pending.approval_id
                )
                self.assertEqual(
                    (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_REVOKED)
                )
                self.assertEqual(
                    [e.kind for e in h.events][-1], ApprovalEventKind.REVOKED
                )
                again = await h.service.revoke(pending.approval_id, self.user)
                self.assertEqual(again.outcome, ApprovalOutcome.NOT_OPEN)

    async def test_an_admin_or_owner_may_revoke_for_the_user(self):
        for role in (SystemRole.ADMIN, SystemRole.OWNER):
            with self.subTest(role=role):
                h = Harness()
                pending = await h.broker.request(self.call)
                result = await h.service.revoke(
                    pending.approval_id, principal(role, U3)
                )
                self.assertEqual(result.outcome, ApprovalOutcome.REVOKED)
                record = await h.approvals.get(pending.approval_id)
                self.assertEqual(
                    (record.status, record.revoked_by), (ApprovalStatus.REVOKED, U3)
                )

    async def test_nobody_else_can_revoke_and_learns_nothing(self):
        pending = await self.pending()
        for who in (
            principal(SystemRole.USER, U2),
            principal(SystemRole.USER, AGENT),
            principal(SystemRole.OWNER, AGENT),
        ):
            with self.subTest(who=who.user_id, role=who.system_role):
                result = await self.h.service.revoke(pending.approval_id, who)
                self.assertEqual(result.outcome, ApprovalOutcome.NOT_FOUND)
        self.assertEqual(
            (await self.h.approvals.get(pending.approval_id)).status,
            ApprovalStatus.PENDING,
        )
        missing = await self.h.service.revoke(uuid.uuid4(), self.user)
        self.assertEqual(missing.outcome, ApprovalOutcome.NOT_FOUND)
        for bad in (("x", self.user), (pending.approval_id, U1), (None, None)):
            self.assertEqual(
                (await self.h.service.revoke(*bad)).outcome, ApprovalOutcome.INVALID
            )

    async def test_a_revocation_is_audited(self):
        pending = await self.pending()
        await self.h.service.revoke(pending.approval_id, principal(SystemRole.USER, U2))
        await self.h.service.revoke(pending.approval_id, self.user)
        rows = [
            (e.action, e.decision, e.reason, e.actor_id)
            for e in self.h.sink.events
            if e.action == "tool.approval.revoke"
        ]
        self.assertEqual(
            rows,
            [
                ("tool.approval.revoke", "deny", "not_authorised", U2),
                ("tool.approval.revoke", "allow", "revoked", U1),
            ],
        )

    async def test_the_approvals_of_a_task_that_ended_are_revoked(self):
        from paw_backend.tasks import TaskState

        class Event:
            def __init__(self, task_id, to_state):
                self.task_id = task_id
                self.to_state = to_state

        other_context = make_context(task_id=uuid.UUID(int=502))
        mine = await self.pending()
        approved = await self.h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/other"})
        )
        await self.h.service.approve(approved.approval_id, self.user)
        elsewhere = await self.h.broker.request(
            make_call("repo.delete_tree", DELETE, context=other_context)
        )
        self.h.events.clear()

        await self.h.service.revoke_on_task_end(Event(TASK, TaskState.RUNNING))
        self.assertEqual(self.h.events, [])  # a running task keeps its approvals
        for state in (TaskState.CANCELLED, TaskState.FAILED):
            await self.h.service.revoke_on_task_end(Event(TASK, state))
        self.assertEqual(
            sorted(e.approval_id for e in self.h.events),
            sorted([mine.approval_id, approved.approval_id]),
        )
        self.assertEqual({e.kind for e in self.h.events}, {ApprovalEventKind.REVOKED})
        for decision in (mine, approved):
            used = await self.h.broker.request(
                make_call("repo.delete_tree", {"path": f"{ROOT}/other"})
                if decision is approved
                else self.call,
                approval_id=decision.approval_id,
            )
            self.assertEqual(used.reason, R.APPROVAL_REVOKED)
        self.assertEqual(
            (await self.h.approvals.get(elsewhere.approval_id)).status,
            ApprovalStatus.PENDING,
        )
        rows = [e for e in self.h.sink.events if e.reason == "task_ended"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(e.actor_id is None and e.decision == "allow" for e in rows))

    async def test_only_a_task_id_and_a_state_are_read_from_the_event(self):
        pending = await self.pending()
        for event in (object(), None, "cancelled", type("E", (), {"task_id": TASK})()):
            await self.h.service.revoke_on_task_end(event)
        self.assertEqual(
            (await self.h.approvals.get(pending.approval_id)).status,
            ApprovalStatus.PENDING,
        )
        with self.assertRaises(TypeError):
            await self.h.service.revoke_task("not-a-uuid")

    async def test_a_strong_approval_records_its_step_up(self):
        h = Harness()
        pending = await h.broker.request(make_call("git.merge", MERGE))
        await h.service.approve(pending.approval_id, self.user)
        record = await h.approvals.get(pending.approval_id)
        self.assertEqual(
            (record.status, record.step_up_verified), (ApprovalStatus.APPROVED, True)
        )


class RevokeFailingStore(InMemoryApprovalStore):
    """The in-memory store, except that revoking a task's approvals can fail."""

    error: Exception | None = None

    async def revoke_task(self, task_id, *, now):
        if self.error is not None:
            raise self.error
        return await super().revoke_task(task_id, now=now)


class TaskEvent:
    """What ``TaskService`` hands to a listener (only the fields read here)."""

    def __init__(self, task_id, to_state, from_state=None):
        self.task_id = task_id
        self.to_state = to_state
        self.from_state = from_state


class TaskEndTest(unittest.IsolatedAsyncioTestCase):
    """What happens to the approvals of a task that ended, on every path.

    The task lifecycle has three terminal states (completed, failed, cancelled)
    and no "expired" one; an approval runs out by its own ``expires_at``. The
    revocation runs after the terminal transition committed, so it may fail
    with nothing to retry it: the broker must not honour such an approval.
    """

    async def asyncSetUp(self):
        self.store = RevokeFailingStore()
        self.h = Harness(approvals=self.store)
        self.user = principal(SystemRole.USER, U1)

    async def open(self, path="build", h=None):
        h = h or self.h
        decision = await h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/{path}"})
        )
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)
        return decision

    async def approved(self, path="build", h=None):
        h = h or self.h
        decision = await self.open(path, h)
        result = await h.service.approve(decision.approval_id, self.user)
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED)
        return decision

    async def use(self, decision, path="build", h=None):
        return await (h or self.h).broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/{path}"}),
            approval_id=decision.approval_id,
        )

    async def status(self, decision, h=None):
        return (await (h or self.h).approvals.get(decision.approval_id)).status

    async def test_each_terminal_state_revokes_and_no_other_state_does(self):
        from paw_backend.tasks import TERMINAL_STATES, TaskState

        self.assertEqual(
            TERMINAL_STATES,
            {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
        )
        for state in TaskState:
            with self.subTest(state=state.value):
                h = Harness(approvals=RevokeFailingStore())
                pending = await self.open("a", h)
                approved = await self.approved("b", h)
                await h.service.revoke_on_task_end(TaskEvent(TASK, state))
                expected = (
                    (ApprovalStatus.REVOKED, ApprovalStatus.REVOKED)
                    if state in TERMINAL_STATES
                    else (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
                )
                self.assertEqual(
                    (await self.status(pending, h), await self.status(approved, h)),
                    expected,
                )

    async def test_leaving_a_terminal_state_revokes_what_survived_the_end(self):
        # Retry / Restart re-open a failed or cancelled task: an approval that
        # is still open then belongs to the run that ended.
        from paw_backend.tasks import TaskState

        for old in (TaskState.FAILED, TaskState.CANCELLED):
            with self.subTest(old=old.value):
                h = Harness(approvals=RevokeFailingStore())
                approved = await self.approved("a", h)
                await h.service.revoke_on_task_end(
                    TaskEvent(TASK, TaskState.QUEUED, from_state=old)
                )
                self.assertEqual(await self.status(approved, h), ApprovalStatus.REVOKED)
        # ... and a transition between live states touches nothing
        h = Harness(approvals=RevokeFailingStore())
        approved = await self.approved("a", h)
        await h.service.revoke_on_task_end(
            TaskEvent(TASK, TaskState.RUNNING, from_state=TaskState.WAITING)
        )
        self.assertEqual(await self.status(approved, h), ApprovalStatus.APPROVED)

    async def test_a_store_failure_is_raised_without_its_message_never_counted_as_zero(
        self,
    ):
        from paw_backend.tasks import TERMINAL_STATES

        pending = await self.open()
        self.store.error = ConnectionError(SECRET)
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(ApprovalRevocationError) as caught:
                await self.h.service.revoke_task(TASK)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("ConnectionError", "\n".join(logs.output))
        for state in TERMINAL_STATES:  # the listener does not swallow it either
            with self.subTest(state=state.value):
                with self.assertLogs(level="ERROR"):
                    with self.assertRaises(ApprovalRevocationError):
                        await self.h.service.revoke_on_task_end(TaskEvent(TASK, state))
        # a live task never reaches the store, so it cannot fail either
        from paw_backend.tasks import TaskState

        await self.h.service.revoke_on_task_end(TaskEvent(TASK, TaskState.RUNNING))
        self.assertEqual(await self.status(pending), ApprovalStatus.PENDING)
        # revoking is idempotent: once the store is back it finishes the job
        self.store.error = None
        self.assertEqual(await self.h.service.revoke_task(TASK), 1)
        self.assertEqual(await self.h.service.revoke_task(TASK), 0)

    async def stalled_service(self, store, **kwargs):
        return ApprovalService(
            store,
            InMemoryAuditSink(),
            step_up=StepUp(True),
            clock=self.h.clock,
            timeout_seconds=0.05,
            **kwargs,
        )

    async def test_a_store_that_never_answers_cannot_hold_the_end_of_a_task_up(self):
        # Finding of the review of PR #74: TaskService awaits its listeners, so a
        # revocation that waits for a stalled database would hold up every cancel,
        # complete and retry request after the transition was committed.
        class Stalled(InMemoryApprovalStore):
            async def revoke_task(self, task_id, *, now):
                await asyncio.Event().wait()

        service = await self.stalled_service(Stalled())
        for call in (
            service.revoke_task(TASK),
            service.revoke_on_task_end(TaskEvent(TASK, TaskState.CANCELLED)),
        ):
            with self.subTest(call=call.__qualname__):
                with self.assertLogs(level="ERROR") as logs:
                    with self.assertRaises(ApprovalRevocationError):
                        async with asyncio.timeout(5):  # far above the 0.05 s limit
                            await call
                self.assertIn("TimeoutError", "\n".join(logs.output))

    async def test_a_lookup_that_never_answers_cannot_hold_the_revocation_up(self):
        class StalledLookup(InMemoryApprovalStore):
            stalled = False

            async def get(self, approval_id):
                if self.stalled:
                    await asyncio.Event().wait()
                return await super().get(approval_id)

        store = StalledLookup()
        h = Harness(approvals=store)
        await self.approved("a", h)
        store.stalled = True
        service = await self.stalled_service(store)
        async with asyncio.timeout(5):
            revoked = await service.revoke_task(TASK)
        # the revocation is done; only what the audit row / event would say
        # about the approval was not looked up
        self.assertEqual(revoked, 1)
        store.stalled = False
        self.assertEqual(
            [r.status for r in store._records.values()], [ApprovalStatus.REVOKED]
        )

    async def test_an_approval_left_open_is_unusable_once_the_task_ended(self):
        approved = await self.approved()
        self.store.error = ConnectionError("down")
        self.h.task_activity.answer = TaskActivity.ENDED
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(ApprovalRevocationError):
                await self.h.service.revoke_task(TASK)
        # the store still says "approved" ...
        self.assertEqual(await self.status(approved), ApprovalStatus.APPROVED)
        # ... and the broker still refuses, without using the approval up
        used = await self.use(approved)
        self.assertEqual(
            (used.verdict, used.reason, used.approval_id),
            (Verdict.DENY, R.TASK_NOT_ACTIVE, approved.approval_id),
        )
        self.assertIsNone(used.invocation)
        self.assertEqual(await self.status(approved), ApprovalStatus.APPROVED)
        # no new request is opened for a task that ended either
        before = len(self.store._records)
        again = await self.open_after_end("other")
        self.assertEqual(
            (again.verdict, again.reason), (Verdict.DENY, R.TASK_NOT_ACTIVE)
        )
        self.assertIsNone(again.approval_id)
        self.assertEqual(len(self.store._records), before)
        self.assertEqual(self.h.executor.invocations, [])

    async def open_after_end(self, path, h=None):
        return await (h or self.h).broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/{path}"})
        )

    async def test_while_the_task_is_alive_an_approval_is_used_as_before(self):
        approved = await self.approved()
        used = await self.use(approved)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.ALLOW, R.APPROVAL_CONSUMED)
        )
        self.assertEqual(self.h.task_activity.checks, [TASK, TASK])  # open + use

    async def test_a_task_that_ends_between_the_check_and_the_use_consumes_nothing(
        self,
    ):
        # The broker's own check answers ACTIVE, and then the task ends: what the
        # store sees, in the step that consumes, is the end. (In production the
        # store reads the task row locked in that transaction; see
        # test_tools_postgres.ConsumeRacesWithTaskEndTest.)
        for answer, reason in (
            (TaskActivity.ENDED, R.TASK_NOT_ACTIVE),
            (TaskActivity.UNKNOWN, R.TASK_UNKNOWN),
        ):
            with self.subTest(answer=answer.value):
                truth = FakeTaskActivity()
                store = InMemoryApprovalStore(task_activity=truth)
                h = Harness(approvals=store)  # the broker's provider says ACTIVE
                approved = await self.approved("a", h)
                truth.answer = answer  # the task ends after the broker's check
                used = await self.use(approved, "a", h)
                self.assertEqual(
                    (used.verdict, used.reason, used.invocation),
                    (Verdict.DENY, reason, None),
                )
                self.assertEqual(h.task_activity.checks, [TASK, TASK])  # open + use
                self.assertEqual(
                    await self.status(approved, h), ApprovalStatus.APPROVED
                )
                # once the revocation runs, it still finds the approval
                self.assertEqual(await h.service.revoke_task(TASK), 1)
                # and a task that is alive uses it, once
        truth = FakeTaskActivity()
        h = Harness(approvals=InMemoryApprovalStore(task_activity=truth))
        approved = await self.approved("a", h)
        used = await self.use(approved, "a", h)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.ALLOW, R.APPROVAL_CONSUMED)
        )
        self.assertEqual(truth.checks, [TASK])

    async def test_the_broker_asks_the_store_to_check_the_task_when_it_consumes(self):
        class Spy(InMemoryApprovalStore):
            calls: list = []

            async def consume(self, approval_id, binding, **arguments):
                self.calls.append(arguments)
                return await super().consume(approval_id, binding, **arguments)

        store = Spy()
        h = Harness(approvals=store)
        approved = await self.approved("a", h)
        await self.use(approved, "a", h)
        (arguments,) = store.calls
        self.assertIs(arguments["require_active_task"], True)

    async def test_a_reason_about_the_approval_itself_wins_over_the_task(self):
        # revoked, and the task ended: the store names what is wrong with the
        # approval (the broker's own check, before, names the task)
        truth = FakeTaskActivity()
        store = InMemoryApprovalStore(task_activity=truth)
        h = Harness(approvals=store)
        approved = await self.approved("a", h)
        await h.service.revoke_task(TASK)
        truth.answer = TaskActivity.ENDED
        used = await self.use(approved, "a", h)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_REVOKED)
        )

    async def test_a_task_that_is_not_known_or_answers_nonsense_denies(self):
        approved = await self.approved()
        cases = {
            "unknown task": (FakeTaskActivity(TaskActivity.UNKNOWN), R.TASK_UNKNOWN),
            "not an answer": (FakeTaskActivity("active"), R.TASK_STATE_UNAVAILABLE),
            "not even a string": (FakeTaskActivity(True), R.TASK_STATE_UNAVAILABLE),
            "no provider": (FailClosedTaskActivity(), R.TASK_UNKNOWN),
        }
        for label, (provider, reason) in cases.items():
            with self.subTest(label):
                h = Harness(approvals=self.store, task_activity=provider)
                used = await self.use(approved, h=h)
                opened = await self.open_after_end("x", h)
                self.assertEqual((used.verdict, used.reason), (Verdict.DENY, reason))
                self.assertEqual(
                    (opened.verdict, opened.reason), (Verdict.DENY, reason)
                )
                self.assertEqual(await self.status(approved), ApprovalStatus.APPROVED)

    async def test_a_provider_that_fails_is_a_denial_without_its_message(self):
        approved = await self.approved()
        h = Harness(
            approvals=self.store,
            task_activity=FakeTaskActivity(error=ConnectionError(SECRET)),
        )
        with self.assertLogs(level="ERROR") as logs:
            used = await self.use(approved, h=h)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.TASK_STATE_UNAVAILABLE)
        )
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertEqual(await self.status(approved), ApprovalStatus.APPROVED)

    async def test_a_provider_that_never_answers_is_a_denial(self):
        class Hangs:
            async def check(self, task_id):
                await asyncio.Event().wait()

        h = Harness(approvals=self.store, task_activity=Hangs(), timeout_seconds=0.05)
        opened = await self.open_after_end("x", h)
        self.assertEqual(
            (opened.verdict, opened.reason), (Verdict.DENY, R.TASK_STATE_UNAVAILABLE)
        )

    async def test_a_call_that_needs_no_approval_does_not_ask_about_the_task(self):
        self.h.task_activity.answer = TaskActivity.ENDED
        for tool, arguments in (
            ("repo.read_file", {"path": f"{ROOT}/a.py"}),
            ("repo.write_file", {"path": f"{ROOT}/a.py", "content": "1"}),
        ):
            with self.subTest(tool=tool):
                decision = await self.h.broker.request(make_call(tool, arguments))
                self.assertTrue(decision.allowed)
        self.assertEqual(self.h.task_activity.checks, [])

    async def test_the_question_is_about_the_task_of_the_call(self):
        other = uuid.UUID(int=502)
        await self.h.broker.request(
            make_call(
                "repo.delete_tree",
                {"path": f"{ROOT}/b"},
                context=make_context(task_id=other),
            )
        )
        self.assertEqual(self.h.task_activity.checks, [other])

    async def test_an_approval_that_ran_out_is_neither_revoked_nor_usable(self):
        approved = await self.approved()
        self.h.clock.advance(hours=2)  # past the hour an approval lives
        self.assertEqual(await self.h.service.revoke_task(TASK), 0)
        self.assertNotEqual(await self.status(approved), ApprovalStatus.REVOKED)
        used = await self.use(approved)
        self.assertEqual(
            (used.verdict, used.reason), (Verdict.DENY, R.APPROVAL_EXPIRED)
        )
        # and once its task has ended, the task is what the broker names
        self.h.task_activity.answer = TaskActivity.ENDED
        used = await self.use(approved)
        self.assertEqual((used.verdict, used.reason), (Verdict.DENY, R.TASK_NOT_ACTIVE))

    async def test_a_broker_needs_a_working_provider_interface(self):
        for provider in (object(), FakeTaskActivity, lambda task_id: None):
            with self.subTest(provider=repr(provider)[:30]):
                with self.assertRaises(TypeError):
                    Harness(task_activity=provider)


class HashInputsAndUndeclaredArgumentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_same_call_of_another_task_user_or_agent_has_its_own_approval(
        self,
    ):
        directory = StaticDirectory(
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
            principal(SystemRole.USER, U2, {P1: ProjectRole.CONTRIBUTOR}),
        )
        h = Harness(directory=directory)
        contexts = {
            "same": make_context(),
            "task": make_context(task_id=uuid.UUID(int=502)),
            "user": make_context(delegator_id=U2),
            "agent": make_context(
                grant=dataclasses.replace(make_grant(), agent_id=uuid.UUID(int=999))
            ),
        }
        decisions = {
            label: await h.broker.request(
                make_call("repo.delete_tree", DELETE, context=ctx)
            )
            for label, ctx in contexts.items()
        }
        for label, decision in decisions.items():
            with self.subTest(label=label):
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.NEEDS_APPROVAL, R.APPROVAL_REQUIRED),
                )
        self.assertEqual(len({d.approval_id for d in decisions.values()}), 4)
        self.assertEqual(len({d.call_hash for d in decisions.values()}), 4)
        self.assertEqual(len(h.approvals._records), 4)

    async def test_an_undeclared_argument_is_refused_even_when_the_count_matches(self):
        h = Harness()
        cases = [
            ("tests.run", {"selectr": "unit"}),  # one declared, one given: misspelt
            ("flag.toggle", {"enabled": True, "cont": 1}),
            ("repo.read_file", {"paht": f"{ROOT}/a"}),
            ("repo.write_file", {"path": f"{ROOT}/a", "contents": "x"}),
            ("web.fetch", {"URL": "https://github.com/x"}),
        ]
        for tool, arguments in cases:
            with self.subTest(tool=tool, arguments=list(arguments)):
                decision = await h.broker.request(make_call(tool, arguments))
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.DENY, R.INVALID_ARGUMENTS),
                )
        self.assertEqual(h.executor.invocations, [])


class ServiceConstructionTest(unittest.TestCase):
    def test_adapters_are_validated_up_front(self):
        sink = InMemoryAuditSink()

        class NoMethods:
            pass

        class SyncVerify:
            def verify(self, user_id, approval_id):
                return True

        class OneArg:
            async def verify(self, user_id):
                return True

        for kwargs in (
            {"store": NoMethods()},
            {"audit": NoMethods()},
            {"step_up": NoMethods()},
            {"step_up": SyncVerify()},
            {"step_up": OneArg()},
            {"timeout_seconds": 0},
            {"listeners": [lambda: None]},
        ):
            with self.subTest(kwargs=list(kwargs)):
                arguments = {"store": InMemoryApprovalStore(), "audit": sink, **kwargs}
                with self.assertRaises((TypeError, ValueError)):
                    ApprovalService(**arguments)
        ApprovalService(InMemoryApprovalStore(), sink)


if __name__ == "__main__":
    unittest.main()
