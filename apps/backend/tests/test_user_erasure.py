"""The erasure of a deleted user's personal data, 30 days on (Issue #127, PostgreSQL).

``UserErasureService`` runs as the table owner (here, the test database's own user),
the way ``python -m paw_backend.cli user-erasure-run`` runs it from the systemd
timer. The user is deleted through the real ``UserLifecycleService`` (at the test
clock ``T0``); the erasure's clock is then moved past the 30 days.
"""

import unittest
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.auth.audit import AuthReason
from paw_backend.auth.errors import RetentionExpiredError
from paw_backend.auth.onboarding.erasure import (
    PERSONAL_TABLES,
    CredentialsOutcome,
    ErasureAlreadyRunningError,
    ErasureOutcome,
    UserErasureService,
)
from paw_backend.tasks import Actor, TaskCommand, TaskService
from paw_backend.tasks.queueing import TaskQueue

from .auth_support import T0, requires_postgres
from .gate_support import ALWAYS_ACTIVE
from .onboarding_support import OnboardingTestCase

DUE = T0 + timedelta(hours=30 * 24, minutes=1)
NOT_YET = T0 + timedelta(hours=30 * 24, minutes=-1)


@requires_postgres
class ErasureTestCase(OnboardingTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.execute(
            "TRUNCATE conversations, memories, tasks, repositories, projects, "
            "shared_memory_candidates CASCADE"
        )
        _, self.admin, self.admin_auth = await self.administrator()
        self.tasks = TaskService(self.database, project_gate=ALWAYS_ACTIVE)
        self.queue = TaskQueue(self.database, project_gate=ALWAYS_ACTIVE)

    def erasure(self, at=DUE) -> UserErasureService:
        return UserErasureService(self.database, clock=lambda: at)

    async def delete(self, user) -> None:
        await self.lifecycle.delete_user(
            self.admin, user.id, self.context(), session_id=self.admin_auth.record.id
        )

    # -- seeding ---------------------------------------------------------------------

    async def project(self, name: str = "Alpha") -> uuid.UUID:
        project_id = uuid.uuid4()
        await self.execute(
            "INSERT INTO projects (id, name, status, created_at, updated_at) "
            "VALUES (:id, :name, 'active', :now, :now)",
            id=project_id,
            name=name,
            now=T0,
        )
        return project_id

    async def member(self, project_id, user_id, role: str = "contributor") -> None:
        await self.execute(
            "INSERT INTO project_members (project_id, user_id, role, status, "
            "invited_at, joined_at) VALUES (:p, :u, :role, 'active', :now, :now)",
            p=project_id,
            u=user_id,
            role=role,
            now=T0,
        )

    async def conversation(self, user_id) -> tuple[uuid.UUID, uuid.UUID]:
        conversation_id, message_id = uuid.uuid4(), uuid.uuid4()
        await self.execute(
            "INSERT INTO conversations (id, owner_user_id, title) "
            "VALUES (:id, :owner, 'private talk')",
            id=conversation_id,
            owner=user_id,
        )
        await self.execute(
            "INSERT INTO messages (id, conversation_id, turn_id, event_sequence, "
            "role, content) VALUES (:id, :c, :turn, 0, 'user', 'my secret plan')",
            id=message_id,
            c=conversation_id,
            turn=uuid.uuid4(),
        )
        await self.execute(
            "INSERT INTO session_states (conversation_id, summary) "
            "VALUES (:c, 'summary of my secret plan')",
            c=conversation_id,
        )
        await self.execute(
            "INSERT INTO memory_journal_entries (id, conversation_id, message_id, "
            "turn_id, event_sequence, owner_user_id) VALUES (gen_random_uuid(), :c, "
            ":m, gen_random_uuid(), 0, :owner)",
            c=conversation_id,
            m=message_id,
            owner=user_id,
        )
        return conversation_id, message_id

    async def memory_version(
        self, memory_id, number: int, *, owner=None, project=None, status="active"
    ) -> uuid.UUID:
        version_id = uuid.uuid4()
        await self.execute(
            "INSERT INTO memory_versions (id, memory_id, version_number, scope, "
            "owner_user_id, project_id, memory_type, title, content, status, "
            "confirmation_state, freshness_policy, actor_type) VALUES (:id, :m, :n, "
            ":scope, :owner, :project, 'preference', 'title', 'content', :status, "
            "'confirmed', 'permanent', 'system')",
            id=version_id,
            m=memory_id,
            n=number,
            scope="user" if owner else "project",
            owner=owner,
            project=project,
            status=status,
        )
        return version_id

    async def memory(self, **options) -> tuple[uuid.UUID, uuid.UUID]:
        memory_id = uuid.uuid4()
        await self.execute("INSERT INTO memories (id) VALUES (:id)", id=memory_id)
        return memory_id, await self.memory_version(memory_id, 1, **options)

    async def personal_data(self, user) -> dict:
        """Everything personal the erasure must remove, for ``user``."""
        await self.sign_in(user, step_up=False)
        await self.execute(
            "INSERT INTO user_passkeys (id, user_id, credential_id, public_key, "
            "sign_count, name, backup_eligible, backed_up, created_at) VALUES "
            "(gen_random_uuid(), :u, :cred, :key, 0, 'laptop', false, false, :now)",
            u=user.id,
            cred=uuid.uuid4().bytes,
            key=b"k" * 32,
            now=T0,
        )
        await self.execute(
            "INSERT INTO connection_quotas (user_id, kind, metric, period, "
            "limit_value, created_at, updated_at) VALUES (:u, 'codex', 'requests', "
            "'day', 10, :now, :now)",
            u=user.id,
            now=T0,
        )
        conversation_id, message_id = await self.conversation(user.id)
        private_memory, _ = await self.memory(owner=user.id)
        return {
            "conversation": conversation_id,
            "message": message_id,
            "private_memory": private_memory,
        }

    async def candidate(self, user_id, state: str, *, origin: str = "user"):
        """A Shared Memory candidate ``user_id`` proposed from a memory (its text
        is a copy of that memory's)."""
        candidate_id = uuid.uuid4()
        decided = state != "pending"
        await self.execute(
            "INSERT INTO shared_memory_candidates (id, state, proposer_user_id, "
            "origin_scope, memory_type, title, content, decided_by, decided_at) "
            "VALUES (:id, :state, :u, :origin, 'preference', 'private title', "
            "'my private memory text', :by, :at)",
            id=candidate_id,
            state=state,
            u=user_id,
            origin=origin,
            by=self.admin.user_id if decided else None,
            at=T0 if decided else None,
        )
        return candidate_id

    async def candidates_of(self, user_id) -> set[tuple[uuid.UUID, str]]:
        return {
            (row.id, row.state)
            for row in await self.query(
                "SELECT id, state FROM shared_memory_candidates "
                "WHERE proposer_user_id = :u",
                u=user_id,
            )
        }

    async def count(self, sql: str, **params) -> int:
        return await self.scalar(sql, **params)

    async def rows_left(self, user_id) -> dict:
        left = {
            table: await self.count(
                f"SELECT count(*) FROM {table} WHERE {column} = :id", id=user_id
            )
            for table, column in PERSONAL_TABLES
        }
        left["memory_versions(user)"] = await self.count(
            "SELECT count(*) FROM memory_versions WHERE owner_user_id = :id",
            id=user_id,
        )
        return {table: n for table, n in left.items() if n}

    async def erase_audit(self) -> list[tuple]:
        return [
            (row.decision, row.reason, row.resource_id)
            for row in await self.audit_rows()
            if row.action == "auth.user.erase"
        ]


class ErasureTest(ErasureTestCase):
    async def test_a_user_deleted_30_days_ago_is_erased_and_marked_deleted(self):
        bob = await self.make_user("bob")
        seeded = await self.personal_data(bob)
        project_id = await self.project()
        await self.member(project_id, bob.id)
        await self.delete(bob)
        self.assertNotEqual(await self.rows_left(bob.id), {})
        # The database erasure commits first; the operator's confirmation of the
        # copies outside the database counts on a later run.
        first = await self.erasure().run()
        self.assertEqual(
            [r.outcome for r in first.results], [ErasureOutcome.COPIES_PENDING]
        )

        report = await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])

        self.assertTrue(report.ok)
        self.assertEqual(
            [(r.user_id, r.outcome) for r in report.results],
            [(bob.id, ErasureOutcome.ERASED)],
        )
        self.assertEqual(await self.rows_left(bob.id), {})
        for table, key in (
            ("conversations", "conversation"),
            ("messages", "message"),
            ("memories", "private_memory"),
        ):
            self.assertEqual(
                await self.count(
                    f"SELECT count(*) FROM {table} WHERE id = :id", id=seeded[key]
                ),
                0,
                table,
            )
        self.assertEqual(
            await self.count(
                "SELECT count(*) FROM session_states WHERE conversation_id = :id",
                id=seeded["conversation"],
            ),
            0,
        )
        # The tombstone: the row, its login name and the whole history stay.
        self.assertEqual(await self.status_of(bob.id), "deleted")
        self.assertEqual(
            await self.history_of(bob.id),
            [("active", "pending_deletion"), ("pending_deletion", "deleted")],
        )
        changed_by = await self.scalar(
            "SELECT changed_by FROM user_status_changes WHERE user_id = :id "
            "AND new_status = 'deleted'",
            id=bob.id,
        )
        self.assertIsNone(changed_by)
        self.assertEqual(
            await self.scalar("SELECT login_name FROM users WHERE id = :id", id=bob.id),
            "bob",
        )
        self.assertEqual(
            await self.erase_audit(),
            [
                ("allow", "data_erased", bob.id),
                ("deny", "copies_pending", bob.id),
                ("allow", "copies_confirmed", bob.id),
                ("allow", "erased", bob.id),
            ],
        )
        for row in await self.audit_rows():
            if row.action == "auth.user.erase":
                self.assertIsNone(row.actor_id)

    async def test_everyone_else_s_data_is_left_alone(self):
        bob = await self.make_user("bob")
        carol = await self.make_user("carol")
        await self.personal_data(bob)
        carol_data = await self.personal_data(carol)
        project_id = await self.project()
        await self.member(project_id, carol.id, "manager")
        # A project memory that cites bob's conversation, and a memory bob kept
        # private first and widened to the project later.
        conversation_id, message_id = await self.conversation(bob.id)
        _, project_version = await self.memory(project=project_id)
        await self.execute(
            "INSERT INTO memory_sources (id, memory_version_id, source_type, "
            "conversation_id, message_id) VALUES (gen_random_uuid(), :v, "
            "'conversation', :c, :m)",
            v=project_version,
            c=conversation_id,
            m=message_id,
        )
        widened, _ = await self.memory(owner=bob.id, status="superseded")
        wider = await self.memory_version(widened, 2, project=project_id)
        task = await self.tasks.create_task(
            project_id=project_id, created_by=bob.id, title="Old work"
        )
        await self.tasks.execute(task.task_id, TaskCommand.CANCEL, actor=Actor.system())
        await self.delete(bob)
        await self.erasure().run()

        report = await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])

        self.assertTrue(report.ok)
        self.assertEqual(await self.rows_left(bob.id), {})
        # carol keeps everything, and stays active.
        self.assertEqual(await self.status_of(carol.id), "active")
        self.assertIn("password_credentials", await self.rows_left(carol.id))
        self.assertEqual(
            await self.count(
                "SELECT count(*) FROM conversations WHERE id = :id",
                id=carol_data["conversation"],
            ),
            1,
        )
        # The project memory keeps its source, marked deleted, without the link.
        source = (
            await self.query(
                "SELECT conversation_id, message_id, source_deleted_at "
                "FROM memory_sources WHERE memory_version_id = :v",
                v=project_version,
            )
        )[0]
        self.assertIsNone(source.conversation_id)
        self.assertIsNone(source.message_id)
        self.assertIsNotNone(source.source_deleted_at)
        # The widened memory keeps its project version only.
        versions = await self.query(
            "SELECT id, scope FROM memory_versions WHERE memory_id = :m", m=widened
        )
        self.assertEqual([(v.id, v.scope) for v in versions], [(wider, "project")])
        # What belongs to the project stays: the task, carol's membership.
        self.assertEqual(
            await self.count(
                "SELECT count(*) FROM tasks WHERE id = :id", id=task.task_id
            ),
            1,
        )
        self.assertEqual(
            await self.count(
                "SELECT count(*) FROM project_members WHERE project_id = :p",
                p=project_id,
            ),
            1,
        )

    async def test_undecided_and_rejected_shared_memory_candidates_are_erased(self):
        # A candidate carries a copy of the private memory it was proposed from.
        # A pending one could still be approved into a Shared Memory after the
        # erasure; a rejected one would keep the private text for good. An approved
        # one is already Shared Memory by decision, and stays as that record.
        bob = await self.make_user("bob")
        carol = await self.make_user("carol")
        await self.personal_data(bob)
        pending = await self.candidate(bob.id, "pending")
        from_project = await self.candidate(bob.id, "pending", origin="project")
        rejected = await self.candidate(bob.id, "rejected")
        approved = await self.candidate(bob.id, "approved")
        carols = await self.candidate(carol.id, "pending")
        await self.delete(bob)
        self.assertIn((pending, "pending"), await self.candidates_of(bob.id))
        self.assertIn((from_project, "pending"), await self.candidates_of(bob.id))
        self.assertIn((rejected, "rejected"), await self.candidates_of(bob.id))
        await self.erasure().run()

        report = await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])

        self.assertTrue(report.ok)
        self.assertEqual(await self.status_of(bob.id), "deleted")
        self.assertEqual(await self.candidates_of(bob.id), {(approved, "approved")})
        self.assertEqual(await self.candidates_of(carol.id), {(carols, "pending")})

    async def test_a_candidate_left_behind_fails_the_verification(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.candidate(bob.id, "pending")
        await self.delete(bob)
        service = self.erasure()
        original = service._delete_personal_data

        async def forgets_the_candidates(session, user_id, now):
            await original(session, user_id, now)
            await session.execute(
                text(
                    "INSERT INTO shared_memory_candidates (state, proposer_user_id, "
                    "origin_scope, memory_type, title, content) VALUES ('pending', "
                    ":u, 'user', 'preference', 'late', 'late private text')"
                ),
                {"u": user_id},
            )

        service._delete_personal_data = forgets_the_candidates

        result = await service.erase_user(bob.id)

        self.assertIs(result.outcome, ErasureOutcome.VERIFICATION_FAILED)
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertEqual(len(await self.candidates_of(bob.id)), 1)

    async def test_nothing_happens_before_the_30_days_are_over(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)

        report = await self.erasure(NOT_YET).run()

        self.assertTrue(report.ok)
        self.assertEqual(report.results, ())
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertIn("password_credentials", await self.rows_left(bob.id))
        self.assertEqual(await self.erase_audit(), [])

    async def test_erasable_exactly_when_the_owner_can_no_longer_restore(self):
        bob = await self.make_user("bob")
        await self.delete(bob)
        self.clock.now = DUE
        _, owner_principal, owner_auth = await self.administrator("owner", "the-owner")

        with self.assertRaises(RetentionExpiredError):
            await self.lifecycle.restore_user(
                owner_principal, bob.id, self.context(), session_id=owner_auth.record.id
            )
        self.assertEqual(await self.erasure().due_user_ids(), (bob.id,))
        self.assertEqual(await self.erasure(NOT_YET).due_user_ids(), ())

    async def test_active_owner_invited_and_restored_users_are_never_erased(self):
        alice = await self.make_user("alice")
        await self.make_user("the-owner", role="owner")
        await self.make_user("ivy", status="invited", password=None)
        dave = await self.make_user("dave")
        await self.delete(dave)
        # dave was restored (the latest pending_deletion change is what counts).
        await self.execute(
            "UPDATE users SET status = 'active' WHERE id = :id", id=dave.id
        )

        report = await self.erasure(T0 + timedelta(days=400)).run()

        self.assertEqual(report.results, ())
        self.assertEqual(await self.status_of(alice.id), "active")
        self.assertEqual(await self.status_of(dave.id), "active")

    async def test_a_second_run_changes_nothing(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)
        await self.erasure().run()
        await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])

        report = await self.erasure(DUE + timedelta(days=2)).run(copies_erased=[bob.id])

        self.assertTrue(report.ok)
        self.assertEqual(report.results, ())
        self.assertEqual(await self.status_of(bob.id), "deleted")
        self.assertEqual(len(await self.history_of(bob.id)), 2)
        self.assertEqual(
            await self.erase_audit(),
            [
                ("allow", "data_erased", bob.id),
                ("deny", "copies_pending", bob.id),
                ("allow", "copies_confirmed", bob.id),
                ("allow", "erased", bob.id),
            ],
        )

    async def test_without_the_copies_confirmed_the_user_stays_pending_deletion(self):
        # REQUIREMENTS.md "User Deletion Retention": not Deleted before the copies
        # outside the database (backups / WAL, the Linux account) are erased too.
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)

        report = await self.erasure().run()

        self.assertFalse(report.ok)
        self.assertEqual(
            [r.outcome for r in report.results], [ErasureOutcome.COPIES_PENDING]
        )
        # The database part does not wait for the operator.
        self.assertEqual(await self.rows_left(bob.id), {})
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertEqual(
            await self.history_of(bob.id), [("active", "pending_deletion")]
        )
        self.assertEqual(
            await self.erase_audit(),
            [("allow", "data_erased", bob.id), ("deny", "copies_pending", bob.id)],
        )
        # An erased but pending user cannot be restored (past the 30 days).
        self.clock.now = DUE
        _, owner_principal, owner_auth = await self.administrator("owner", "the-owner")
        with self.assertRaises(RetentionExpiredError):
            await self.lifecycle.restore_user(
                owner_principal, bob.id, self.context(), session_id=owner_auth.record.id
            )

        # Refused again every day until the operator confirms; then deleted.
        again = await self.erasure(DUE + timedelta(days=1)).run()
        self.assertEqual(
            [r.outcome for r in again.results], [ErasureOutcome.COPIES_PENDING]
        )
        other = uuid.uuid4()  # another id on the list changes nothing for bob
        report = await self.erasure(DUE + timedelta(days=2)).run(
            copies_erased=[bob.id, other]
        )

        self.assertTrue(report.ok)
        self.assertEqual([r.outcome for r in report.results], [ErasureOutcome.ERASED])
        self.assertEqual(await self.status_of(bob.id), "deleted")
        self.assertEqual(
            (await self.erase_audit())[-2:],
            [("allow", "copies_confirmed", bob.id), ("allow", "erased", bob.id)],
        )

    async def test_the_copies_confirmation_needs_an_erasure_committed_earlier(self):
        # Codex P1 (PR #142): the operator's "copies erased" cannot cover the
        # backups / WAL taken while the database rows still existed, so it is not
        # accepted in the run whose transaction deletes them. The first run
        # commits the database erasure; only a later run accepts it.
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)

        first = await self.erasure().run(copies_erased=[bob.id])

        self.assertFalse(first.ok)
        self.assertEqual(
            [r.outcome for r in first.results], [ErasureOutcome.COPIES_PENDING]
        )
        self.assertEqual(await self.rows_left(bob.id), {})
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertEqual(
            await self.erase_audit(),
            [("allow", "data_erased", bob.id), ("deny", "copies_pending", bob.id)],
        )

        second = await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])

        self.assertTrue(second.ok)
        self.assertEqual([r.outcome for r in second.results], [ErasureOutcome.ERASED])
        self.assertEqual(await self.status_of(bob.id), "deleted")

    async def test_rows_deleted_in_the_confirming_run_postpone_the_confirmation(self):
        # Personal rows that appear after the committed erasure are deleted by
        # the next run; that run's own deletions are not committed yet, so the
        # confirmation waits for another run again.
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)
        await self.erasure().run()
        await self.execute(
            "INSERT INTO connection_quotas (user_id, kind, metric, period, "
            "limit_value, created_at, updated_at) VALUES (:u, 'codex', 'requests', "
            "'day', 10, :now, :now)",
            u=bob.id,
            now=T0,
        )

        report = await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])

        self.assertEqual(
            [r.outcome for r in report.results], [ErasureOutcome.COPIES_PENDING]
        )
        self.assertEqual(await self.rows_left(bob.id), {})
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        report = await self.erasure(DUE + timedelta(days=2)).run(copies_erased=[bob.id])
        self.assertEqual([r.outcome for r in report.results], [ErasureOutcome.ERASED])

    async def test_an_erasure_of_a_user_who_is_not_due_is_refused_quietly(self):
        bob = await self.make_user("bob")

        result = await self.erasure().erase_user(bob.id)

        self.assertIs(result.outcome, ErasureOutcome.NOT_DUE)
        self.assertTrue(result.ok)
        self.assertEqual(await self.erase_audit(), [])


