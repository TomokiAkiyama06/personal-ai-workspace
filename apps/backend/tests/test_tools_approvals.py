import asyncio
import contextlib
import dataclasses
import unittest
import uuid
from datetime import timedelta

from paw_backend.authz import (
    InMemoryAuditSink,
    ProjectRole,
    SystemRole,
)
from paw_backend.tools import (
    ApprovalEventKind,
    ApprovalLevel,
    ApprovalOutcome,
    ApprovalService,
    ApprovalStatus,
    BrokerReason,
    BudgetStatus,
    FailClosedStepUp,
    InMemoryApprovalStore,
    TaskScope,
    ToolRegistry,
    Verdict,
)
from paw_backend.tools.scope import Target, TargetKind

from .authz_support import AGENT, SECRET, FailingSink, StaticDirectory, principal
from .tools_store_contract import StoreContract, new_approval
from .tools_support import (
    NOW,
    P1,
    ROOT,
    TASK,
    U1,
    U2,
    Harness,
    StepUp,
    make_call,
    make_context,
    make_grant,
    sample_registry,
)

R = BrokerReason
U3 = uuid.UUID(int=3)
DELETE = {"path": f"{ROOT}/build"}
MERGE = {
    "remote": "https://github.com/org/repo.git",
    "credential": "cred_" + "a1" * 16,
    "pull_request": 7,
}
HOUR = timedelta(hours=1)


class InMemoryStoreTest(StoreContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemoryApprovalStore()

    async def test_the_test_double_is_bounded(self):
        small = InMemoryApprovalStore(max_records=2)
        await small.open_request(new_approval(), now=NOW)
        await small.open_request(new_approval(), now=NOW)
        with self.assertRaises(RuntimeError):
            await small.open_request(new_approval(), now=NOW)


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
                    self.assertEqual(result.outcome, ApprovalOutcome.NOT_AUTHORISED)
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
        stranger = await service.approve(
            (await self.open_merge()).approval_id, principal(SystemRole.OWNER, U3)
        )
        self.assertEqual(stranger.outcome, ApprovalOutcome.NOT_AUTHORISED)
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

            async def consume(self, approval_id, binding, *, now):
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

            async def consume(self, approval_id, binding, *, now):
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
