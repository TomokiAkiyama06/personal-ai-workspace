"""A change of Shared Memory leaves a completion record (PAW-046, Decision 0009 s.13).

The Authorizer's ``allow`` row is written BEFORE the change (fail-closed), so on
its own it cannot say whether the change happened: a delete of a missing memory,
a restore in the wrong state, a lock timeout or a failed update leaves the same
row as a delete that worked, and a delete or restore stores neither actor nor
time on the memory. These tests read the real ``audit_events`` table (as the
application role too, see ``test_shared_memory_grants.py``) and check that

* every operation that changed something appends ONE more row, in the very
  transaction of the change, naming the actor, the operation, the resource, the
  transition time (the service clock) and the correlation id of the attempt,
* every failure path (missing, wrong state, version conflict, lock timeout, a
  failing or skipped update or insert) leaves NO completion row and no change,
  only the attempt,
* a completion row that cannot be written takes the change back with it, and
* the attempt keeps its rules: refused and audit-failed attempts are unchanged
  and never completed.

``audit_events`` is append-only, so every test uses fresh users (their ids are
the filter). Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.authz import Authorizer, Capability, Principal, SystemRole
from paw_backend.memory.shared import (
    COMPLETED_REASON,
    CandidateNotFoundError,
    SharedMemoryBusyError,
    SharedMemoryChanges,
    SharedMemoryNotFoundError,
    SharedMemoryPermissionError,
    SharedMemoryStateError,
    SharedMemoryVersionConflictError,
    memory_lock_key,
)
from paw_backend.memory.shared.audit import completion_event

from .authz_support import FailingSink
from .shared_memory_support import T0, draft, requires_postgres
from .test_shared_memory_audit_actions import (
    APPROVE,
    CREATE,
    DELETE,
    EDIT,
    REJECT,
    RESTORE,
    AuditActionTestCase,
)

# Spelled out, not taken from the code.
COMPLETED = "completed"

_COLUMNS = (
    "action, decision, reason, correlation_id, occurred_at, recorded_at, actor_id,"
    " actor_role, agent_id, resource_kind, resource_id, project_id, repo_id,"
    " repo_acl, old_role, new_role, client_request_id"
)
_LOCK_ROWS = "SELECT id FROM shared_memory_candidates WHERE id = :id FOR UPDATE"


class CompletionTestCase(AuditActionTestCase):
    def trail(self, actor: Principal) -> list[dict[str, Any]]:
        return self.rows(
            f"SELECT {_COLUMNS} FROM audit_events WHERE actor_id = :actor"
            " ORDER BY recorded_at",
            actor=actor.user_id,
        )

    def attempts(self, actor: Principal) -> list[dict[str, Any]]:
        return [row for row in self.trail(actor) if row["reason"] != COMPLETED]

    def completions(self, actor: Principal) -> list[dict[str, Any]]:
        return [row for row in self.trail(actor) if row["reason"] == COMPLETED]

    def fail_on(
        self,
        table: str,
        event: str,
        *,
        skip: bool = False,
        when: str | None = None,
        at_commit: bool = False,
    ) -> None:
        """Make the database fail (or silently skip) every ``event`` on ``table``.

        ``event`` is ``INSERT``, ``UPDATE`` or both; ``skip`` makes the row
        trigger return NULL (the statement then changes nothing, no error), else
        it raises. ``at_commit`` raises only when the transaction commits (a
        deferred constraint trigger), after every statement has worked. Removed
        when the test ends.
        """
        name = f"paw_test_{uuid4().hex[:12]}"
        body = "RETURN NULL;" if skip else "RAISE EXCEPTION 'injected failure';"
        condition = f"WHEN ({when})" if when else ""
        timing = (
            f"CONSTRAINT TRIGGER {name} AFTER {event} ON {table}"
            " DEFERRABLE INITIALLY DEFERRED"
            if at_commit
            else f"TRIGGER {name} BEFORE {event} ON {table}"
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql"
                    f" AS $$ BEGIN {body} END $$"
                )
            )
            connection.execute(
                text(
                    f"CREATE {timing}"
                    f" FOR EACH ROW {condition} EXECUTE FUNCTION {name}()"
                )
            )

        def remove() -> None:
            with self.engine.begin() as connection:
                connection.execute(text(f"DROP TRIGGER {name} ON {table}"))
                connection.execute(text(f"DROP FUNCTION {name}()"))

        self.addCleanup(remove)

    def hold_candidate(self, candidate_id: UUID) -> None:
        """Hold the row lock of a candidate from a separate connection."""
        connection = self.engine.connect()
        self.addCleanup(connection.close)
        transaction = connection.begin()
        self.addCleanup(
            lambda: transaction.rollback() if transaction.is_active else None
        )
        connection.execute(text(_LOCK_ROWS), {"id": candidate_id})

    async def assert_attempted_but_not_completed(
        self, name: str, call: Any, error: type[BaseException], action: str
    ) -> None:
        """``call(actor)`` raises ``error``; only the attempt is on the trail."""
        actor = self.person(SystemRole.OWNER)
        before = self.snapshot()
        with self.assertRaises(error, msg=name):
            await call(actor)
        self.assertEqual(self.snapshot(), before, name)
        attempts = self.attempts(actor)
        self.assertEqual(
            [(row["action"], row["decision"]) for row in attempts],
            [(action, "allow")],
            name,
        )
        self.assertEqual(self.completions(actor), [], name)


@requires_postgres
class EverySuccessfulChangeIsCompletedTest(CompletionTestCase):
    async def test_a_completion_row_follows_the_attempt_and_names_actor_and_operation(
        self,
    ):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            ids = self.seed_all()
            for name, call, action, kind, resource_id in self.operations(ids):
                with self.subTest(role=role.value, operation=name):
                    actor = self.person(role)
                    self.clock.advance(minutes=7)
                    result = await call(actor)
                    trail = self.trail(actor)
                    self.assertEqual(len(trail), 2)
                    attempt, completion = trail
                    # A create names the memory it made; the others the one they
                    # changed (a candidate stays the resource of its decision).
                    created = resource_id or result.memory_id
                    self.assertEqual(
                        completion,
                        {
                            "action": action,
                            "decision": "allow",
                            "reason": COMPLETED,
                            "correlation_id": attempt["correlation_id"],
                            "occurred_at": self.clock.now,
                            "recorded_at": completion["recorded_at"],
                            "actor_id": actor.user_id,
                            "actor_role": role.value,
                            "agent_id": None,
                            "resource_kind": kind,
                            "resource_id": created,
                            "project_id": None,
                            "repo_id": None,
                            "repo_acl": None,
                            "old_role": None,
                            "new_role": None,
                            "client_request_id": None,
                        },
                    )
                    # The attempt is what it was: the decision, before the change.
                    self.assertEqual(
                        (
                            attempt["action"],
                            attempt["decision"],
                            attempt["reason"],
                            attempt["resource_id"],
                        ),
                        (action, "allow", "granted_by_system_role", resource_id),
                    )
                    self.assertLess(attempt["recorded_at"], completion["recorded_at"])

    async def test_the_history_of_one_memory_says_who_deleted_and_restored_it_and_when(
        self,
    ):
        # The case of the review finding: a status change stores no actor and no
        # time on the memory, so the trail is the only record of who did it.
        first = self.person(SystemRole.ADMIN)
        second = self.person(SystemRole.OWNER)
        memory_id = self.seed_memory(title="Rule")
        self.clock.advance(hours=1)
        deleted_at = self.clock.now
        await self.service.delete_memory(first, memory_id)
        self.clock.advance(hours=1)
        restored_at = self.clock.now
        await self.service.restore_memory(second, memory_id)
        rows = self.rows(
            "SELECT action, actor_id, occurred_at FROM audit_events"
            " WHERE resource_id = :m AND reason = :reason ORDER BY recorded_at",
            m=memory_id,
            reason=COMPLETED,
        )
        self.assertEqual(
            rows,
            [
                {
                    "action": DELETE,
                    "actor_id": first.user_id,
                    "occurred_at": deleted_at,
                },
                {
                    "action": RESTORE,
                    "actor_id": second.user_id,
                    "occurred_at": restored_at,
                },
            ],
        )

    async def test_every_operation_of_a_life_is_completed_in_order(self):
        actor = self.person(SystemRole.ADMIN)
        created = await self.service.create_memory(actor, draft())
        memory_id = created.memory_id
        await self.service.edit_memory(
            actor, memory_id, 1, SharedMemoryChanges(title="Edited")
        )
        await self.service.delete_memory(actor, memory_id)
        await self.service.restore_memory(actor, memory_id)
        self.assertEqual(
            [(row["action"], row["resource_id"]) for row in self.completions(actor)],
            [
                (CREATE, memory_id),
                (EDIT, memory_id),
                (DELETE, memory_id),
                (RESTORE, memory_id),
            ],
        )
        self.assertEqual(len(self.attempts(actor)), 4)

    async def test_an_edit_that_changes_nothing_is_attempted_but_not_completed(self):
        actor = self.person(SystemRole.OWNER)
        memory_id = self.seed_memory(title="Same")
        before = self.snapshot()
        result = await self.service.edit_memory(
            actor, memory_id, 1, SharedMemoryChanges(title="Same")
        )
        self.assertEqual(result.version_number, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([row["action"] for row in self.attempts(actor)], [EDIT])
        self.assertEqual(self.completions(actor), [])


@requires_postgres
class AFailedChangeIsNeverCompletedTest(CompletionTestCase):
    """Each way a change fails after the attempt was recorded (real PostgreSQL)."""

    async def test_a_missing_memory_or_candidate(self):
        missing = uuid4()
        private = self.seed_memory(scope="user")
        service = self.service
        cases = [
            (
                "edit",
                lambda who: service.edit_memory(
                    who, missing, 1, SharedMemoryChanges(title="X")
                ),
                SharedMemoryNotFoundError,
                EDIT,
            ),
            (
                "delete",
                lambda who: service.delete_memory(who, missing),
                SharedMemoryNotFoundError,
                DELETE,
            ),
            (
                "restore",
                lambda who: service.restore_memory(who, missing),
                SharedMemoryNotFoundError,
                RESTORE,
            ),
            (
                "delete of a private memory",
                lambda who: service.delete_memory(who, private),
                SharedMemoryNotFoundError,
                DELETE,
            ),
            (
                "approve",
                lambda who: service.approve_candidate(who, missing),
                CandidateNotFoundError,
                APPROVE,
            ),
            (
                "reject",
                lambda who: service.reject_candidate(who, missing),
                CandidateNotFoundError,
                REJECT,
            ),
        ]
        for name, call, error, action in cases:
            await self.assert_attempted_but_not_completed(name, call, error, action)

    async def test_the_wrong_state_or_a_stale_version(self):
        live = self.seed_memory(title="Live")
        gone = self.seed_memory(title="Gone", status="deprecated")
        decided = self.seed_candidate(state="approved")
        rejected = self.seed_candidate(state="rejected")
        service = self.service
        cases = [
            (
                "delete of a deleted memory",
                lambda who: service.delete_memory(who, gone),
                SharedMemoryStateError,
                DELETE,
            ),
            (
                "restore of a live memory",
                lambda who: service.restore_memory(who, live),
                SharedMemoryStateError,
                RESTORE,
            ),
            (
                "edit of a deleted memory",
                lambda who: service.edit_memory(
                    who, gone, 1, SharedMemoryChanges(title="X")
                ),
                SharedMemoryStateError,
                EDIT,
            ),
            (
                "edit of a stale version",
                lambda who: service.edit_memory(
                    who, live, 9, SharedMemoryChanges(title="X")
                ),
                SharedMemoryVersionConflictError,
                EDIT,
            ),
            (
                "approve of an approved candidate",
                lambda who: service.approve_candidate(who, decided),
                SharedMemoryStateError,
                APPROVE,
            ),
            (
                "approve of a rejected candidate",
                lambda who: service.approve_candidate(who, rejected),
                SharedMemoryStateError,
                APPROVE,
            ),
            (
                "reject of an approved candidate",
                lambda who: service.reject_candidate(who, decided),
                SharedMemoryStateError,
                REJECT,
            ),
        ]
        for name, call, error, action in cases:
            await self.assert_attempted_but_not_completed(name, call, error, action)

    async def test_a_lock_that_is_not_released_in_time(self):
        live = self.seed_memory(title="Live")
        gone = self.seed_memory(title="Gone", status="deprecated")
        candidate = self.seed_candidate()
        impatient = self.new_service(lock_timeout_ms=200)
        self.hold_advisory_lock(memory_lock_key(live))
        self.hold_advisory_lock(memory_lock_key(gone))
        self.hold_candidate(candidate)
        cases = [
            (
                "delete",
                lambda who: impatient.delete_memory(who, live),
                DELETE,
            ),
            (
                "edit",
                lambda who: impatient.edit_memory(
                    who, live, 1, SharedMemoryChanges(title="X")
                ),
                EDIT,
            ),
            (
                "restore",
                lambda who: impatient.restore_memory(who, gone),
                RESTORE,
            ),
            (
                "approve",
                lambda who: impatient.approve_candidate(who, candidate),
                APPROVE,
            ),
            (
                "reject",
                lambda who: impatient.reject_candidate(who, candidate),
                REJECT,
            ),
        ]
        for name, call, action in cases:
            await self.assert_attempted_but_not_completed(
                name, call, SharedMemoryBusyError, action
            )

    async def test_an_update_that_raises(self):
        live = self.seed_memory(title="Live")
        gone = self.seed_memory(title="Gone", status="deprecated")
        candidate = self.seed_candidate()
        self.fail_on("memory_versions", "UPDATE")
        self.fail_on("shared_memory_candidates", "UPDATE")
        service = self.service
        cases = [
            (
                "delete",
                lambda who: service.delete_memory(who, live),
                DELETE,
            ),
            (
                "restore",
                lambda who: service.restore_memory(who, gone),
                RESTORE,
            ),
            (
                "edit",
                lambda who: service.edit_memory(
                    who, live, 1, SharedMemoryChanges(title="X")
                ),
                EDIT,
            ),
            (
                "approve",
                lambda who: service.approve_candidate(who, candidate),
                APPROVE,
            ),
            (
                "reject",
                lambda who: service.reject_candidate(who, candidate),
                REJECT,
            ),
        ]
        for name, call, action in cases:
            await self.assert_attempted_but_not_completed(
                name, call, DBAPIError, action
            )

    async def test_an_update_that_changes_no_row(self):
        live = self.seed_memory(title="Live")
        gone = self.seed_memory(title="Gone", status="deprecated")
        candidate = self.seed_candidate()
        self.fail_on("memory_versions", "UPDATE", skip=True)
        self.fail_on("shared_memory_candidates", "UPDATE", skip=True)
        service = self.service
        cases = [
            (
                "delete",
                lambda who: service.delete_memory(who, live),
                DELETE,
            ),
            (
                "restore",
                lambda who: service.restore_memory(who, gone),
                RESTORE,
            ),
            (
                "edit",
                lambda who: service.edit_memory(
                    who, live, 1, SharedMemoryChanges(title="X")
                ),
                EDIT,
            ),
            (
                "approve",
                lambda who: service.approve_candidate(who, candidate),
                APPROVE,
            ),
            (
                "reject",
                lambda who: service.reject_candidate(who, candidate),
                REJECT,
            ),
        ]
        for name, call, action in cases:
            await self.assert_attempted_but_not_completed(
                name, call, SharedMemoryBusyError, action
            )

    async def test_an_insert_that_raises_after_other_rows_were_written(self):
        # The status update, the memory row or the version row were already
        # written when the next statement fails: the whole change is taken back.
        live = self.seed_memory(title="Live")
        candidate = self.seed_candidate()
        self.fail_on("memory_versions", "INSERT")
        service = self.service
        cases = [
            (
                "create",
                lambda who: service.create_memory(who, draft()),
                CREATE,
            ),
            (
                "edit",
                lambda who: service.edit_memory(
                    who, live, 1, SharedMemoryChanges(title="X")
                ),
                EDIT,
            ),
            (
                "approve",
                lambda who: service.approve_candidate(who, candidate),
                APPROVE,
            ),
        ]
        for name, call, action in cases:
            await self.assert_attempted_but_not_completed(
                name, call, DBAPIError, action
            )

    async def test_the_last_write_of_an_edit_fails(self):
        live = self.seed_memory(title="Live")
        self.fail_on("memory_relations", "INSERT")
        await self.assert_attempted_but_not_completed(
            "edit",
            lambda who: self.service.edit_memory(
                who, live, 1, SharedMemoryChanges(title="X")
            ),
            DBAPIError,
            EDIT,
        )


@requires_postgres
class ACompletionThatCannotBeWrittenTakesTheChangeBackTest(CompletionTestCase):
    """The completion row is part of the change: no row, no change (fail-closed)."""

    async def test_each_operation_is_rolled_back_when_its_completion_fails(self):
        live = self.seed_memory(title="Live")
        gone = self.seed_memory(title="Gone", status="deprecated")
        approve = self.seed_candidate()
        reject = self.seed_candidate()
        self.fail_on("audit_events", "INSERT", when=f"NEW.reason = '{COMPLETED}'")
        service = self.service
        cases = [
            ("create", lambda who: service.create_memory(who, draft()), CREATE),
            (
                "edit",
                lambda who: service.edit_memory(
                    who, live, 1, SharedMemoryChanges(title="X")
                ),
                EDIT,
            ),
            ("delete", lambda who: service.delete_memory(who, live), DELETE),
            ("restore", lambda who: service.restore_memory(who, gone), RESTORE),
            (
                "approve",
                lambda who: service.approve_candidate(who, approve),
                APPROVE,
            ),
            (
                "reject",
                lambda who: service.reject_candidate(who, reject),
                REJECT,
            ),
        ]
        for name, call, action in cases:
            await self.assert_attempted_but_not_completed(
                name, call, DBAPIError, action
            )

    async def test_each_operation_is_rolled_back_when_the_commit_fails(self):
        # Every statement, the completion row included, has worked when the commit
        # fails: the completion row goes with the change, not before it.
        live = self.seed_memory(title="Live")
        gone = self.seed_memory(title="Gone", status="deprecated")
        approve = self.seed_candidate()
        reject = self.seed_candidate()
        self.fail_on("memory_versions", "INSERT OR UPDATE", at_commit=True)
        self.fail_on("shared_memory_candidates", "UPDATE", at_commit=True)
        service = self.service
        cases = [
            ("create", lambda who: service.create_memory(who, draft()), CREATE),
            (
                "edit",
                lambda who: service.edit_memory(
                    who, live, 1, SharedMemoryChanges(title="X")
                ),
                EDIT,
            ),
            ("delete", lambda who: service.delete_memory(who, live), DELETE),
            ("restore", lambda who: service.restore_memory(who, gone), RESTORE),
            (
                "approve",
                lambda who: service.approve_candidate(who, approve),
                APPROVE,
            ),
            (
                "reject",
                lambda who: service.reject_candidate(who, reject),
                REJECT,
            ),
        ]
        for name, call, action in cases:
            await self.assert_attempted_but_not_completed(
                name, call, DBAPIError, action
            )


@requires_postgres
class TheAttemptKeepsItsRulesTest(CompletionTestCase):
    async def test_a_refused_attempt_is_a_denial_and_is_never_completed(self):
        ids = self.seed_all()
        before = self.snapshot()
        for name, call, action, _, _ in self.operations(ids):
            with self.subTest(operation=name):
                user = self.person(SystemRole.USER)
                with self.assertRaises(SharedMemoryPermissionError):
                    await call(user)
                self.assertEqual(
                    [(row["action"], row["decision"]) for row in self.trail(user)],
                    [(action, "deny")],
                )
        self.assertEqual(self.snapshot(), before)

    async def test_an_attempt_that_cannot_be_audited_is_refused_and_never_completed(
        self,
    ):
        ids = self.seed_all()
        # ``operations`` closes over ``self.service``.
        self.service = self.new_service(
            authorizer=Authorizer(FailingSink(), directory=self.directory)
        )
        before = self.snapshot()
        actor = self.person(SystemRole.OWNER)
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            for name, call, _, _, _ in self.operations(ids):
                with self.subTest(operation=name):
                    with self.assertRaises(SharedMemoryPermissionError) as caught:
                        await call(actor)
                    self.assertEqual(caught.exception.reason, "audit_unavailable")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.trail(actor), [])


class CompletionEventTest(unittest.TestCase):
    """The row is built from ids, enums and one fixed reason: nothing else."""

    def test_the_event_of_each_capability(self):
        actor = Principal(uuid4(), SystemRole.ADMIN)
        resource_id, correlation_id = uuid4(), uuid4()
        for capability in (
            Capability.SHARED_MEMORY_CREATE,
            Capability.SHARED_MEMORY_EDIT,
            Capability.SHARED_MEMORY_DELETE,
            Capability.SHARED_MEMORY_RESTORE,
            Capability.SHARED_MEMORY_CANDIDATE_APPROVE,
            Capability.SHARED_MEMORY_CANDIDATE_REJECT,
        ):
            with self.subTest(capability=capability.value):
                event = completion_event(
                    actor,
                    capability,
                    "shared_memory",
                    resource_id,
                    correlation_id,
                    T0 + timedelta(seconds=5),
                )
                self.assertEqual(
                    event.model_dump(exclude={"event_id"}),
                    {
                        "correlation_id": correlation_id,
                        "occurred_at": T0 + timedelta(seconds=5),
                        "actor_id": actor.user_id,
                        "actor_role": "admin",
                        "agent_id": None,
                        "action": capability.value,
                        "resource_kind": "shared_memory",
                        "resource_id": resource_id,
                        "project_id": None,
                        "repo_id": None,
                        "repo_acl": None,
                        "decision": "allow",
                        "reason": "completed",
                        "old_role": None,
                        "new_role": None,
                        "client_request_id": None,
                    },
                )

    def test_two_events_never_share_an_id(self):
        actor = Principal(uuid4(), SystemRole.OWNER)
        args = (
            actor,
            Capability.SHARED_MEMORY_DELETE,
            "shared_memory",
            uuid4(),
            uuid4(),
            T0,
        )
        self.assertNotEqual(
            completion_event(*args).event_id, completion_event(*args).event_id
        )

    def test_the_reason_is_the_documented_constant(self):
        self.assertEqual(COMPLETED_REASON, COMPLETED)


if __name__ == "__main__":
    unittest.main()
