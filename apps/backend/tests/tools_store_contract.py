"""The rules every approval store must obey, run against each implementation."""

import asyncio
import hashlib
import uuid
from datetime import timedelta

from paw_backend.tools import (
    ApprovalBinding,
    ApprovalEventKind,
    ApprovalLevel,
    ApprovalStatus,
    ConsumeOutcome,
    DecideOutcome,
    NewApproval,
    OpenLimits,
    OpenOutcome,
    RevokeOutcome,
    SummaryItem,
    TaskRun,
)
from paw_backend.tools.scope import Target, TargetKind

from .tools_support import AGENT, NOW, P1, ROOT, RUN, U1, U2

LIMITS = OpenLimits(max_pending=10, rejection_cooldown=timedelta(minutes=5))


def new_approval(**overrides) -> NewApproval:
    arguments = {
        "approval_id": uuid.uuid4(),
        # A task of its own, so that tests sharing a database do not fill each
        # other's per-task cap.
        "task_id": uuid.uuid4(),
        "task_run": RUN,
        "project_id": P1,
        "agent_id": AGENT,
        "requester_user_id": U1,
        "tool": "repo.delete_tree",
        "level": ApprovalLevel.APPROVAL,
        "call_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        "targets": (Target(TargetKind.PATH, f"{ROOT}/x"),),
        "summary": (SummaryItem("path", "path", f"{ROOT}/x"),),
        "expires_at": NOW + timedelta(hours=1),
    }
    arguments.update(overrides)
    return NewApproval(**arguments)


def binding_of(new: NewApproval, **overrides) -> ApprovalBinding:
    arguments = {
        "task_id": new.task_id,
        "task_run": new.task_run,
        "agent_id": new.agent_id,
        "requester_user_id": new.requester_user_id,
        "tool": new.tool,
        "level": new.level,
        "call_hash": new.call_hash,
    }
    arguments.update(overrides)
    return ApprovalBinding(**arguments)


HOUR = timedelta(hours=1)


