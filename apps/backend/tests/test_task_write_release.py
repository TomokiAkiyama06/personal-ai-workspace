"""Releasing by hand the repository write of a crashed process (issue #129).

Decision 0048 (Proposed). ``TaskService.release_stale_repository_write`` (the
release itself: refused for a live holder, allowed for a stale one, the
repositories count as written, the history records who and why) and
``projects.TaskWriteReleaser`` (who may: authorization, audit, Passkey Step-up).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from paw_backend.auth.errors import StepUpMethodInsufficientError, StepUpRequiredError
from paw_backend.auth.onboarding.common import StepUpGuard
from paw_backend.authz import Authorizer, Capability, InMemoryAuditSink, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.authz.subjects import Principal
from paw_backend.projects import (
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectStateError,
    StepUpCheck,
    TaskWriteReleaser,
)
from paw_backend.tasks import (
    ActorKind,
    EvaluationResult,
    InvalidCommandArgumentError,
    ModifiedRepositoryDowngradeRefusedError,
    RepoRole,
    RepositoryChangeState,
    RepositoryWriteHolderAliveError,
    RepositoryWriteInFlightError,
    RepositoryWriteNotFoundError,
    RepositoryWriteNotHeldError,
    ReviewState,
    ReviewStatus,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskState,
    WorkingSetOperation,
)
from paw_backend.tasks.queueing import TaskQueue

from .gate_support import ALWAYS_ACTIVE
from .task_support import new_database, requires_postgres
from .test_task_working_set import BASE_B, WorkingSetTestCase

C = TaskCommand
Op = WorkingSetOperation
TARGET = RepoRole.TARGET
REASON = "the worker's host was rebooted; no executor process is left"


class ReleaseTestCase(WorkingSetTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.queue = TaskQueue(self.database, project_gate=ALWAYS_ACTIVE)

    async def owner_sql(self, sql: str, **parameters) -> None:
        """Change rows as the database's owner (what the application cannot)."""
        database = new_database()
        try:
            async with database.engine.begin() as connection:
                await connection.execute(text(sql), parameters)
        finally:
            await database.dispose()

    async def claimed(self, task_id: uuid.UUID) -> None:
        """A worker claims the task's queue entry (it holds a valid lease).

        Entries that earlier tests left waiting may come first: they are claimed
        too (their tasks are not used again)."""
        await self.queue.enqueue(task_id)
        for _ in range(1000):
            entry = await self.queue.claim_next("worker-1")
            self.assertIsNotNone(entry)
            if entry.task_id == task_id:
                return
        self.fail("the task's entry was never claimed")

    async def lease_lost(self, task_id: uuid.UUID) -> None:
        """The worker crashed: its lease ended an hour ago (nobody heartbeat)."""
        await self.owner_sql(
            "UPDATE queue_entries SET claimed_at = clock_timestamp() - interval "
            "'2 hours', lease_expires_at = clock_timestamp() - interval '1 hour' "
            "WHERE task_id = :t AND status = 'claimed'",
            t=task_id,
        )

    async def state_of(self, task_id, repository_id):
        snapshot = await self.service.restore(task_id)
        return snapshot.attempt.repository(repository_id)

    async def everything(self) -> tuple:
        """What a refused release must leave exactly as it was."""
        return (
            *await self.table_counts(),
            await self.scalar(
                "SELECT md5(string_agg(t::text, ',' ORDER BY id, repository_id)) "
                "FROM task_repository_writes t"
            ),
            await self.scalar(
                "SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM tasks t"
            ),
        )

    async def release(self, task_id, reservation, **kwargs):
        return await self.service.release_stale_repository_write(
            task_id,
            reservation,
            actor=kwargs.pop("actor", self.user),
            reason=kwargs.pop("reason", REASON),
            **kwargs,
        )


