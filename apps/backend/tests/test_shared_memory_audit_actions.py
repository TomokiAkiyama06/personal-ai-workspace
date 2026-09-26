"""Each Shared Memory management operation has its own audit action (PAW-046).

The Authorizer stores the capability value as the ``action`` of its audit row,
and a status change (delete, restore) records neither actor nor time on the
memory. If every operation asked for one shared capability, the audit history
could not tell a deletion from a restoration, an edit or an approval. These
tests read the rows from the real ``audit_events`` table (written by
``PostgresAuditSink``, as the application role too, see
``test_shared_memory_grants.py``) and check that

* each operation is recorded under its own action and names the acting user,
  the role and the resource,
* a refused attempt names the operation that was tried,
* the audit row exists before the change is visible, and
* the administration views (deleted memories, candidates) stay ``manage``.

``audit_events`` is append-only, so every test uses fresh users (their ids are
the filter) and never expects an empty table. Skipped unless
``PAW_TEST_DATABASE_URL`` is set.
"""

from typing import Any
from uuid import uuid4

from paw_backend.authz import Authorizer, PostgresAuditSink, Principal, SystemRole
from paw_backend.memory.shared import (
    SharedMemoryChanges,
    SharedMemoryPermissionError,
)

from .authz_support import FailingSink
from .shared_memory_support import (
    AsyncPostgresSharedTestCase,
    draft,
    requires_postgres,
)

# The expected actions are spelled out literally, not taken from the code.
CREATE = "shared_memory.create"
EDIT = "shared_memory.edit"
DELETE = "shared_memory.delete"
RESTORE = "shared_memory.restore"
APPROVE = "shared_memory.candidate.approve"
REJECT = "shared_memory.candidate.reject"
MANAGE = "shared_memory.manage"

_COLUMNS = (
    "action, decision, reason, actor_id, actor_role, agent_id,"
    " resource_kind, resource_id"
)


class ProbeSink:
    """Writes to the real sink and remembers what the database looked like then."""

    def __init__(self, inner: Any, observe: Any) -> None:
        self._inner = inner
        self._observe = observe
        self.seen: list[tuple[str, Any]] = []

    async def record(self, event: Any) -> None:
        self.seen.append((event.action, self._observe()))
        await self._inner.record(event)