class StoreContract:
    """Mixin for an ``IsolatedAsyncioTestCase``; the class provides ``self.store``."""

    store = None

    async def kinds(self, approval_id) -> list[ApprovalEventKind]:
        return [h.kind for h in await self.store.history(approval_id)]

    async def approved(self, **overrides) -> NewApproval:
        new = new_approval(**overrides)
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        result = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(result.outcome, DecideOutcome.DECIDED)
        return new

    # -- open_request --

    async def test_a_request_is_stored_pending_with_its_history(self):
        new = new_approval()
        opened = await self.store.open_request(new, now=NOW, limits=LIMITS)
        self.assertTrue(opened.created)
        record = opened.record
        self.assertEqual(
            (
                record.approval_id,
                record.task_id,
                record.task_run,
                record.project_id,
                record.agent_id,
                record.requester_user_id,
                record.tool,
                record.level,
                record.call_hash,
                record.targets,
                record.status,
                record.created_at,
                record.expires_at,
                record.approver_id,
                record.decided_at,
                record.consumed_at,
            ),
            (
                new.approval_id,
                new.task_id,
                RUN,
                P1,
                AGENT,
                U1,
                "repo.delete_tree",
                ApprovalLevel.APPROVAL,
                new.call_hash,
                new.targets,
                ApprovalStatus.PENDING,
                NOW,
                NOW + HOUR,
                None,
                None,
                None,
            ),
        )
        self.assertEqual(await self.store.get(new.approval_id), record)
        history = await self.store.history(new.approval_id)
        self.assertEqual(
            [(h.kind, h.actor_user_id, h.agent_id, h.occurred_at) for h in history],
            [(ApprovalEventKind.REQUESTED, None, AGENT, NOW)],
        )

    async def test_an_unknown_approval_is_not_found(self):
        self.assertIsNone(await self.store.get(uuid.uuid4()))
        self.assertEqual(await self.store.history(uuid.uuid4()), [])

    async def test_the_same_call_opens_one_request(self):
        first = new_approval()
        again = new_approval(call_hash=first.call_hash)
        a = await self.store.open_request(first, now=NOW, limits=LIMITS)
        b = await self.store.open_request(
            again, now=NOW + timedelta(minutes=5), limits=LIMITS
        )
        self.assertEqual((a.created, b.created), (True, False))
        self.assertEqual(b.record.approval_id, first.approval_id)
        self.assertEqual(
            await self.kinds(first.approval_id), [ApprovalEventKind.REQUESTED]
        )
        self.assertIsNone(await self.store.get(again.approval_id))

    async def test_an_approved_but_unused_request_is_also_open(self):
        first = await self.approved()
        again = new_approval(call_hash=first.call_hash)
        opened = await self.store.open_request(again, now=NOW, limits=LIMITS)
        self.assertEqual(
            (opened.created, opened.record.status), (False, ApprovalStatus.APPROVED)
        )
        self.assertEqual(opened.record.approval_id, first.approval_id)

    async def test_a_new_request_can_follow_an_expired_one(self):
        first = new_approval()
        await self.store.open_request(first, now=NOW, limits=LIMITS)
        later = NOW + HOUR
        second = new_approval(call_hash=first.call_hash, expires_at=later + HOUR)
        opened = await self.store.open_request(second, now=later, limits=LIMITS)
        self.assertTrue(opened.created)
        self.assertEqual(opened.record.approval_id, second.approval_id)
        old = await self.store.get(first.approval_id)
        self.assertEqual(old.status, ApprovalStatus.EXPIRED)
        self.assertEqual(
            await self.kinds(first.approval_id),
            [ApprovalEventKind.REQUESTED, ApprovalEventKind.EXPIRED],
        )

    async def test_a_new_request_can_follow_a_consumed_one_and_a_rejected_one_later(
        self,
    ):
        used = await self.approved()
        await self.store.consume(used.approval_id, binding_of(used), now=NOW)
        again = new_approval(task_id=used.task_id, call_hash=used.call_hash)
        self.assertTrue(
            (await self.store.open_request(again, now=NOW, limits=LIMITS)).created
        )
        rejected = new_approval()
        await self.store.open_request(rejected, now=NOW, limits=LIMITS)
        await self.store.decide(
            rejected.approval_id, approver_id=U1, approve=False, now=NOW
        )
        again = new_approval(
            task_id=rejected.task_id,
            call_hash=rejected.call_hash,
            expires_at=NOW + timedelta(hours=2),
        )
        later = NOW + timedelta(minutes=6)  # after the cooldown
        self.assertTrue(
            (await self.store.open_request(again, now=later, limits=LIMITS)).created
        )

    async def test_different_calls_do_not_share_a_request(self):
        a = await self.store.open_request(new_approval(), now=NOW, limits=LIMITS)
        b = await self.store.open_request(new_approval(), now=NOW, limits=LIMITS)
        self.assertTrue(a.created and b.created)
        self.assertNotEqual(a.record.approval_id, b.record.approval_id)

    # -- decide --

    async def test_the_delegating_user_approves(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        at = NOW + timedelta(minutes=3)
        result = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=at
        )
        self.assertEqual(result.outcome, DecideOutcome.DECIDED)
        self.assertEqual(
            (result.record.status, result.record.approver_id, result.record.decided_at),
            (ApprovalStatus.APPROVED, U1, at),
        )
        history = await self.store.history(new.approval_id)
        self.assertEqual(
            [(h.kind, h.actor_user_id, h.agent_id) for h in history],
            [
                (ApprovalEventKind.REQUESTED, None, AGENT),
                (ApprovalEventKind.APPROVED, U1, None),
            ],
        )
        self.assertEqual([h.seq for h in history], sorted({h.seq for h in history}))

    async def test_rejecting_is_final(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        result = await self.store.decide(
            new.approval_id, approver_id=U1, approve=False, now=NOW
        )
        self.assertEqual(result.record.status, ApprovalStatus.REJECTED)
        again = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(again.outcome, DecideOutcome.NOT_PENDING)
        self.assertEqual(
            await self.store.consume(new.approval_id, binding_of(new), now=NOW),
            ConsumeOutcome.REJECTED,
        )

    async def test_nobody_but_the_delegating_user_decides(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        for who in (AGENT, U2, uuid.uuid4()):
            for approve in (True, False):
                with self.subTest(who=who, approve=approve):
                    result = await self.store.decide(
                        new.approval_id, approver_id=who, approve=approve, now=NOW
                    )
                    self.assertEqual(result.outcome, DecideOutcome.NOT_AUTHORISED)
                    self.assertIsNone(result.record)
        record = await self.store.get(new.approval_id)
        self.assertEqual(
            (record.status, record.approver_id), (ApprovalStatus.PENDING, None)
        )
        self.assertEqual(
            await self.kinds(new.approval_id), [ApprovalEventKind.REQUESTED]
        )

    async def test_an_unknown_approval_cannot_be_decided(self):
        result = await self.store.decide(
            uuid.uuid4(), approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(result.outcome, DecideOutcome.NOT_FOUND)

    async def test_a_decision_is_made_once(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        first = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        second = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(
            (first.outcome, second.outcome),
            (DecideOutcome.DECIDED, DecideOutcome.NOT_PENDING),
        )
        self.assertEqual(
            await self.kinds(new.approval_id),
            [ApprovalEventKind.REQUESTED, ApprovalEventKind.APPROVED],
        )

    async def test_an_expired_request_cannot_be_decided_and_is_marked_expired(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        # exactly at expiry counts as expired; one microsecond earlier does not
        just_before = NOW + HOUR - timedelta(microseconds=1)
        result = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW + HOUR
        )
        self.assertEqual(result.outcome, DecideOutcome.EXPIRED)
        record = await self.store.get(new.approval_id)
        self.assertEqual(
            (record.status, record.approver_id), (ApprovalStatus.EXPIRED, None)
        )
        self.assertEqual(
            await self.kinds(new.approval_id),
            [ApprovalEventKind.REQUESTED, ApprovalEventKind.EXPIRED],
        )
        other = new_approval()
        await self.store.open_request(other, now=NOW, limits=LIMITS)
        ok = await self.store.decide(
            other.approval_id, approver_id=U1, approve=True, now=just_before
        )
        self.assertEqual(ok.outcome, DecideOutcome.DECIDED)

    async def test_an_approved_request_cannot_be_decided_again_after_expiry(self):
        new = await self.approved()
        result = await self.store.decide(
            new.approval_id, approver_id=U1, approve=False, now=NOW + 2 * HOUR
        )
        self.assertEqual(result.outcome, DecideOutcome.NOT_PENDING)

    # -- consume --

    async def test_an_approval_is_consumed_once(self):
        new = await self.approved()
        at = NOW + timedelta(minutes=10)
        first = await self.store.consume(new.approval_id, binding_of(new), now=at)
        second = await self.store.consume(new.approval_id, binding_of(new), now=at)
        self.assertEqual(
            (first, second), (ConsumeOutcome.CONSUMED, ConsumeOutcome.ALREADY_USED)
        )
        record = await self.store.get(new.approval_id)
        self.assertEqual(
            (record.status, record.consumed_at), (ApprovalStatus.CONSUMED, at)
        )
        history = await self.store.history(new.approval_id)
        self.assertEqual(
            [(h.kind, h.agent_id) for h in history][-1],
            (ApprovalEventKind.CONSUMED, AGENT),
        )
        self.assertEqual(
            [h.kind for h in history],
            [
                ApprovalEventKind.REQUESTED,
                ApprovalEventKind.APPROVED,
                ApprovalEventKind.CONSUMED,
            ],
        )

    async def test_a_pending_approval_cannot_be_consumed(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        self.assertEqual(
            await self.store.consume(new.approval_id, binding_of(new), now=NOW),
            ConsumeOutcome.PENDING,
        )
        self.assertEqual(
            (await self.store.get(new.approval_id)).status, ApprovalStatus.PENDING
        )

    async def test_an_approval_is_only_good_for_the_exact_call_it_was_granted_for(self):
        new = await self.approved()
        mismatches = {
            "task": {"task_id": uuid.uuid4()},
            "agent": {"agent_id": uuid.uuid4()},
            "user": {"requester_user_id": U2},
            "tool": {"tool": "host.install_package"},
            "level": {"level": ApprovalLevel.STRONG_APPROVAL},
            "hash": {"call_hash": hashlib.sha256(b"another call").hexdigest()},
        }
        for label, override in mismatches.items():
            with self.subTest(differs=label):
                outcome = await self.store.consume(
                    new.approval_id, binding_of(new, **override), now=NOW
                )
                self.assertEqual(outcome, ConsumeOutcome.MISMATCH)
        # none of that used it up
        self.assertEqual(
            await self.store.consume(new.approval_id, binding_of(new), now=NOW),
            ConsumeOutcome.CONSUMED,
        )

    async def test_a_request_stores_the_run_it_was_made_in(self):
        for run in (TaskRun(1, 0), TaskRun(3, 0), TaskRun(2, 5)):
            with self.subTest(run=run):
                new = new_approval(task_run=run)
                opened = await self.store.open_request(new, now=NOW, limits=LIMITS)
                self.assertEqual(opened.record.task_run, run)
                self.assertEqual((await self.store.get(new.approval_id)).task_run, run)

    async def test_an_approval_can_only_be_used_in_the_run_it_was_requested_in(self):
        new = await self.approved(task_run=TaskRun(2, 1))
        for run in (TaskRun(1, 1), TaskRun(3, 1), TaskRun(2, 0), TaskRun(2, 2)):
            with self.subTest(run=run):
                outcome = await self.store.consume(
                    new.approval_id, binding_of(new, task_run=run), now=NOW
                )
                self.assertEqual(outcome, ConsumeOutcome.SUPERSEDED)
        # none of that used it up, and it left no trace in the history
        self.assertEqual(
            (await self.store.get(new.approval_id)).status, ApprovalStatus.APPROVED
        )
        self.assertEqual(
            await self.kinds(new.approval_id),
            [ApprovalEventKind.REQUESTED, ApprovalEventKind.APPROVED],
        )
        self.assertEqual(
            await self.store.consume(new.approval_id, binding_of(new), now=NOW),
            ConsumeOutcome.CONSUMED,
        )

    async def test_a_pending_approval_of_another_run_is_superseded_not_pending(self):
        # it can never be used by that run, so waiting for a decision is pointless
        new = new_approval(task_run=TaskRun(1, 0))
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        outcome = await self.store.consume(
            new.approval_id, binding_of(new, task_run=TaskRun(2, 0)), now=NOW
        )
        self.assertEqual(outcome, ConsumeOutcome.SUPERSEDED)

    async def test_what_an_approval_is_says_more_than_the_run_it_was_requested_in(self):
        other_run = TaskRun(2, 0)
        # a call that differs in anything else than the run is a mismatch
        new = await self.approved()
        outcome = await self.store.consume(
            new.approval_id,
            binding_of(new, task_run=other_run, tool="host.install_package"),
            now=NOW,
        )
        self.assertEqual(outcome, ConsumeOutcome.MISMATCH)
        # a finished approval reports how it finished, whatever run asks
        used = await self.approved()
        await self.store.consume(used.approval_id, binding_of(used), now=NOW)
        revoked = await self.approved()
        await self.store.revoke(revoked.approval_id, actor_id=U1, now=NOW)
        rejected = new_approval()
        await self.store.open_request(rejected, now=NOW, limits=LIMITS)
        await self.store.decide(
            rejected.approval_id, approver_id=U1, approve=False, now=NOW
        )
        expired = await self.approved()
        for label, approval, at, expected in (
            ("used", used, NOW, ConsumeOutcome.ALREADY_USED),
            ("revoked", revoked, NOW, ConsumeOutcome.REVOKED),
            ("rejected", rejected, NOW, ConsumeOutcome.REJECTED),
            ("expired", expired, NOW + 2 * HOUR, ConsumeOutcome.EXPIRED),
        ):
            with self.subTest(approval=label):
                outcome = await self.store.consume(
                    approval.approval_id,
                    binding_of(approval, task_run=other_run),
                    now=at,
                )
                self.assertEqual(outcome, expected)

    async def test_an_unknown_approval_cannot_be_consumed(self):
        new = new_approval()
        self.assertEqual(
            await self.store.consume(uuid.uuid4(), binding_of(new), now=NOW),
            ConsumeOutcome.NOT_FOUND,
        )

    async def test_an_approval_expires_whatever_state_it_is_in(self):
        new = await self.approved()
        just_before = NOW + HOUR - timedelta(microseconds=1)
        early = new_approval()
        await self.store.open_request(early, now=NOW, limits=LIMITS)
        await self.store.decide(
            early.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(
            await self.store.consume(
                early.approval_id, binding_of(early), now=just_before
            ),
            ConsumeOutcome.CONSUMED,
        )
        self.assertEqual(
            await self.store.consume(new.approval_id, binding_of(new), now=NOW + HOUR),
            ConsumeOutcome.EXPIRED,
        )
        record = await self.store.get(new.approval_id)
        self.assertEqual(
            (record.status, record.consumed_at), (ApprovalStatus.EXPIRED, None)
        )
        self.assertEqual(
            await self.kinds(new.approval_id),
            [
                ApprovalEventKind.REQUESTED,
                ApprovalEventKind.APPROVED,
                ApprovalEventKind.EXPIRED,
            ],
        )
        # and it stays expired
        self.assertEqual(
            await self.store.consume(new.approval_id, binding_of(new), now=NOW),
            ConsumeOutcome.EXPIRED,
        )

    async def test_a_pending_request_that_ran_out_is_expired_not_pending(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        self.assertEqual(
            await self.store.consume(
                new.approval_id, binding_of(new), now=NOW + 2 * HOUR
            ),
            ConsumeOutcome.EXPIRED,
        )

    # -- concurrency --

    async def test_of_many_simultaneous_approvals_exactly_one_wins(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        results = await asyncio.gather(
            *(
                self.store.decide(
                    new.approval_id, approver_id=U1, approve=True, now=NOW
                )
                for _ in range(20)
            )
        )
        outcomes = sorted(r.outcome.value for r in results)
        self.assertEqual(outcomes, ["decided"] + ["not_pending"] * 19)
        self.assertEqual(
            await self.kinds(new.approval_id),
            [ApprovalEventKind.REQUESTED, ApprovalEventKind.APPROVED],
        )

    async def test_an_approval_and_a_rejection_racing_leave_one_decision(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        results = await asyncio.gather(
            *(
                self.store.decide(
                    new.approval_id, approver_id=U1, approve=i % 2 == 0, now=NOW
                )
                for i in range(20)
            )
        )
        decided = [r for r in results if r.outcome is DecideOutcome.DECIDED]
        self.assertEqual(len(decided), 1)
        record = await self.store.get(new.approval_id)
        self.assertEqual(record.status, decided[0].record.status)
        kinds = await self.kinds(new.approval_id)
        self.assertEqual(len(kinds), 2)

    async def test_of_many_simultaneous_uses_exactly_one_consumes(self):
        new = await self.approved()
        outcomes = await asyncio.gather(
            *(
                self.store.consume(new.approval_id, binding_of(new), now=NOW)
                for _ in range(20)
            )
        )
        self.assertEqual(
            sorted(o.value for o in outcomes), ["already_used"] * 19 + ["consumed"]
        )
        self.assertEqual(
            (await self.kinds(new.approval_id)).count(ApprovalEventKind.CONSUMED), 1
        )

    async def test_simultaneous_requests_for_one_call_open_one_approval(self):
        first = new_approval()
        requests = [new_approval(call_hash=first.call_hash) for _ in range(12)]
        opened = await asyncio.gather(
            *(self.store.open_request(new, now=NOW, limits=LIMITS) for new in requests)
        )
        self.assertEqual(sum(o.created for o in opened), 1)
        self.assertEqual(len({o.record.approval_id for o in opened}), 1)
        winner = opened[0].record.approval_id
        self.assertEqual(await self.kinds(winner), [ApprovalEventKind.REQUESTED])

    # -- what the approver is shown --

    async def test_the_summary_is_stored_with_the_request_and_its_history(self):
        summary = (
            SummaryItem("package", "text", "evil-backdoor==1.0"),
            SummaryItem("url", "url", "https://example.org/x?data=SECRETBYTES"),
        )
        new = new_approval(summary=summary)
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        record = await self.store.get(new.approval_id)
        self.assertEqual(record.summary, summary)
        history = await self.store.history(new.approval_id)
        self.assertEqual(history[0].summary, summary)
        await self.store.decide(new.approval_id, approver_id=U1, approve=True, now=NOW)
        history = await self.store.history(new.approval_id)
        self.assertEqual([h.summary for h in history][1:], [None])

    # -- the cap on open approvals --

    async def test_a_task_and_user_may_hold_only_so_many_open_approvals(self):
        limits = OpenLimits(max_pending=3, rejection_cooldown=timedelta(minutes=5))
        task_id = uuid.uuid4()
        opened = [
            await self.store.open_request(
                new_approval(task_id=task_id), now=NOW, limits=limits
            )
            for _ in range(3)
        ]
        self.assertEqual([o.outcome for o in opened], [OpenOutcome.CREATED] * 3)
        refused = await self.store.open_request(
            new_approval(task_id=task_id), now=NOW, limits=limits
        )
        self.assertEqual(
            (refused.outcome, refused.record), (OpenOutcome.TOO_MANY_PENDING, None)
        )
        # ... the same call is still found (it does not count twice) ...
        again = await self.store.open_request(
            new_approval(task_id=task_id, call_hash=opened[0].record.call_hash),
            now=NOW,
            limits=limits,
        )
        self.assertEqual(again.outcome, OpenOutcome.EXISTING)
        # ... another task is not affected ...
        other = await self.store.open_request(new_approval(), now=NOW, limits=limits)
        self.assertEqual(other.outcome, OpenOutcome.CREATED)
        # ... and a decided, revoked or expired approval frees its place.
        await self.store.decide(
            opened[0].record.approval_id, approver_id=U1, approve=False, now=NOW
        )
        freed = await self.store.open_request(
            new_approval(task_id=task_id), now=NOW, limits=limits
        )
        self.assertEqual(freed.outcome, OpenOutcome.CREATED)
        self.assertEqual(
            (
                await self.store.open_request(
                    new_approval(task_id=task_id), now=NOW, limits=limits
                )
            ).outcome,
            OpenOutcome.TOO_MANY_PENDING,
        )
        await self.store.revoke(opened[1].record.approval_id, actor_id=U1, now=NOW)
        later = NOW + HOUR  # the other two ran out
        expired = await self.store.open_request(
            new_approval(task_id=task_id, expires_at=later + HOUR),
            now=later,
            limits=limits,
        )
        self.assertEqual(expired.outcome, OpenOutcome.CREATED)

    async def test_the_cap_counts_approved_but_unused_approvals(self):
        limits = OpenLimits(max_pending=2, rejection_cooldown=timedelta(minutes=5))
        task_id = uuid.uuid4()
        for _ in range(2):
            new = new_approval(task_id=task_id)
            await self.store.open_request(new, now=NOW, limits=limits)
            await self.store.decide(
                new.approval_id, approver_id=U1, approve=True, now=NOW
            )
        refused = await self.store.open_request(
            new_approval(task_id=task_id), now=NOW, limits=limits
        )
        self.assertEqual(refused.outcome, OpenOutcome.TOO_MANY_PENDING)

    async def test_a_different_user_of_the_same_task_has_a_cap_of_their_own(self):
        limits = OpenLimits(max_pending=1, rejection_cooldown=timedelta(minutes=5))
        task_id = uuid.uuid4()
        a = await self.store.open_request(
            new_approval(task_id=task_id), now=NOW, limits=limits
        )
        b = await self.store.open_request(
            new_approval(task_id=task_id, requester_user_id=U2), now=NOW, limits=limits
        )
        self.assertEqual(
            (a.outcome, b.outcome), (OpenOutcome.CREATED, OpenOutcome.CREATED)
        )

    async def test_simultaneous_requests_cannot_exceed_the_cap(self):
        limits = OpenLimits(max_pending=5, rejection_cooldown=timedelta(minutes=5))
        task_id = uuid.uuid4()
        opened = await asyncio.gather(
            *(
                self.store.open_request(
                    new_approval(task_id=task_id), now=NOW, limits=limits
                )
                for _ in range(30)
            )
        )
        outcomes = sorted(o.outcome.value for o in opened)
        self.assertEqual(outcomes, ["created"] * 5 + ["too_many_pending"] * 25)

    # -- a rejection is not forgotten at once --

    async def test_a_rejected_call_cannot_be_asked_again_during_the_cooldown(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        await self.store.decide(new.approval_id, approver_id=U1, approve=False, now=NOW)
        again = new_approval(task_id=new.task_id, call_hash=new.call_hash)
        for minutes in (0, 1, 4):
            refused = await self.store.open_request(
                again, now=NOW + timedelta(minutes=minutes), limits=LIMITS
            )
            self.assertEqual(
                (refused.outcome, refused.record), (OpenOutcome.COOLING_DOWN, None)
            )
        # the cooldown ends exactly after its length
        ok = await self.store.open_request(
            new_approval(
                task_id=new.task_id,
                call_hash=new.call_hash,
                expires_at=NOW + timedelta(hours=2),
            ),
            now=NOW + timedelta(minutes=5, microseconds=1),
            limits=LIMITS,
        )
        self.assertEqual(ok.outcome, OpenOutcome.CREATED)

    async def test_a_rejection_only_cools_down_that_exact_call(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        await self.store.decide(new.approval_id, approver_id=U1, approve=False, now=NOW)
        other = await self.store.open_request(
            new_approval(task_id=new.task_id), now=NOW, limits=LIMITS
        )
        self.assertEqual(other.outcome, OpenOutcome.CREATED)

    async def test_the_cooldown_length_is_the_configured_one(self):
        limits = OpenLimits(max_pending=10, rejection_cooldown=timedelta(hours=1))
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=limits)
        await self.store.decide(new.approval_id, approver_id=U1, approve=False, now=NOW)
        again = new_approval(task_id=new.task_id, call_hash=new.call_hash)
        refused = await self.store.open_request(
            again, now=NOW + timedelta(minutes=59), limits=limits
        )
        self.assertEqual(refused.outcome, OpenOutcome.COOLING_DOWN)

    # -- revoke --

    async def test_a_pending_or_approved_approval_can_be_revoked_once(self):
        for approve_first in (False, True):
            with self.subTest(approved=approve_first):
                new = new_approval()
                await self.store.open_request(new, now=NOW, limits=LIMITS)
                if approve_first:
                    await self.store.decide(
                        new.approval_id, approver_id=U1, approve=True, now=NOW
                    )
                at = NOW + timedelta(minutes=2)
                first = await self.store.revoke(new.approval_id, actor_id=U1, now=at)
                second = await self.store.revoke(new.approval_id, actor_id=U1, now=at)
                self.assertEqual(
                    (first, second), (RevokeOutcome.REVOKED, RevokeOutcome.NOT_OPEN)
                )
                record = await self.store.get(new.approval_id)
                self.assertEqual(
                    (record.status, record.revoked_at, record.revoked_by),
                    (ApprovalStatus.REVOKED, at, U1),
                )
                history = await self.store.history(new.approval_id)
                self.assertEqual(
                    (history[-1].kind, history[-1].actor_user_id),
                    (ApprovalEventKind.REVOKED, U1),
                )
                self.assertEqual(
                    await self.store.consume(new.approval_id, binding_of(new), now=at),
                    ConsumeOutcome.REVOKED,
                )
                decided = await self.store.decide(
                    new.approval_id, approver_id=U1, approve=True, now=at
                )
                self.assertEqual(decided.outcome, DecideOutcome.NOT_PENDING)

    async def test_finished_or_unknown_approvals_cannot_be_revoked(self):
        used = await self.approved()
        await self.store.consume(used.approval_id, binding_of(used), now=NOW)
        rejected = new_approval()
        await self.store.open_request(rejected, now=NOW, limits=LIMITS)
        await self.store.decide(
            rejected.approval_id, approver_id=U1, approve=False, now=NOW
        )
        stale = new_approval()
        await self.store.open_request(stale, now=NOW, limits=LIMITS)
        for approval_id in (used.approval_id, rejected.approval_id):
            self.assertEqual(
                await self.store.revoke(approval_id, actor_id=U1, now=NOW),
                RevokeOutcome.NOT_OPEN,
            )
        self.assertEqual(
            await self.store.revoke(stale.approval_id, actor_id=U1, now=NOW + HOUR),
            RevokeOutcome.NOT_OPEN,  # ran out already
        )
        self.assertEqual(
            await self.store.revoke(uuid.uuid4(), actor_id=U1, now=NOW),
            RevokeOutcome.NOT_FOUND,
        )
        self.assertEqual(
            (await self.store.get(used.approval_id)).status, ApprovalStatus.CONSUMED
        )

    async def test_the_open_approvals_of_a_task_are_revoked_together(self):
        task_id = uuid.uuid4()
        mine = [new_approval(task_id=task_id) for _ in range(3)]
        for new in mine:
            await self.store.open_request(new, now=NOW, limits=LIMITS)
        await self.store.decide(
            mine[0].approval_id, approver_id=U1, approve=True, now=NOW
        )
        await self.store.decide(
            mine[1].approval_id, approver_id=U1, approve=False, now=NOW
        )
        elsewhere = new_approval()
        await self.store.open_request(elsewhere, now=NOW, limits=LIMITS)
        revoked = await self.store.revoke_task(task_id, now=NOW)
        # the approved one and the pending one; not the rejected, not another task's
        self.assertEqual(
            sorted(revoked), sorted([mine[0].approval_id, mine[2].approval_id])
        )
        self.assertEqual(
            (await self.store.get(mine[1].approval_id)).status, ApprovalStatus.REJECTED
        )
        self.assertEqual(
            (await self.store.get(elsewhere.approval_id)).status, ApprovalStatus.PENDING
        )
        record = await self.store.get(mine[0].approval_id)
        self.assertEqual(
            (record.status, record.revoked_by), (ApprovalStatus.REVOKED, None)
        )
        history = await self.store.history(mine[0].approval_id)
        self.assertEqual(
            (history[-1].kind, history[-1].actor_user_id),
            (ApprovalEventKind.REVOKED, None),
        )
        self.assertEqual(await self.store.revoke_task(task_id, now=NOW), [])

    async def test_of_many_simultaneous_revocations_exactly_one_wins(self):
        new = await self.approved()
        outcomes = await asyncio.gather(
            *(
                self.store.revoke(new.approval_id, actor_id=U1, now=NOW)
                for _ in range(20)
            )
        )
        self.assertEqual(
            sorted(o.value for o in outcomes), ["not_open"] * 19 + ["revoked"]
        )

    # -- step-up --

    async def test_a_strong_approval_is_only_granted_with_a_step_up(self):
        new = new_approval(level=ApprovalLevel.STRONG_APPROVAL)
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        refused = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(refused.outcome, DecideOutcome.STEP_UP_REQUIRED)
        self.assertEqual(
            (await self.store.get(new.approval_id)).status, ApprovalStatus.PENDING
        )
        granted = await self.store.decide(
            new.approval_id,
            approver_id=U1,
            approve=True,
            now=NOW,
            step_up_verified=True,
        )
        self.assertEqual(granted.outcome, DecideOutcome.DECIDED)
        self.assertTrue(granted.record.step_up_verified)

    async def test_rejecting_needs_no_step_up_and_records_none(self):
        new = new_approval(level=ApprovalLevel.STRONG_APPROVAL)
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        result = await self.store.decide(
            new.approval_id,
            approver_id=U1,
            approve=False,
            now=NOW,
            step_up_verified=True,
        )
        self.assertEqual(result.outcome, DecideOutcome.DECIDED)
        self.assertFalse(result.record.step_up_verified)

    async def test_an_ordinary_approval_records_whether_a_step_up_was_given(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW, limits=LIMITS)
        result = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertFalse(result.record.step_up_verified)