@requires_postgres
class ReleaseTest(ReleaseTestCase):
    async def test_a_stale_reservation_is_released_and_counts_as_written(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        with self.assertRaises(RepositoryWriteInFlightError):
            await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        before = await self.service.restore(task_id)

        event = await self.release(task_id, reservation)

        self.assertIs(event.command, C.RELEASE_REPOSITORY_WRITE)
        self.assertEqual((event.from_state, event.to_state), (TaskState.RUNNING,) * 2)
        self.assertEqual(
            (event.actor.kind, event.actor.id), (ActorKind.USER, self.user_id)
        )
        self.assertEqual(event.reason, REASON)
        self.assertEqual(event.detail["reservation_id"], str(reservation))
        self.assertEqual(event.detail["repository_ids"], [str(self.other)])
        self.assertEqual((event.detail["attempt"], event.detail["retry_count"]), (1, 0))
        self.assertIn("admitted_at", event.detail)
        self.assertIn("expires_at", event.detail)
        after = await self.service.restore(task_id)
        self.assertEqual(after.version, before.version + 1)
        self.assertEqual(after.state, TaskState.RUNNING)
        self.assertEqual(after.last_event, event)
        self.assertEqual(await self.live_reservations(task_id), 0)
        # What the crashed executor may have written counts: judged again.
        state = await self.state_of(task_id, self.other)
        self.assertTrue(state.modified)
        self.assertEqual(
            state.review,
            ReviewState(ReviewStatus.NOT_STARTED, EvaluationResult.NOT_RUN),
        )
        # The task is no longer held...
        await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        # ...and a downgrade still needs the change verifiably discarded.
        self.inspector.states[self.other] = RepositoryChangeState(
            False, BASE_B, False, False
        )
        with self.assertRaises(ModifiedRepositoryDowngradeRefusedError):
            await self.change(task_id, Op.DOWNGRADE_TO_WORKING, self.other, TARGET)
        self.inspector.clean_at(self.other, BASE_B)
        await self.change(task_id, Op.DOWNGRADE_TO_WORKING, self.other, TARGET)

    async def test_a_live_holder_keeps_its_reservation(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        await self.claimed(task_id)
        everything = await self.everything()
        with self.assertRaises(RepositoryWriteHolderAliveError) as caught:
            await self.release(task_id, reservation)
        self.assertEqual(caught.exception.code, "repository_write_holder_alive")
        self.assertEqual(await self.everything(), everything)
        self.assertEqual(await self.live_reservations(task_id), 1)
        # Once the lease ended (the worker is gone), the person may.
        await self.lease_lost(task_id)
        await self.release(task_id, reservation)
        self.assertEqual(await self.live_reservations(task_id), 0)

    async def test_a_queued_entry_is_no_holder(self):
        """A waiting entry has no worker: nothing of the task runs."""
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        await self.queue.enqueue(task_id)
        await self.release(task_id, reservation)
        self.assertEqual(await self.live_reservations(task_id), 0)

    async def test_the_holder_of_another_task_does_not_count(self):
        task_id = await self.two_targets()
        other_task = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        await self.claimed(other_task)
        await self.release(task_id, reservation)
        self.assertEqual(await self.live_reservations(task_id), 0)

    async def test_every_repository_of_the_reservation_is_released(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(
            task_id, self.repository_id, self.other, ended=False
        )
        event = await self.release(task_id, reservation)
        self.assertEqual(
            sorted(event.detail["repository_ids"]),
            sorted([str(self.repository_id), str(self.other)]),
        )
        self.assertEqual(await self.live_reservations(task_id), 0)
        for repository_id in (self.repository_id, self.other):
            self.assertTrue((await self.state_of(task_id, repository_id)).modified)

    async def test_only_a_reservation_that_still_holds_is_released(self):
        task_id = await self.two_targets()
        other_task = await self.two_targets()
        ended = await self.write_to(task_id, self.other)
        expired = await self.write_to(task_id, self.other, ended=False)
        await self.expire(expired)
        foreign = await self.write_to(other_task, self.other, ended=False)
        everything = await self.everything()
        for reservation, error in (
            (ended, RepositoryWriteNotHeldError),
            (expired, RepositoryWriteNotHeldError),
            (uuid.uuid4(), RepositoryWriteNotFoundError),
            # Another task's reservation is not found for this one.
            (foreign, RepositoryWriteNotFoundError),
        ):
            with self.subTest(error=error.__name__):
                with self.assertRaises(error):
                    await self.release(task_id, reservation)
        self.assertEqual(await self.everything(), everything)
        with self.assertRaises(TaskNotFoundError):
            await self.release(uuid.uuid4(), foreign)

    async def test_a_second_release_is_refused(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        await self.release(task_id, reservation)
        everything = await self.everything()
        with self.assertRaises(RepositoryWriteNotHeldError):
            await self.release(task_id, reservation)
        self.assertEqual(await self.everything(), everything)
        # The executor that comes back after all releases nothing more.
        await self.service.release_repository_use(task_id, reservation)
        self.assertEqual(await self.everything(), everything)

    async def test_an_old_runs_reservation_after_a_restart(self):
        """The write of an earlier attempt is released on that attempt's state;
        the new attempt starts clean (Decision 0030, section 1)."""
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        event = await self.release(task_id, reservation)
        self.assertEqual(event.detail["attempt"], 1)
        self.assertEqual(event.to_state, TaskState.QUEUED)
        self.assertFalse((await self.state_of(task_id, self.other)).modified)
        self.assertTrue(
            await self.scalar(
                "SELECT modified FROM task_attempt_repositories "
                "WHERE task_id = :t AND attempt = 1 AND repository_id = :r",
                t=task_id,
                r=self.other,
            )
        )

    async def test_a_failed_task_can_be_released_before_a_retry(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        event = await self.release(task_id, reservation)
        self.assertEqual(event.to_state, TaskState.FAILED)

    async def test_a_stale_expected_version_changes_nothing(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        version = (await self.service.restore(task_id)).version
        everything = await self.everything()
        with self.assertRaises(TaskConflictError):
            await self.release(task_id, reservation, expected_version=version + 1)
        self.assertEqual(await self.everything(), everything)
        await self.release(task_id, reservation, expected_version=version)

    async def test_a_failing_step_rolls_the_release_back(self):
        """The Step-up is checked in the release's transaction: a refusal leaves
        nothing of it."""
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        seen = []

        async def refuse(session, step_task, project_id):
            seen.append((step_task, project_id))
            # The release is written (in this transaction) when the step runs.
            live = await session.scalar(
                text(
                    "SELECT count(*) FROM task_repository_writes "
                    "WHERE id = :id AND released_at IS NULL"
                ),
                {"id": reservation},
            )
            self.assertEqual(live, 0)
            raise StepUpRequiredError

        everything = await self.everything()
        with self.assertRaises(StepUpRequiredError):
            await self.release(task_id, reservation, in_transaction=refuse)
        self.assertEqual(seen, [(task_id, self.project_id)])
        self.assertEqual(await self.everything(), everything)
        self.assertEqual(await self.live_reservations(task_id), 1)

    async def test_only_a_person_releases_and_says_why(self):
        task_id = await self.two_targets()
        reservation = await self.write_to(task_id, self.other, ended=False)
        for actor in (self.system, type(self.system).policy()):
            with self.subTest(actor=actor.kind.value):
                with self.assertRaises(InvalidCommandArgumentError):
                    await self.release(task_id, reservation, actor=actor)
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                with self.assertRaises(InvalidCommandArgumentError):
                    await self.release(task_id, reservation, reason=reason)
        self.assertEqual(await self.live_reservations(task_id), 1)


class FakeStepUp:
    """Records the check; raises ``error`` when set."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.error: Exception | None = None

    async def require_in(self, session, *, session_id, user_id, now):
        self.calls.append((session_id, user_id))
        if self.error is not None:
            raise self.error


class FailingSink:
    async def record(self, event) -> None:
        raise RuntimeError("audit store down")


@requires_postgres
class ReleaserTest(ReleaseTestCase):
    """Who may release (Decision 0048 Proposed): the project Manager and the
    Owner / Admin, with a Passkey Step-up; every decision is audited."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project_id = await self.seed_project("active")
        self.sink = InMemoryAuditSink()
        self.step_up = FakeStepUp()
        self.releaser = self.make_releaser(Authorizer(self.sink))
        self.session_id = uuid.uuid4()

    def make_releaser(self, authorizer: Authorizer) -> TaskWriteReleaser:
        return TaskWriteReleaser(
            self.database,
            tasks=self.service,
            authorizer=authorizer,
            step_up=self.step_up,
        )

    async def seed_user(self, system_role: str = "user") -> uuid.UUID:
        if system_role == "owner":
            # There is one Owner (``uq_users_single_owner``): reuse it.
            async with self.database.engine.connect() as connection:
                owner = (
                    await connection.execute(
                        text("SELECT id FROM users WHERE system_role = 'owner'")
                    )
                ).scalar_one_or_none()
            if owner is not None:
                return owner
        user_id = uuid.uuid4()
        now = datetime.now(UTC)
        await self.owner_sql(
            "INSERT INTO users (id, login_name, system_role, status, "
            "passkey_required, created_at, updated_at) VALUES (:id, :login, :role, "
            "'active', :passkey, :now, :now)",
            id=user_id,
            login="u" + user_id.hex[:12],
            role=system_role,
            passkey=system_role in ("owner", "admin"),
            now=now,
        )
        return user_id

    async def seed_project(self, status: str) -> uuid.UUID:
        project_id = uuid.uuid4()
        now = datetime.now(UTC)
        await self.owner_sql(
            "INSERT INTO projects (id, name, status, created_at, updated_at) "
            "VALUES (:id, :name, :status, :now, :now)",
            id=project_id,
            name="Release " + project_id.hex[:8],
            status=status,
            now=now,
        )
        return project_id

    async def member(self, role: str, *, system_role: str = "user") -> Principal:
        user_id = await self.seed_user(system_role)
        now = datetime.now(UTC)
        await self.owner_sql(
            "INSERT INTO project_members (project_id, user_id, role, status, "
            "invited_at, joined_at) VALUES (:p, :u, :role, 'active', :now, :now)",
            p=self.project_id,
            u=user_id,
            role=role,
            now=now,
        )
        return Principal(user_id, SystemRole(system_role))

    async def outsider(self, system_role: str = "user") -> Principal:
        return Principal(await self.seed_user(system_role), SystemRole(system_role))

    async def held(self) -> tuple[uuid.UUID, uuid.UUID]:
        task_id = await self.two_targets()
        return task_id, await self.write_to(task_id, self.other, ended=False)

    async def release_as(self, actor, task_id, reservation, releaser=None):
        return await (releaser or self.releaser).release(
            actor,
            task_id,
            reservation,
            reason=REASON,
            session_id=self.session_id,
        )

    def decisions(self) -> list[tuple]:
        return [
            (e.actor_id, e.action, e.project_id, e.decision, e.reason)
            for e in self.sink.events
        ]

    async def test_the_manager_releases_after_a_step_up(self):
        manager = await self.member("manager")
        task_id, reservation = await self.held()
        event = await self.release_as(manager, task_id, reservation)
        self.assertEqual(
            (event.actor.kind, event.actor.id), (ActorKind.USER, manager.user_id)
        )
        self.assertEqual(event.reason, REASON)
        self.assertEqual(await self.live_reservations(task_id), 0)
        self.assertEqual(self.step_up.calls, [(self.session_id, manager.user_id)])
        self.assertEqual(
            self.decisions(),
            [
                (
                    manager.user_id,
                    Capability.PROJECT_TASK_WRITE_RESERVATION_RELEASE.value,
                    self.project_id,
                    "allow",
                    Reason.GRANTED_BY_PROJECT_ROLE.value,
                )
            ],
        )

    async def test_the_owner_and_an_admin_release_without_membership(self):
        for role in ("owner", "admin"):
            with self.subTest(role=role):
                actor = await self.outsider(role)
                task_id, reservation = await self.held()
                await self.release_as(actor, task_id, reservation)
                self.assertEqual(await self.live_reservations(task_id), 0)
                self.assertEqual(
                    self.decisions()[-1][3:],
                    ("allow", Reason.GRANTED_BY_SYSTEM_ROLE.value),
                )

    async def test_a_contributor_or_viewer_is_denied_and_audited(self):
        for role in ("contributor", "viewer"):
            with self.subTest(role=role):
                actor = await self.member(role)
                task_id, reservation = await self.held()
                everything = await self.everything()
                with self.assertRaises(ProjectPermissionDeniedError) as caught:
                    await self.release_as(actor, task_id, reservation)
                self.assertIs(caught.exception.reason, Reason.CAPABILITY_NOT_GRANTED)
                self.assertEqual(await self.everything(), everything)
                self.assertEqual(
                    self.decisions()[-1],
                    (
                        actor.user_id,
                        Capability.PROJECT_TASK_WRITE_RESERVATION_RELEASE.value,
                        self.project_id,
                        "deny",
                        Reason.CAPABILITY_NOT_GRANTED.value,
                    ),
                )
        # Refused before the Step-up is even asked for.
        self.assertEqual(self.step_up.calls, [])

    async def test_a_non_member_does_not_learn_of_the_project(self):
        actor = await self.outsider()
        task_id, reservation = await self.held()
        everything = await self.everything()
        with self.assertRaises(ProjectNotFoundError):
            await self.release_as(actor, task_id, reservation)
        self.assertEqual(await self.everything(), everything)
        self.assertEqual(
            self.decisions()[-1][3:], ("deny", Reason.NOT_PROJECT_MEMBER.value)
        )

    async def test_the_manager_of_another_project_is_not_a_member(self):
        other_project = self.project_id
        manager = await self.member("manager")
        self.project_id = await self.seed_project("active")
        task_id, reservation = await self.held()
        self.assertNotEqual(self.project_id, other_project)
        with self.assertRaises(ProjectNotFoundError):
            await self.release_as(manager, task_id, reservation)
        self.assertEqual(await self.live_reservations(task_id), 1)

    async def test_only_an_active_project(self):
        manager = await self.member("manager")
        task_id, reservation = await self.held()
        await self.owner_sql(
            "UPDATE projects SET status = 'archived' WHERE id = :p", p=self.project_id
        )
        with self.assertRaises(ProjectStateError):
            await self.release_as(manager, task_id, reservation)
        self.assertEqual(await self.live_reservations(task_id), 1)
        self.assertEqual(
            self.decisions()[-1][3:], ("deny", Reason.PROJECT_STATE_FORBIDS.value)
        )

    async def test_without_a_step_up_nothing_is_released(self):
        manager = await self.member("manager")
        task_id, reservation = await self.held()
        everything = await self.everything()
        for error in (StepUpRequiredError(), StepUpMethodInsufficientError()):
            with self.subTest(error=type(error).__name__):
                self.step_up.error = error
                with self.assertRaises(type(error)):
                    await self.release_as(manager, task_id, reservation)
                self.assertEqual(await self.everything(), everything)
        self.step_up.error = None
        await self.release_as(manager, task_id, reservation)
        self.assertEqual(await self.live_reservations(task_id), 0)

    async def test_an_unavailable_audit_denies(self):
        manager = await self.member("manager")
        task_id, reservation = await self.held()
        releaser = self.make_releaser(Authorizer(FailingSink(), timeout_seconds=1.0))
        everything = await self.everything()
        with self.assertRaises(ProjectPermissionDeniedError) as caught:
            await self.release_as(manager, task_id, reservation, releaser)
        self.assertIs(caught.exception.reason, Reason.AUDIT_UNAVAILABLE)
        self.assertEqual(await self.everything(), everything)

    async def test_an_authorized_release_still_spares_a_live_holder(self):
        manager = await self.member("manager")
        task_id, reservation = await self.held()
        await self.claimed(task_id)
        with self.assertRaises(RepositoryWriteHolderAliveError):
            await self.release_as(manager, task_id, reservation)
        self.assertEqual(await self.live_reservations(task_id), 1)

    async def test_an_unknown_task_is_not_found(self):
        manager = await self.member("manager")
        with self.assertRaises(TaskNotFoundError):
            await self.release_as(manager, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(self.sink.events, [])

    async def test_arguments_are_checked(self):
        manager = await self.member("manager")
        task_id, reservation = await self.held()
        for arguments in (
            dict(actor=object()),
            dict(task_id=str(task_id)),
            dict(reservation_id=str(reservation)),
            dict(session_id=None),
            dict(reason=""),
            dict(reason="  "),
            dict(reason=None),
            dict(reason="x" * 501),
            dict(expected_version=0),
            dict(expected_version="1"),
        ):
            with self.subTest(arguments=list(arguments)):
                call = dict(
                    actor=manager,
                    task_id=task_id,
                    reservation_id=reservation,
                    reason=REASON,
                    session_id=self.session_id,
                )
                call.update(arguments)
                with self.assertRaises(InvalidCommandArgumentError):
                    await self.releaser.release(
                        call.pop("actor"),
                        call.pop("task_id"),
                        call.pop("reservation_id"),
                        **call,
                    )
        self.assertEqual(self.sink.events, [])

    def test_the_passkey_step_up_guard_is_the_check(self):
        """What production passes as ``step_up`` (the check of every other
        sensitive operation of an Owner or an Admin) fits the protocol."""
        self.assertTrue(issubclass(StepUpGuard, StepUpCheck))
        self.assertIsInstance(StepUpGuard(policy=None), StepUpCheck)
        with self.assertRaises(TypeError):
            TaskWriteReleaser(
                self.database,
                tasks=self.service,
                authorizer=Authorizer(self.sink),
                step_up=object(),
            )