class AuditActionTestCase(AsyncPostgresSharedTestCase):
    """A service whose Authorizer writes to the real ``audit_events`` table."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        database = self.new_database()
        self.addAsyncCleanup(database.dispose)
        self.sink = PostgresAuditSink(database)
        self.authorizer = Authorizer(self.sink, directory=self.directory)
        self.service = self.new_service(authorizer=self.authorizer)

    @staticmethod
    def person(role: SystemRole) -> Principal:
        """A user nobody else in the test run uses: the filter for their audit rows."""
        return Principal(uuid4(), role)

    def audit_of(self, actor: Principal) -> list[dict[str, Any]]:
        """The Authorizer's rows (the attempts) of ``actor``, not the completions.

        A change also appends a completion row (reason ``completed``); those are
        checked in ``test_shared_memory_completion.py``.
        """
        return self.rows(
            f"SELECT {_COLUMNS} FROM audit_events WHERE actor_id = :actor"
            " AND reason <> 'completed' ORDER BY recorded_at, occurred_at",
            actor=actor.user_id,
        )

    def seed_all(self) -> dict[str, Any]:
        return {
            "live": self.seed_memory(title="Live"),
            "gone": self.seed_memory(title="Gone", status="deprecated"),
            "to_approve": self.seed_candidate(),
            "to_reject": self.seed_candidate(),
        }

    def operations(self, ids: dict[str, Any]) -> list[tuple[Any, ...]]:
        """(name, call(actor), action, resource kind, resource id) of the six."""
        service = self.service
        return [
            (
                "create_memory",
                lambda who: service.create_memory(who, draft()),
                CREATE,
                "shared_memory",
                None,
            ),
            (
                "edit_memory",
                lambda who: service.edit_memory(
                    who, ids["live"], 1, SharedMemoryChanges(title="Edited")
                ),
                EDIT,
                "shared_memory",
                ids["live"],
            ),
            (
                "delete_memory",
                lambda who: service.delete_memory(who, ids["live"]),
                DELETE,
                "shared_memory",
                ids["live"],
            ),
            (
                "restore_memory",
                lambda who: service.restore_memory(who, ids["gone"]),
                RESTORE,
                "shared_memory",
                ids["gone"],
            ),
            (
                "approve_candidate",
                lambda who: service.approve_candidate(who, ids["to_approve"]),
                APPROVE,
                "shared_memory_candidate",
                ids["to_approve"],
            ),
            (
                "reject_candidate",
                lambda who: service.reject_candidate(who, ids["to_reject"]),
                REJECT,
                "shared_memory_candidate",
                ids["to_reject"],
            ),
        ]


@requires_postgres
class EachOperationHasItsOwnActionTest(AuditActionTestCase):
    async def test_each_operation_is_recorded_under_its_own_action(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            ids = self.seed_all()
            for name, call, action, kind, resource_id in self.operations(ids):
                with self.subTest(role=role.value, operation=name):
                    actor = self.person(role)
                    await call(actor)
                    self.assertEqual(
                        self.audit_of(actor),
                        [
                            {
                                "action": action,
                                "decision": "allow",
                                "reason": "granted_by_system_role",
                                "actor_id": actor.user_id,
                                "actor_role": role.value,
                                "agent_id": None,
                                "resource_kind": kind,
                                "resource_id": resource_id,
                            }
                        ],
                    )

    async def test_the_six_operations_never_share_an_action(self):
        actions = [action for _, _, action, _, _ in self.operations(self.seed_all())]
        self.assertEqual(len(set(actions)), 6)
        self.assertNotIn(MANAGE, actions)

    async def test_the_history_of_one_memory_tells_delete_from_restore(self):
        # The case of the review finding: the memory row carries neither actor nor
        # time of a status change. The audit history tells the operations apart by
        # ``action`` (the version's own history, ``memory_metadata_changes``, is in
        # ``test_shared_memory_status_history.py``; Decision 0026).
        admin = self.person(SystemRole.ADMIN)
        memory_id = self.seed_memory(title="Rule")
        await self.service.edit_memory(
            admin, memory_id, 1, SharedMemoryChanges(title="Rule 2")
        )
        await self.service.delete_memory(admin, memory_id)
        await self.service.restore_memory(admin, memory_id)
        await self.service.delete_memory(admin, memory_id)
        rows = self.audit_of(admin)
        self.assertEqual(
            [row["action"] for row in rows], [EDIT, DELETE, RESTORE, DELETE]
        )
        self.assertEqual({row["actor_id"] for row in rows}, {admin.user_id})
        self.assertEqual({row["resource_id"] for row in rows}, {memory_id})
        self.assertEqual({row["decision"] for row in rows}, {"allow"})


@requires_postgres
class ARefusalNamesTheOperationTest(AuditActionTestCase):
    async def test_a_user_who_tries_each_operation_leaves_one_named_denial(self):
        ids = self.seed_all()
        before = self.snapshot()
        for name, call, action, kind, resource_id in self.operations(ids):
            with self.subTest(operation=name):
                user = self.person(SystemRole.USER)
                with self.assertRaises(SharedMemoryPermissionError) as caught:
                    await call(user)
                self.assertEqual(caught.exception.reason, "capability_not_granted")
                self.assertEqual(
                    self.audit_of(user),
                    [
                        {
                            "action": action,
                            "decision": "deny",
                            "reason": "capability_not_granted",
                            "actor_id": user.user_id,
                            "actor_role": "user",
                            "agent_id": None,
                            "resource_kind": kind,
                            "resource_id": resource_id,
                        }
                    ],
                )
        self.assertEqual(self.snapshot(), before)


@requires_postgres
class TheAdministrationViewsStayManageTest(AuditActionTestCase):
    """Reads of what only managers see change nothing, so they share ``manage``.

    The resource tells them apart: a candidate or a memory, with or without an id.
    """

    async def test_the_views_are_recorded_as_manage_with_their_resource(self):
        candidate_id = self.seed_candidate()
        memory_id = self.seed_memory(status="deprecated")
        admin = self.person(SystemRole.ADMIN)
        await self.service.list_candidates(admin)
        await self.service.get_candidate(admin, candidate_id)
        await self.service.list_memories(admin, include_deleted=True)
        await self.service.get_memory(admin, memory_id, include_deleted=True)
        self.assertEqual(
            [
                (row["action"], row["resource_kind"], row["resource_id"])
                for row in self.audit_of(admin)
            ],
            [
                (MANAGE, "shared_memory_candidate", None),
                (MANAGE, "shared_memory_candidate", candidate_id),
                (MANAGE, "shared_memory", None),
                (MANAGE, "shared_memory", memory_id),
            ],
        )


@requires_postgres
class TheAuditRowComesBeforeTheChangeTest(AuditActionTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.probe = ProbeSink(self.sink, self.snapshot)
        self.service = self.new_service(
            authorizer=Authorizer(self.probe, directory=self.directory)
        )

    async def test_nothing_is_visible_yet_when_the_audit_row_is_written(self):
        ids = self.seed_all()
        for name, call, action, _, _ in self.operations(ids):
            with self.subTest(operation=name):
                actor = self.person(SystemRole.OWNER)
                before = self.snapshot()
                self.probe.seen.clear()
                await call(actor)
                self.assertEqual([seen for seen, _ in self.probe.seen], [action])
                # ... and at that moment the database was still what it had been.
                self.assertEqual(self.probe.seen[0][1], before)
                self.assertNotEqual(self.snapshot(), before)


@requires_postgres
class AnAuditFailureBlocksTheOperationTest(AuditActionTestCase):
    async def test_a_rejection_needs_its_audit_row(self):
        candidate_id = self.seed_candidate()
        service = self.new_service(
            authorizer=Authorizer(FailingSink(), directory=self.directory)
        )
        before = self.snapshot()
        with self.assertLogs("paw_backend.authz.authorizer", level="ERROR"):
            with self.assertRaises(SharedMemoryPermissionError) as caught:
                await service.reject_candidate(self.admin, candidate_id)
        self.assertEqual(caught.exception.reason, "audit_unavailable")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.candidate_row(candidate_id)["state"], "pending")