class RefusalTest(ErasureTestCase):
    async def test_an_active_task_keeps_the_user_pending_deletion(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        task = await self.tasks.create_task(
            project_id=uuid.uuid4(), created_by=bob.id, title="Still running"
        )
        await self.delete(bob)

        report = await self.erasure().run()

        self.assertFalse(report.ok)
        self.assertEqual(
            [r.outcome for r in report.results], [ErasureOutcome.TASKS_ACTIVE]
        )
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertIn("password_credentials", await self.rows_left(bob.id))
        self.assertEqual(await self.erase_audit(), [("deny", "tasks_active", bob.id)])

        # Once the task is stopped, the next run erases.
        await self.tasks.execute(task.task_id, TaskCommand.CANCEL, actor=Actor.policy())
        await self.erasure(DUE + timedelta(days=1)).run()
        self.assertEqual(await self.rows_left(bob.id), {})
        report = await self.erasure(DUE + timedelta(days=2)).run(copies_erased=[bob.id])
        self.assertTrue(report.ok)
        self.assertEqual(await self.status_of(bob.id), "deleted")

    async def test_an_active_queue_entry_of_a_finished_task_also_refuses(self):
        bob = await self.make_user("bob")
        task = await self.tasks.create_task(
            project_id=uuid.uuid4(), created_by=bob.id, title="Queued"
        )
        await self.queue.enqueue(task.task_id)
        await self.execute(
            "UPDATE tasks SET state = 'cancelled' WHERE id = :id", id=task.task_id
        )
        await self.delete(bob)

        result = (await self.erasure().run()).results[0]

        self.assertIs(result.outcome, ErasureOutcome.TASKS_ACTIVE)

    async def checkout(self, user_id) -> None:
        project_id = await self.project()
        repository_id = uuid.uuid4()
        await self.execute(
            "INSERT INTO repositories (id, project_id, name, default_branch, source, "
            "created_at, updated_at) VALUES (:id, :p, 'repo', 'main', 'new_local', "
            ":now, :now)",
            id=repository_id,
            p=project_id,
            now=T0,
        )
        await self.execute(
            "INSERT INTO repository_checkouts (id, repository_id, project_id, user_id, "
            "path, state, created_at, updated_at) VALUES (gen_random_uuid(), :r, :p, "
            ":u, '/home/bob/workspaces/alpha/repo', 'pending', :now, :now)",
            r=repository_id,
            p=project_id,
            u=user_id,
            now=T0,
        )

    async def test_managed_checkouts_block_until_the_operator_says_they_are_gone(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.checkout(bob.id)
        await self.delete(bob)

        refused = await self.erasure().run()

        self.assertEqual(
            [r.outcome for r in refused.results], [ErasureOutcome.CHECKOUTS_REMAINING]
        )
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertIn("password_credentials", await self.rows_left(bob.id))

        other = uuid.uuid4()  # another id on the list changes nothing for bob
        released = await self.erasure().run(
            checkouts_removed=[bob.id, other], copies_erased=[bob.id]
        )

        # Released and erased in this run: not committed yet when the operator
        # confirmed, so the confirmation counts on the next run.
        self.assertEqual(
            [r.outcome for r in released.results], [ErasureOutcome.COPIES_PENDING]
        )
        self.assertEqual(released.results[0].released_checkouts, 1)
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        report = await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])
        self.assertTrue(report.ok)
        self.assertEqual(await self.status_of(bob.id), "deleted")
        self.assertEqual(
            await self.count(
                "SELECT count(*) FROM repository_checkouts WHERE user_id = :id",
                id=bob.id,
            ),
            0,
        )
        self.assertEqual(
            await self.erase_audit(),
            [
                ("deny", "checkouts_remaining", bob.id),
                ("allow", "checkouts_released", bob.id),
                ("allow", "data_erased", bob.id),
                ("deny", "copies_pending", bob.id),
                ("allow", "copies_confirmed", bob.id),
                ("allow", "erased", bob.id),
            ],
        )

    async def test_a_failed_step_rolls_everything_back(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)
        service = self.erasure()
        record = service._record

        async def broken_record(session, reason, *args):
            if reason is AuthReason.DATA_ERASED:
                raise RuntimeError("disk on fire")
            await record(session, reason, *args)

        service._record = broken_record

        result = await service.erase_user(bob.id)

        self.assertIs(result.outcome, ErasureOutcome.FAILED)
        self.assertEqual(result.error_type, "RuntimeError")
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertIn("password_credentials", await self.rows_left(bob.id))
        self.assertEqual(await self.erase_audit(), [("deny", "erasure_failed", bob.id)])

    async def test_a_failed_mark_rolls_the_confirmation_back(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)
        await self.erasure().run()
        service = self.erasure(DUE + timedelta(days=1))

        async def broken(*_args):
            raise RuntimeError("disk on fire")

        service._mark_deleted = broken

        result = await service.erase_user(bob.id, copies_erased=True)

        self.assertIs(result.outcome, ErasureOutcome.FAILED)
        self.assertEqual(result.error_type, "RuntimeError")
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertEqual(
            await self.erase_audit(),
            [
                ("allow", "data_erased", bob.id),
                ("deny", "copies_pending", bob.id),
                ("deny", "erasure_failed", bob.id),
            ],
        )

    async def test_a_failed_verification_rolls_everything_back(self):
        bob = await self.make_user("bob")
        await self.personal_data(bob)
        await self.delete(bob)
        service = self.erasure()

        async def keeps_the_password(session, user_id, now):
            pass  # a step that "forgot" to delete anything

        service._delete_personal_data = keeps_the_password

        result = await service.erase_user(bob.id)

        self.assertIs(result.outcome, ErasureOutcome.VERIFICATION_FAILED)
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")
        self.assertEqual(
            await self.erase_audit(), [("deny", "verification_failed", bob.id)]
        )

    async def test_a_locked_user_row_is_reported_busy_not_waited_for(self):
        bob = await self.make_user("bob")
        await self.delete(bob)
        service = UserErasureService(
            self.database, clock=lambda: DUE, lock_timeout_ms=100
        )
        holder = self.new_database()
        async with holder.session() as session, session.begin():
            await session.execute(
                text("SELECT 1 FROM users WHERE id = :id FOR UPDATE"),
                {"id": bob.id},
            )
            result = await service.erase_user(bob.id)

        self.assertIs(result.outcome, ErasureOutcome.BUSY)
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")

    async def test_two_runs_at_once_the_second_is_refused(self):
        service = self.erasure()
        async with service.run_lock():
            with self.assertRaises(ErasureAlreadyRunningError):
                await self.erasure().run()
        # Released: a later run goes ahead.
        self.assertTrue((await self.erasure().run()).ok)


