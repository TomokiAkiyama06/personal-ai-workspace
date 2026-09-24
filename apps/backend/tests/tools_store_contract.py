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
)
from paw_backend.tools.scope import Target, TargetKind

from .tools_support import AGENT, NOW, P1, ROOT, TASK, U1, U2


def new_approval(**overrides) -> NewApproval:
    arguments = {
        "approval_id": uuid.uuid4(),
        "task_id": TASK,
        "project_id": P1,
        "agent_id": AGENT,
        "requester_user_id": U1,
        "tool": "repo.delete_tree",
        "level": ApprovalLevel.APPROVAL,
        "call_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        "targets": (Target(TargetKind.PATH, f"{ROOT}/x"),),
        "expires_at": NOW + timedelta(hours=1),
    }
    arguments.update(overrides)
    return NewApproval(**arguments)


def binding_of(new: NewApproval, **overrides) -> ApprovalBinding:
    arguments = {
        "task_id": new.task_id,
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
        await self.store.open_request(new, now=NOW)
        result = await self.store.decide(
            new.approval_id, approver_id=U1, approve=True, now=NOW
        )
        self.assertEqual(result.outcome, DecideOutcome.DECIDED)
        return new

    # -- open_request -------------------------------------------------------

    async def test_a_request_is_stored_pending_with_its_history(self):
        new = new_approval()
        opened = await self.store.open_request(new, now=NOW)
        self.assertTrue(opened.created)
        record = opened.record
        self.assertEqual(
            (
                record.approval_id,
                record.task_id,
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
                TASK,
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
        a = await self.store.open_request(first, now=NOW)
        b = await self.store.open_request(again, now=NOW + timedelta(minutes=5))
        self.assertEqual((a.created, b.created), (True, False))
        self.assertEqual(b.record.approval_id, first.approval_id)
        self.assertEqual(
            await self.kinds(first.approval_id), [ApprovalEventKind.REQUESTED]
        )
        self.assertIsNone(await self.store.get(again.approval_id))

    async def test_an_approved_but_unused_request_is_also_open(self):
        first = await self.approved()
        again = new_approval(call_hash=first.call_hash)
        opened = await self.store.open_request(again, now=NOW)
        self.assertEqual(
            (opened.created, opened.record.status), (False, ApprovalStatus.APPROVED)
        )
        self.assertEqual(opened.record.approval_id, first.approval_id)

    async def test_a_new_request_can_follow_an_expired_one(self):
        first = new_approval()
        await self.store.open_request(first, now=NOW)
        later = NOW + HOUR
        second = new_approval(call_hash=first.call_hash, expires_at=later + HOUR)
        opened = await self.store.open_request(second, now=later)
        self.assertTrue(opened.created)
        self.assertEqual(opened.record.approval_id, second.approval_id)
        old = await self.store.get(first.approval_id)
        self.assertEqual(old.status, ApprovalStatus.EXPIRED)
        self.assertEqual(
            await self.kinds(first.approval_id),
            [ApprovalEventKind.REQUESTED, ApprovalEventKind.EXPIRED],
        )

    async def test_a_new_request_can_follow_a_consumed_or_rejected_one(self):
        used = await self.approved()
        await self.store.consume(used.approval_id, binding_of(used), now=NOW)
        again = new_approval(call_hash=used.call_hash)
        self.assertTrue((await self.store.open_request(again, now=NOW)).created)
        rejected = new_approval()
        await self.store.open_request(rejected, now=NOW)
        await self.store.decide(
            rejected.approval_id, approver_id=U1, approve=False, now=NOW
        )
        again = new_approval(call_hash=rejected.call_hash)
        self.assertTrue((await self.store.open_request(again, now=NOW)).created)

    async def test_different_calls_do_not_share_a_request(self):
        a = await self.store.open_request(new_approval(), now=NOW)
        b = await self.store.open_request(new_approval(), now=NOW)
        self.assertTrue(a.created and b.created)
        self.assertNotEqual(a.record.approval_id, b.record.approval_id)

    # -- decide -------------------------------------------------------------

    async def test_the_delegating_user_approves(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW)
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
        await self.store.open_request(new, now=NOW)
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
        await self.store.open_request(new, now=NOW)
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
        await self.store.open_request(new, now=NOW)
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
        await self.store.open_request(new, now=NOW)
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
        await self.store.open_request(other, now=NOW)
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

    # -- consume ------------------------------------------------------------

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
        await self.store.open_request(new, now=NOW)
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
        await self.store.open_request(early, now=NOW)
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
        await self.store.open_request(new, now=NOW)
        self.assertEqual(
            await self.store.consume(
                new.approval_id, binding_of(new), now=NOW + 2 * HOUR
            ),
            ConsumeOutcome.EXPIRED,
        )

    # -- concurrency ----------------------------------------------------------

    async def test_of_many_simultaneous_approvals_exactly_one_wins(self):
        new = new_approval()
        await self.store.open_request(new, now=NOW)
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
        await self.store.open_request(new, now=NOW)
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
            *(self.store.open_request(new, now=NOW) for new in requests)
        )
        self.assertEqual(sum(o.created for o in opened), 1)
        self.assertEqual(len({o.record.approval_id for o in opened}), 1)
        winner = opened[0].record.approval_id
        self.assertEqual(await self.kinds(winner), [ApprovalEventKind.REQUESTED])