class WebRoleTest(ErasureTestCase):
    async def test_the_web_function_still_cannot_mark_a_user_deleted(self):
        bob = await self.make_user("bob")
        await self.delete(bob)

        with self.assertRaises(DBAPIError):
            await self.execute(
                "SELECT paw_change_user_status(:id, 'pending_deletion', 'deleted', "
                ":now, NULL)",
                id=bob.id,
                now=DUE,
            )
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")


class ExternalCredentialsTest(ErasureTestCase):
    """Codex P1 (PR #142, REQUIREMENTS.md "User Lifecycle" / "Shared Codex / Claude
    system connection"): the user's own GitHub / SSH credentials live in their Linux
    account, which the backend never touches (Decision 0043 C, point 4: revoking
    them is the deployment's work). The deletion therefore records that work as a
    required action, and every run of the job tells the Owner (exit 3) about each
    ``pending_deletion`` user until the operator confirms it
    (``credentials_revoked``), from the first day, not after the 30 days."""

    async def credentials_audit(self) -> list[tuple]:
        return [
            (row.decision, row.reason, row.resource_id)
            for row in await self.audit_rows()
            if row.action == "auth.user.credentials"
        ]

    async def test_the_deletion_records_the_revocation_as_required(self):
        bob = await self.make_user("bob")

        await self.delete(bob)

        self.assertEqual(
            await self.credentials_audit(), [("deny", "credentials_pending", bob.id)]
        )
        row = next(
            r for r in await self.audit_rows() if r.action == "auth.user.credentials"
        )
        self.assertEqual(row.actor_id, self.admin.user_id)

    async def test_an_unconfirmed_revocation_fails_every_run_from_the_first_day(self):
        bob = await self.make_user("bob")
        await self.delete(bob)

        report = await self.erasure(T0 + timedelta(hours=1)).run()

        # Nothing is due for the erasure yet ...
        self.assertEqual(report.results, ())
        self.assertTrue(report.ok)
        # ... but the Owner hears that the credentials are not revoked yet.
        self.assertFalse(report.credentials_ok)
        self.assertEqual(
            [(r.user_id, r.outcome) for r in report.credentials],
            [(bob.id, CredentialsOutcome.PENDING)],
        )
        self.assertEqual(
            await self.credentials_audit(),
            [
                ("deny", "credentials_pending", bob.id),
                ("deny", "credentials_pending", bob.id),
            ],
        )
        again = await self.erasure(T0 + timedelta(days=1)).run()
        self.assertFalse(again.credentials_ok)

    async def test_the_operator_s_confirmation_ends_the_reminder(self):
        bob = await self.make_user("bob")
        carol = await self.make_user("carol")
        await self.delete(bob)
        await self.delete(carol)

        report = await self.erasure(T0 + timedelta(hours=1)).run(
            credentials_revoked=[bob.id, uuid.uuid4()]
        )

        self.assertEqual(
            sorted((r.user_id, r.outcome) for r in report.credentials),
            sorted(
                [
                    (bob.id, CredentialsOutcome.REVOKED),
                    (carol.id, CredentialsOutcome.PENDING),
                ]
            ),
        )
        self.assertFalse(report.credentials_ok)  # carol is still pending
        self.assertIn(
            ("allow", "credentials_revoked", bob.id), await self.credentials_audit()
        )
        credentials = next(
            r
            for r in await self.audit_rows()
            if r.action == "auth.user.credentials" and r.decision == "allow"
        )
        self.assertIsNone(credentials.actor_id)

        later = await self.erasure(T0 + timedelta(days=1)).run(
            credentials_revoked=[carol.id]
        )
        self.assertTrue(later.credentials_ok)
        self.assertEqual(
            [(r.user_id, r.outcome) for r in later.credentials],
            [(carol.id, CredentialsOutcome.REVOKED)],
        )
        # Once confirmed, a user is not reported again.
        last = await self.erasure(T0 + timedelta(days=2)).run()
        self.assertTrue(last.credentials_ok)
        self.assertEqual(last.credentials, ())

    async def test_a_confirmation_does_not_carry_over_to_a_later_deletion(self):
        bob = await self.make_user("bob")
        await self.delete(bob)
        await self.erasure(T0 + timedelta(hours=1)).run(credentials_revoked=[bob.id])
        # Restored by the Owner, then deleted again: the credentials were usable
        # again in between, so the old confirmation does not count.
        await self.execute(
            "UPDATE users SET status = 'active' WHERE id = :id", id=bob.id
        )
        await self.execute(
            "INSERT INTO user_status_changes (id, user_id, old_status, new_status, "
            "changed_at, changed_by, recorded_at) VALUES (gen_random_uuid(), :u, "
            "'pending_deletion', 'active', :now, NULL, clock_timestamp())",
            u=bob.id,
            now=T0,
        )
        await self.delete(bob)

        report = await self.erasure(T0 + timedelta(days=1)).run()

        self.assertEqual(
            [(r.user_id, r.outcome) for r in report.credentials],
            [(bob.id, CredentialsOutcome.PENDING)],
        )

    async def test_a_user_restored_after_the_listing_is_not_reported(self):
        # Codex P2 (PR #142): the Owner restores the user between the run's listing
        # and the reminder. The reminder rechecks under the row lock: the operator
        # must not be told to revoke the credentials of an active user.
        bob = await self.make_user("bob")
        await self.delete(bob)
        await self.execute(
            "UPDATE users SET status = 'active' WHERE id = :id", id=bob.id
        )
        before = await self.credentials_audit()

        service = self.erasure(T0 + timedelta(hours=1))
        self.assertIsNone(await service.check_credentials(bob.id))
        self.assertIsNone(await service.check_credentials(bob.id, revoked=True))

        self.assertEqual(await self.credentials_audit(), before)

    async def test_active_restored_invited_owner_and_deleted_users_are_not_reported(
        self,
    ):
        await self.make_user("alice")
        await self.make_user("the-owner", role="owner")
        await self.make_user("ivy", status="invited", password=None)
        dave = await self.make_user("dave")
        await self.delete(dave)
        await self.execute(
            "UPDATE users SET status = 'active' WHERE id = :id", id=dave.id
        )
        erin = await self.make_user("erin", status="deleted")

        report = await self.erasure(T0 + timedelta(hours=1)).run(
            credentials_revoked=[dave.id, erin.id]
        )

        self.assertEqual(report.credentials, ())
        self.assertTrue(report.credentials_ok)
        self.assertNotIn(
            ("allow", "credentials_revoked", dave.id), await self.credentials_audit()
        )

    async def test_a_user_marked_deleted_by_the_run_is_not_reported(self):
        # The copies confirmation covers the credentials in the Linux account too
        # (Decision 0043 D 4): once the run marks the user deleted, no reminder.
        bob = await self.make_user("bob")
        await self.delete(bob)
        await self.erasure().run()

        report = await self.erasure(DUE + timedelta(days=1)).run(copies_erased=[bob.id])

        self.assertTrue(report.ok)
        self.assertEqual(await self.status_of(bob.id), "deleted")
        self.assertEqual(report.credentials, ())
        self.assertTrue(report.credentials_ok)


if __name__ == "__main__":
    unittest.main()
