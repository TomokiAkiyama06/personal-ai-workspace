"""The Working Set of a task on a real PostgreSQL (issue #85, Decisions 0014, 0030).

* it is stored (``task_repositories``: repository, role, starting commit, who
  added it) with the state of each repository in each attempt
  (``task_attempt_repositories``), and ``restore`` returns both;
* every change goes through ``TaskService.change_working_set``, is recorded in
  ``task_events`` with its actor, keeps a ``target`` (serialised on the task row:
  #85 constraint 4) and refuses to downgrade or remove a repository whose change
  was not verifiably discarded, judged against that repository's own starting
  commit (#85 constraint 1);
* Start needs a ``target``; Complete needs every repository to meet its
  requirements, a target's review approved included (#85 constraint 5);
* the Tool Broker's Working Set tools run through ``WorkingSetExecutor``.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import asyncio
import uuid

from sqlalchemy import text

from paw_backend.authz import Capability, ProjectRole, RepoAcl, SystemRole
from paw_backend.tasks import (
    Actor,
    CompletionRequirementsNotMetError,
    EvaluationResult,
    IllegalTransitionError,
    InvalidCommandArgumentError,
    LastTargetRemovalRefusedError,
    ModifiedRepositoryDowngradeRefusedError,
    NoTargetRepositoryError,
    PullRequestInfo,
    PullRequestState,
    RepoRole,
    RepositoryChangeState,
    RepositoryNotInAttemptError,
    RepositoryRoleInsufficientError,
    RepositoryRoleUnresolvedError,
    ReviewState,
    ReviewStatus,
    StaleAttemptError,
    TaskCommand,
    TaskService,
    TaskState,
    WorkingSetChangeInvalidError,
    WorkingSetConflictError,
    WorkingSetEntry,
    WorkingSetOperation,
    WorktreeState,
)
from paw_backend.tasks.service import MAX_WORKING_SET_REPOSITORIES
from paw_backend.tools import (
    WORKING_SET_TOOL_SPECS,
    ArgumentKind,
    ArgumentSpec,
    ScopedRepository,
    ToolCapability,
    ToolRegistry,
    ToolRunner,
    ToolSpec,
    WorkingSetExecutor,
    with_working_set_roles,
)
from paw_backend.tools.runner import ExecutionStatus

from .authz_support import StaticDirectory, principal
from .gate_support import ALWAYS_ACTIVE
from .task_support import (
    FIRST_RUN,
    OPEN_PULL_REQUEST,
    PASSED_REVIEW,
    PostgresTaskTestCase,
    requires_postgres,
)
from .tools_support import (
    P1,
    ROOT,
    Harness,
    Registrations,
    make_call,
    make_context,
    make_grant,
    make_scope,
    sample_specs,
)

C = TaskCommand
S = TaskState
Op = WorkingSetOperation
REFERENCED, WORKING, TARGET = RepoRole.REFERENCED, RepoRole.WORKING, RepoRole.TARGET
BASE_A = "a" * 40
BASE_B = "b" * 40


def run_in_repository() -> ToolSpec:
    """Tests / a build / a command in a repository (``project.task.run``)."""
    return ToolSpec(
        "tests.run_in",
        frozenset({ToolCapability.EXECUTE}),
        Capability.PROJECT_TASK_RUN,
        {"path": ArgumentSpec(ArgumentKind.PATH)},
    )


class Inspector:
    """Reports, per repository, what a test says the repository looks like."""

    def __init__(self) -> None:
        self.states: dict[uuid.UUID, RepositoryChangeState | None] = {}
        self.calls: list[tuple[uuid.UUID, int, uuid.UUID]] = []

    def clean_at(self, repository_id: uuid.UUID, head: str) -> None:
        self.states[repository_id] = RepositoryChangeState(True, head, False, False)

    async def inspect(self, task_id, attempt, repository_id):
        self.calls.append((task_id, attempt, repository_id))
        return self.states.get(repository_id)


class WorkingSetTestCase(PostgresTaskTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.inspector = Inspector()
        self.service = TaskService(
            self.database, project_gate=ALWAYS_ACTIVE, change_inspector=self.inspector
        )
        self.other = uuid.uuid4()  # a second repository

    async def two_targets(self) -> uuid.UUID:
        """A running task whose Working Set has two targets with their own baselines."""
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, TARGET, BASE_B),
            ]
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        return task_id

    async def change(self, task_id, operation, repository_id, expected, **kwargs):
        return await self.service.change_working_set(
            task_id,
            operation,
            repository_id,
            actor=kwargs.pop("actor", self.user),
            expected_role=expected,
            **kwargs,
        )

    async def write_to(self, task_id, *repository_ids, run=FIRST_RUN):
        """The Tool Broker admitted a repository write on them."""
        await self.service.admit_repository_use(
            task_id,
            run,
            list(repository_ids),
            capability=Capability.PROJECT_REPO_WRITE,
            executes=False,
        )

    async def roles(self, task_id) -> dict[uuid.UUID, RepoRole]:
        snapshot = await self.service.restore(task_id)
        return {entry.repository_id: entry.role for entry in snapshot.working_set}

    async def table_counts(self) -> tuple:
        return (
            await self.scalar("SELECT count(*) FROM task_events"),
            await self.scalar("SELECT count(*) FROM task_repositories"),
            await self.scalar(
                "SELECT md5(string_agg(t::text, ',' ORDER BY task_id, repository_id)) "
                "FROM task_repositories t"
            ),
            await self.scalar(
                "SELECT md5(string_agg(t::text, ',' ORDER BY id)) "
                "FROM task_attempt_repositories t"
            ),
        )


@requires_postgres
class PersistenceTest(WorkingSetTestCase):
    async def test_restore_returns_the_working_set_and_each_repositorys_state(self):
        task_id = await self.two_targets()
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.other,
            worktree=WorktreeState("agent/b", "/srv/w/b", BASE_B),
        )
        fresh = TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE)
        snapshot = await fresh.restore(task_id)
        self.assertEqual(snapshot, await self.service.restore(task_id))
        self.assertEqual(
            [
                (e.repository_id, e.role, e.starting_commit, e.added_by)
                for e in snapshot.working_set
            ],
            [
                (self.repository_id, TARGET, BASE_A, self.user),
                (self.other, TARGET, BASE_B, self.user),
            ],
        )
        self.assertEqual(snapshot.repository(self.other).starting_commit, BASE_B)
        self.assertIsNone(snapshot.repository(uuid.uuid4()))
        self.assertEqual(
            {r.repository_id for r in snapshot.attempt.repositories},
            {self.repository_id, self.other},
        )
        self.assertEqual(
            snapshot.attempt.repository(self.other).worktree,
            WorktreeState("agent/b", "/srv/w/b", BASE_B),
        )
        self.assertEqual(
            snapshot.attempt.repository(self.repository_id).worktree, WorktreeState()
        )

    async def test_a_task_without_a_target_cannot_start(self):
        for repositories in ([], None):
            with self.subTest(repositories=repositories):
                overrides = {} if repositories is None else {"repositories": []}
                task_id = (
                    await self.service.create_task(
                        project_id=self.project_id,
                        created_by=self.user_id,
                        title="No target",
                        **overrides,
                    )
                ).task_id
                before = await self.service.restore(task_id)
                with self.assertRaises(NoTargetRepositoryError) as caught:
                    await self.service.execute(task_id, C.START, actor=self.system)
                self.assertEqual(caught.exception.code, "no_target_repository")
                self.assertEqual(await self.service.restore(task_id), before)
                # Giving it a target makes it startable.
                await self.change(task_id, Op.SET_TARGET, self.repository_id, None)
                event = await self.service.execute(task_id, C.START, actor=self.system)
                self.assertEqual(event.to_state, S.RUNNING)

    async def test_no_command_puts_a_task_without_a_target_back_to_running(self):
        """A task of before revision 0085 has an empty Working Set: Resume and
        Unblock (not only Start) refuse to run it until it gets a target."""
        for state, command in ((S.PAUSED, C.RESUME), (S.WAITING, C.UNBLOCK)):
            with self.subTest(command=command.value):
                task_id = await self.task_in_state(state)
                async with self.database.engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE task_repositories SET removed_at = now() "
                            "WHERE task_id = :t"
                        ),
                        {"t": task_id},
                    )
                before = await self.service.restore(task_id)
                with self.assertRaises(NoTargetRepositoryError):
                    await self.service.execute(task_id, command, actor=self.user)
                self.assertEqual(await self.service.restore(task_id), before)

    async def test_a_working_set_needs_a_target_when_it_is_given(self):
        with self.assertRaises(InvalidCommandArgumentError):
            await self.create_task(
                repositories=[WorkingSetEntry(self.repository_id, WORKING)]
            )

    async def test_restart_keeps_the_working_set_and_each_starting_commit(self):
        """#85 constraint 1: a baseline per repository, reused by Restart."""
        task_id = await self.two_targets()
        await self.change(
            task_id, Op.ADD_REFERENCED, uuid.UUID(int=9), None, starting_commit="c" * 40
        )
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.repository_id,
            review=PASSED_REVIEW,
            pull_request=OPEN_PULL_REQUEST,
        )
        before = (await self.service.restore(task_id)).working_set
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.working_set, before)
        self.assertEqual(
            [e.starting_commit for e in snapshot.working_set],
            [BASE_A, BASE_B, "c" * 40],
        )
        # The new attempt starts every repository afresh; the old one is history.
        self.assertEqual(snapshot.attempt.number, 2)
        self.assertEqual(len(snapshot.attempt.repositories), 3)
        for state in snapshot.attempt.repositories:
            self.assertEqual(
                (state.review, state.pull_request, state.modified),
                (ReviewState(), None, False),
            )
        (old,) = snapshot.previous_attempts
        self.assertEqual(
            old.repository(self.repository_id).pull_request, OPEN_PULL_REQUEST
        )

    async def test_update_attempt_names_a_repository_of_the_attempt(self):
        task_id = await self.two_targets()
        with self.assertRaises(RepositoryNotInAttemptError):
            await self.service.update_attempt(
                task_id, run=FIRST_RUN, repository_id=uuid.uuid4(), review=ReviewState()
            )


@requires_postgres
class ChangeTest(WorkingSetTestCase):
    async def test_each_change_is_recorded_with_its_actor(self):
        task_id = await self.two_targets()
        agent = uuid.uuid4()
        new = uuid.uuid4()
        before = await self.service.restore(task_id)
        event = await self.change(
            task_id,
            Op.ADD_REFERENCED,
            new,
            None,
            starting_commit="c" * 40,
            agent_id=agent,
            reason="read its API",
        )
        self.assertEqual(
            (event.command, event.from_state, event.to_state),
            (C.CHANGE_WORKING_SET, S.RUNNING, S.RUNNING),
        )
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.reason, "read its API")
        self.assertEqual(
            event.detail,
            {
                "operation": "add_referenced",
                "repository_id": str(new),
                "from_role": None,
                "to_role": "referenced",
                "starting_commit": "c" * 40,
                "agent_id": str(agent),
            },
        )
        self.assertEqual(event.task_version, before.version + 1)
        promoted = await self.change(
            task_id, Op.SET_WORKING, new, REFERENCED, actor=Actor.system()
        )
        self.assertEqual(
            (promoted.detail["from_role"], promoted.detail["to_role"]),
            ("referenced", "working"),
        )
        snapshot = await self.service.restore(task_id)
        entry = snapshot.repository(new)
        self.assertEqual((entry.role, entry.added_by), (WORKING, Actor.system()))
        self.assertEqual(entry.starting_commit, "c" * 40)
        self.assertEqual(snapshot.attempt.repository(new).strongest_role, WORKING)
        history = [e.command for e in await self.service.history(task_id)]
        self.assertEqual(history.count(C.CHANGE_WORKING_SET), 2)
        self.assertEqual(snapshot.last_event, promoted)

    async def test_a_change_decided_on_a_stale_role_is_refused(self):
        task_id = await self.two_targets()
        counts = await self.table_counts()
        for expected in (None, WORKING, REFERENCED):
            with self.subTest(expected=expected):
                with self.assertRaises(WorkingSetConflictError):
                    await self.change(
                        task_id, Op.DOWNGRADE_TO_WORKING, self.other, expected
                    )
        self.assertEqual(await self.table_counts(), counts)

    async def test_an_operation_that_does_not_fit_the_role_is_refused(self):
        task_id = await self.two_targets()
        await self.change(task_id, Op.ADD_REFERENCED, uuid.UUID(int=7), None)
        cases = [
            (Op.ADD_REFERENCED, uuid.UUID(int=7), REFERENCED),
            (Op.SET_WORKING, self.other, TARGET),
            (Op.SET_TARGET, self.other, TARGET),
            (Op.DOWNGRADE_TO_WORKING, uuid.UUID(int=7), REFERENCED),
            (Op.DOWNGRADE_TO_REFERENCED, uuid.UUID(int=7), REFERENCED),
            (Op.REMOVE, uuid.uuid4(), None),
        ]
        counts = await self.table_counts()
        for operation, repository_id, expected in cases:
            with self.subTest(operation=operation.value):
                with self.assertRaises(WorkingSetChangeInvalidError):
                    await self.change(task_id, operation, repository_id, expected)
        self.assertEqual(await self.table_counts(), counts)

    async def test_the_only_target_is_never_downgraded_or_removed(self):
        task_id = await self.task_in_state(S.RUNNING)
        self.inspector.clean_at(self.repository_id, "0" * 40)
        counts = await self.table_counts()
        for operation in (
            Op.DOWNGRADE_TO_WORKING,
            Op.DOWNGRADE_TO_REFERENCED,
            Op.REMOVE,
        ):
            with self.subTest(operation=operation.value):
                with self.assertRaises(LastTargetRemovalRefusedError) as caught:
                    await self.change(task_id, operation, self.repository_id, TARGET)
                self.assertEqual(caught.exception.code, "last_target_removal_refused")
        self.assertEqual(await self.table_counts(), counts)
        # With a second target first, it can go.
        await self.change(task_id, Op.SET_TARGET, self.other, None)
        await self.change(task_id, Op.REMOVE, self.repository_id, TARGET)
        self.assertEqual(await self.roles(task_id), {self.other: TARGET})

    async def test_nothing_changes_after_the_task_completed(self):
        task_id = await self.task_in_state(S.COMPLETED)
        with self.assertRaises(IllegalTransitionError):
            await self.change(task_id, Op.ADD_REFERENCED, uuid.uuid4(), None)

    async def test_the_working_set_is_bounded(self):
        task_id = await self.task_in_state(S.QUEUED)
        for index in range(MAX_WORKING_SET_REPOSITORIES - 1):
            await self.change(
                task_id, Op.ADD_REFERENCED, uuid.UUID(int=1000 + index), None
            )
        with self.assertRaises(InvalidCommandArgumentError):
            await self.change(task_id, Op.ADD_REFERENCED, uuid.uuid4(), None)
        # A removed one does not count.
        await self.change(task_id, Op.REMOVE, uuid.UUID(int=1000), REFERENCED)
        await self.change(task_id, Op.ADD_REFERENCED, uuid.uuid4(), None)

    async def test_a_removed_repository_can_join_again_with_a_new_baseline(self):
        task_id = await self.task_in_state(S.RUNNING)
        new = uuid.uuid4()
        await self.change(task_id, Op.ADD_REFERENCED, new, None, starting_commit=BASE_A)
        await self.change(task_id, Op.REMOVE, new, REFERENCED)
        self.assertNotIn(new, await self.roles(task_id))
        await self.change(task_id, Op.SET_WORKING, new, None, starting_commit=BASE_B)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            (snapshot.repository(new).role, snapshot.repository(new).starting_commit),
            (WORKING, BASE_B),
        )


@requires_postgres
class ConcurrencyTest(WorkingSetTestCase):
    """#85 constraint 4: two downgrades of two targets cannot both succeed."""

    async def test_two_concurrent_downgrades_leave_one_target(self):
        task_id = await self.two_targets()
        self.inspector.clean_at(self.repository_id, BASE_A)
        self.inspector.clean_at(self.other, BASE_B)
        first = TaskService(
            self.new_database(),
            project_gate=ALWAYS_ACTIVE,
            change_inspector=self.inspector,
        )
        second = TaskService(
            self.new_database(),
            project_gate=ALWAYS_ACTIVE,
            change_inspector=self.inspector,
        )
        async with self.database.engine.connect() as blocker:
            # Both changes wait for the task's row lock the blocker holds, having
            # each seen two targets before it; then they run one after the other.
            await blocker.execute(
                text("SELECT 1 FROM tasks WHERE id = :id FOR UPDATE"), {"id": task_id}
            )
            downgrades = [
                asyncio.create_task(
                    service.change_working_set(
                        task_id,
                        Op.DOWNGRADE_TO_REFERENCED,
                        repository_id,
                        actor=self.user,
                        expected_role=TARGET,
                    )
                )
                for service, repository_id in (
                    (first, self.repository_id),
                    (second, self.other),
                )
            ]
            await self.wait_for_lock_waiters(2)
            await blocker.rollback()
        results = await asyncio.gather(*downgrades, return_exceptions=True)
        refused = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(refused), 1, results)
        self.assertIsInstance(refused[0], LastTargetRemovalRefusedError)
        roles = await self.roles(task_id)
        self.assertEqual(list(roles.values()).count(TARGET), 1)

    async def test_many_concurrent_removals_of_targets_keep_one(self):
        task_id = await self.two_targets()
        third = uuid.uuid4()
        await self.change(task_id, Op.SET_TARGET, third, None, starting_commit=BASE_A)
        for repository_id, base in (
            (self.repository_id, BASE_A),
            (self.other, BASE_B),
            (third, BASE_A),
        ):
            self.inspector.clean_at(repository_id, base)
        services = [
            TaskService(
                self.new_database(),
                project_gate=ALWAYS_ACTIVE,
                change_inspector=self.inspector,
            )
            for _ in range(3)
        ]
        results = await asyncio.gather(
            *(
                service.change_working_set(
                    task_id,
                    Op.REMOVE,
                    repository_id,
                    actor=self.user,
                    expected_role=TARGET,
                )
                for service, repository_id in zip(
                    services, (self.repository_id, self.other, third), strict=True
                )
            ),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(isinstance(r, LastTargetRemovalRefusedError) for r in results), 1
        )
        self.assertEqual(list((await self.roles(task_id)).values()), [TARGET])


@requires_postgres
class DiscardTest(WorkingSetTestCase):
    """Decision 0030, section 3; #85 constraint 1: judged per repository."""

    async def test_a_downgrade_needs_the_change_verifiably_discarded(self):
        task_id = await self.two_targets()
        cases = {
            "the inspector cannot tell": None,
            "dirty worktree": RepositoryChangeState(False, BASE_B, False, False),
            # The OTHER repository's baseline is not this one's.
            "HEAD at another repository's baseline": RepositoryChangeState(
                True, BASE_A, False, False
            ),
            "branch pushed": RepositoryChangeState(True, BASE_B, True, False),
            "pull request open": RepositoryChangeState(True, BASE_B, False, True),
        }
        counts = await self.table_counts()
        for label, found in cases.items():
            with self.subTest(label):
                self.inspector.states[self.other] = found
                with self.assertRaises(ModifiedRepositoryDowngradeRefusedError) as c:
                    await self.change(
                        task_id, Op.DOWNGRADE_TO_REFERENCED, self.other, TARGET
                    )
                self.assertEqual(
                    c.exception.code, "modified_repository_downgrade_refused"
                )
        self.assertEqual(await self.table_counts(), counts)
        self.inspector.clean_at(self.other, BASE_B)
        event = await self.change(
            task_id, Op.DOWNGRADE_TO_REFERENCED, self.other, TARGET
        )
        self.assertEqual(
            event.detail["discarded"],
            {
                "head_commit": BASE_B,
                "clean": True,
                "branch_pushed": False,
                "open_pull_request": False,
            },
        )
        self.assertEqual(self.inspector.calls[-1], (task_id, 1, self.other))
        state = (await self.service.restore(task_id)).attempt.repository(self.other)
        self.assertEqual((state.strongest_role, state.modified), (REFERENCED, False))

    async def test_a_stored_open_pull_request_is_never_discarded(self):
        task_id = await self.two_targets()
        self.inspector.clean_at(self.other, BASE_B)
        for state in (PullRequestState.OPEN, PullRequestState.DRAFT):
            with self.subTest(state=state.value):
                await self.service.update_attempt(
                    task_id,
                    run=FIRST_RUN,
                    repository_id=self.other,
                    pull_request=PullRequestInfo(4, "https://example.test/4", state),
                )
                with self.assertRaises(ModifiedRepositoryDowngradeRefusedError):
                    await self.change(task_id, Op.REMOVE, self.other, TARGET)

    async def test_a_repository_without_a_baseline_cannot_be_verified(self):
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, WORKING, None),
            ]
        )
        self.inspector.clean_at(self.other, BASE_A)
        with self.assertRaises(ModifiedRepositoryDowngradeRefusedError):
            await self.change(task_id, Op.DOWNGRADE_TO_REFERENCED, self.other, WORKING)
        # Nothing was asked: there is nothing to compare with.
        self.assertEqual(self.inspector.calls, [])

    async def test_without_an_inspector_no_write_role_is_ever_narrowed(self):
        service = TaskService(self.database, project_gate=ALWAYS_ACTIVE)
        task_id = await self.two_targets()
        with self.assertRaises(ModifiedRepositoryDowngradeRefusedError):
            await service.change_working_set(
                task_id,
                Op.DOWNGRADE_TO_WORKING,
                self.other,
                actor=self.user,
                expected_role=TARGET,
            )

    async def test_a_repository_that_was_only_ever_referenced_needs_no_inspection(
        self,
    ):
        task_id = await self.two_targets()
        new = uuid.uuid4()
        await self.change(task_id, Op.ADD_REFERENCED, new, None)
        await self.change(task_id, Op.REMOVE, new, REFERENCED)
        self.assertEqual(self.inspector.calls, [])

    async def test_a_repository_that_was_working_in_the_attempt_is_inspected(self):
        # Downgraded to referenced (verified), then removed: its strongest role in
        # the attempt was reset by the verified discard, so no second inspection.
        task_id = await self.two_targets()
        self.inspector.clean_at(self.other, BASE_B)
        await self.change(task_id, Op.DOWNGRADE_TO_REFERENCED, self.other, TARGET)
        await self.change(task_id, Op.REMOVE, self.other, REFERENCED)
        self.assertEqual(len(self.inspector.calls), 1)


@requires_postgres
class RepositoryUseTest(WorkingSetTestCase):
    """``admit_repository_use``: the Tool Broker's use of a repository, judged on
    the role stored now (Decision 0030, 4.1 / 4.6), with the ceiling of section 4
    (#85 constraint 2); a write or an execution marks it changed (section 5)."""

    async def use(self, task_id, repository_id, capability, executes=False):
        await self.service.admit_repository_use(
            task_id,
            FIRST_RUN,
            [repository_id],
            capability=capability,
            executes=executes,
        )

    async def modified(self, task_id, repository_id) -> bool:
        snapshot = await self.service.restore(task_id)
        return snapshot.attempt.repository(repository_id).modified

    async def test_a_write_marks_the_repository_changed(self):
        task_id = await self.two_targets()
        await self.write_to(task_id, self.other)
        self.assertTrue(await self.modified(task_id, self.other))

    async def test_an_execution_marks_a_working_repository_changed(self):
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, WORKING, BASE_B),
            ]
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.use(task_id, self.other, Capability.PROJECT_READ)
        self.assertFalse(await self.modified(task_id, self.other))
        await self.use(task_id, self.other, Capability.PROJECT_TASK_RUN, True)
        self.assertTrue(await self.modified(task_id, self.other))

    async def test_the_stored_role_decides_not_the_callers_scope(self):
        """A target downgraded to ``working`` gets no pull request, and one
        downgraded to ``referenced`` runs nothing, whatever a stale scope says."""
        task_id = await self.two_targets()
        self.inspector.clean_at(self.other, BASE_B)
        await self.change(task_id, Op.DOWNGRADE_TO_WORKING, self.other, TARGET)
        counts = await self.table_counts()
        with self.assertRaises(RepositoryRoleInsufficientError):
            await self.use(task_id, self.other, Capability.PROJECT_PR_CREATE)
        self.assertEqual(await self.table_counts(), counts)
        await self.change(task_id, Op.DOWNGRADE_TO_REFERENCED, self.other, WORKING)
        counts = await self.table_counts()
        for capability, executes in (
            (Capability.PROJECT_REPO_WRITE, False),
            (Capability.PROJECT_TASK_RUN, True),
        ):
            with self.subTest(capability=capability.value):
                with self.assertRaises(RepositoryRoleInsufficientError):
                    await self.use(task_id, self.other, capability, executes)
        self.assertEqual(await self.table_counts(), counts)
        # Reading a referenced repository is what it is for.
        await self.use(task_id, self.other, Capability.PROJECT_READ)

    async def test_a_repository_outside_the_working_set_is_unresolved(self):
        task_id = await self.two_targets()
        new = uuid.uuid4()
        await self.change(task_id, Op.ADD_REFERENCED, new, None)
        await self.change(task_id, Op.REMOVE, new, REFERENCED)
        counts = await self.table_counts()
        for repository_ids in ([new], [uuid.uuid4()], [self.other, new]):
            with self.subTest(repository_ids=repository_ids):
                with self.assertRaises(RepositoryRoleUnresolvedError):
                    await self.service.admit_repository_use(
                        task_id,
                        FIRST_RUN,
                        repository_ids,
                        capability=Capability.PROJECT_READ,
                        executes=False,
                    )
        self.assertEqual(await self.table_counts(), counts)

    async def test_only_a_working_or_target_repository_is_written(self):
        task_id = await self.two_targets()
        new = uuid.uuid4()
        await self.change(task_id, Op.ADD_REFERENCED, new, None)
        counts = await self.table_counts()
        with self.assertRaises(RepositoryRoleInsufficientError):
            await self.write_to(task_id, self.other, new)
        self.assertEqual(await self.table_counts(), counts)

    async def test_a_superseded_run_writes_nothing(self):
        task_id = await self.two_targets()
        await self.service.execute(task_id, C.FAIL, actor=self.system)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        with self.assertRaises(StaleAttemptError):
            await self.write_to(task_id, self.other)


@requires_postgres
class CompletionTest(WorkingSetTestCase):
    async def evaluating(self, repositories) -> uuid.UUID:
        task_id = await self.create_task(repositories=repositories)
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        return task_id

    async def meet(
        self, task_id, repository_id, review=PASSED_REVIEW, pr=OPEN_PULL_REQUEST
    ):
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=repository_id,
            review=review,
            pull_request=pr,
        )

    async def assert_not_completable(self, task_id):
        before = await self.service.restore(task_id)
        with self.assertRaises(CompletionRequirementsNotMetError) as caught:
            await self.service.execute(task_id, C.COMPLETE, actor=self.system)
        self.assertEqual(caught.exception.code, "completion_requirements_not_met")
        self.assertEqual(await self.service.restore(task_id), before)

    async def test_a_target_needs_evaluation_pull_request_and_approved_review(self):
        cases = {
            "nothing yet": None,
            "evaluation failed": (
                ReviewState(ReviewStatus.APPROVED, EvaluationResult.FAILED),
                OPEN_PULL_REQUEST,
            ),
            "no pull request": (PASSED_REVIEW, None),
            "draft pull request": (
                PASSED_REVIEW,
                PullRequestInfo(1, "https://example.test/1", PullRequestState.DRAFT),
            ),
            "closed pull request": (
                PASSED_REVIEW,
                PullRequestInfo(1, "https://example.test/1", PullRequestState.CLOSED),
            ),
            # #85 constraint 5
            "changes requested": (
                ReviewState(ReviewStatus.CHANGES_REQUESTED, EvaluationResult.PASSED),
                OPEN_PULL_REQUEST,
            ),
            "review not started": (
                ReviewState(ReviewStatus.NOT_STARTED, EvaluationResult.PASSED),
                OPEN_PULL_REQUEST,
            ),
        }
        for label, state in cases.items():
            with self.subTest(label):
                task_id = await self.evaluating(
                    [WorkingSetEntry(self.repository_id, TARGET, BASE_A)]
                )
                if state is not None:
                    await self.meet(task_id, self.repository_id, *state)
                await self.assert_not_completable(task_id)
        for pr_state in (PullRequestState.OPEN, PullRequestState.MERGED):
            with self.subTest(pr_state=pr_state.value):
                task_id = await self.evaluating(
                    [WorkingSetEntry(self.repository_id, TARGET, BASE_A)]
                )
                await self.meet(
                    task_id,
                    self.repository_id,
                    pr=PullRequestInfo(1, "https://example.test/1", pr_state),
                )
                event = await self.service.execute(
                    task_id, C.COMPLETE, actor=self.system
                )
                self.assertEqual(event.to_state, S.COMPLETED)

    async def test_every_target_must_meet_them(self):
        task_id = await self.evaluating(
            [
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, TARGET, BASE_B),
            ]
        )
        await self.meet(task_id, self.repository_id)
        await self.assert_not_completable(task_id)
        await self.meet(task_id, self.other)
        await self.service.execute(task_id, C.COMPLETE, actor=self.system)

    async def test_a_working_repository_that_was_changed_needs_its_evaluation(self):
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, WORKING, BASE_B),
            ]
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.write_to(task_id, self.other)
        await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        await self.meet(task_id, self.repository_id)
        await self.assert_not_completable(task_id)
        # no pull request and no review needed: it is not a target
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.other,
            review=ReviewState(ReviewStatus.NOT_STARTED, EvaluationResult.PASSED),
        )
        await self.service.execute(task_id, C.COMPLETE, actor=self.system)

    async def test_untouched_working_and_referenced_repositories_need_nothing(self):
        """Untouched as the backend verifies it in the repository (clean, HEAD at
        its starting commit, not pushed, no pull request)."""
        task_id = await self.evaluating(
            [
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, WORKING, BASE_B),
                WorkingSetEntry(uuid.uuid4(), REFERENCED, None),
            ]
        )
        self.inspector.clean_at(self.other, BASE_B)
        await self.meet(task_id, self.repository_id)
        await self.service.execute(task_id, C.COMPLETE, actor=self.system)
        # Only the working one was inspected: a referenced one cannot be changed.
        self.assertEqual([call[2] for call in self.inspector.calls], [self.other])

    async def test_a_working_repository_that_cannot_be_verified_counts_as_changed(
        self,
    ):
        """Decision 0030, section 5: what the backend cannot tell is a change
        (fail-closed). An agent may have changed it with a command that the
        broker did not see as a write."""
        cases = {
            "the inspector cannot tell": None,
            "a dirty worktree": RepositoryChangeState(False, BASE_B, False, False),
            "HEAD moved": RepositoryChangeState(True, "e" * 40, False, False),
            "the branch was pushed": RepositoryChangeState(True, BASE_B, True, False),
        }
        for label, found in cases.items():
            with self.subTest(label):
                task_id = await self.evaluating(
                    [
                        WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                        WorkingSetEntry(self.other, WORKING, BASE_B),
                    ]
                )
                self.inspector.states[self.other] = found
                await self.meet(task_id, self.repository_id)
                await self.assert_not_completable(task_id)
                await self.service.update_attempt(
                    task_id,
                    run=FIRST_RUN,
                    repository_id=self.other,
                    review=ReviewState(
                        ReviewStatus.NOT_STARTED, EvaluationResult.PASSED
                    ),
                )
                await self.service.execute(task_id, C.COMPLETE, actor=self.system)

    async def test_an_execution_in_a_working_repository_needs_its_evaluation(self):
        """The review's scenario: a command ran in ``working`` W, the worker reported
        nothing for W; Complete still judges W."""
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, WORKING, BASE_B),
            ]
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.admit_repository_use(
            task_id,
            FIRST_RUN,
            [self.other],
            capability=Capability.PROJECT_TASK_RUN,
            executes=True,
        )
        # Even an inspector that finds it clean does not undo a recorded change.
        self.inspector.clean_at(self.other, BASE_B)
        await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        await self.meet(task_id, self.repository_id)
        await self.assert_not_completable(task_id)

    async def test_a_changed_repository_keeps_its_obligations(self):
        """A target that was written to cannot shed its pull request by a downgrade:
        the downgrade is refused until the change is discarded, so Complete still
        needs it."""
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, TARGET, BASE_B),
            ]
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.write_to(task_id, self.other)
        self.inspector.states[self.other] = RepositoryChangeState(
            False, "c" * 40, True, False
        )
        with self.assertRaises(ModifiedRepositoryDowngradeRefusedError):
            await self.change(task_id, Op.DOWNGRADE_TO_REFERENCED, self.other, TARGET)
        await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        await self.meet(task_id, self.repository_id)
        await self.assert_not_completable(task_id)

    async def test_a_recorded_head_that_moved_counts_as_a_change(self):
        task_id = await self.evaluating(
            [
                WorkingSetEntry(self.repository_id, TARGET, BASE_A),
                WorkingSetEntry(self.other, WORKING, BASE_B),
            ]
        )
        await self.meet(task_id, self.repository_id)
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.other,
            worktree=WorktreeState("agent/b", "/srv/w/b", "d" * 40),
        )
        await self.assert_not_completable(task_id)


@requires_postgres
class ToolExecutionTest(WorkingSetTestCase):
    """A Working Set tool, from the broker's decision to the stored change."""

    NEW = uuid.UUID(int=905)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.task_id = await self.task_in_state(S.RUNNING)
        self.registrations = Registrations(RepoAcl.inherit(self.NEW, P1))
        self.executor = WorkingSetExecutor(self.service)
        self.h = Harness(
            registry=ToolRegistry([*sample_specs(), *WORKING_SET_TOOL_SPECS]),
            registrations=self.registrations,
            executor=self.executor,
            directory=StaticDirectory(
                principal(SystemRole.USER, self.user_id, {P1: ProjectRole.CONTRIBUTOR})
            ),
        )

    async def scope_now(self):
        """The task scope as the backend builds it: roles from the stored Working
        Set (the repositories' entries as ``RepositoryService.scope_entries``)."""
        snapshot = await self.service.restore(self.task_id)
        entries = [
            ScopedRepository(
                self.repository_id,
                P1,
                f"{ROOT}/main",
                RepoAcl.inherit(self.repository_id, P1),
            ),
            ScopedRepository(
                self.NEW, P1, f"{ROOT}/new", RepoAcl.inherit(self.NEW, P1)
            ),
        ]
        return make_scope(
            repositories=with_working_set_roles(entries, snapshot.working_set)
        )

    async def context(self):
        return make_context(
            task_id=self.task_id,
            scope=await self.scope_now(),
            grant=make_grant(
                Capability.PROJECT_READ,
                Capability.PROJECT_REPO_WRITE,
                Capability.PROJECT_TASK_RUN,
                Capability.PROJECT_TASK_WORKING_SET_MANAGE,
            ),
            delegator_id=self.user_id,
        )

    async def test_an_allowed_addition_is_stored_and_changes_the_next_scope(self):
        runner = ToolRunner(self.h.broker, self.executor)
        context = await self.context()
        # Before: the new repository is in no Working Set, so nothing touches it.
        write_new = {"path": f"{ROOT}/new/x", "content": "1"}
        denied = await self.h.broker.request(
            make_call("repo.write_file", write_new, context=context)
        )
        self.assertEqual(denied.reason.value, "repository_role_unresolved")

        outcome = await runner.run(
            make_call(
                "task.working_set.add_referenced",
                {"repository": str(self.NEW)},
                context=context,
            )
        )
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        self.assertEqual(
            outcome.result, {"repository": str(self.NEW), "role": "referenced"}
        )
        snapshot = await self.service.restore(self.task_id)
        entry = snapshot.repository(self.NEW)
        self.assertEqual(
            (entry.role, entry.added_by), (REFERENCED, Actor.user(self.user_id))
        )
        event = snapshot.last_event
        self.assertEqual(event.detail["agent_id"], str(context.grant.agent_id))

        # The next scope carries the role: a read now works, a write does not.
        context = await self.context()
        read = await self.h.broker.request(
            make_call("repo.read_file", {"path": f"{ROOT}/new/x"}, context=context)
        )
        self.assertTrue(read.allowed)
        write = await self.h.broker.request(
            make_call("repo.write_file", write_new, context=context)
        )
        self.assertEqual(write.reason.value, "repository_role_insufficient")

    async def test_a_scope_built_before_a_downgrade_does_not_lift_the_role(self):
        """The Claude review's scenario on the real service: the worker keeps a
        context in which NEW is ``working``; NEW is downgraded to ``referenced``
        (its change verifiably discarded); the broker refuses a write and an
        execution on NEW because the stored role decides."""
        await self.change(
            self.task_id, Op.SET_WORKING, self.NEW, None, starting_commit=BASE_B
        )
        stale = await self.context()
        self.inspector.clean_at(self.NEW, BASE_B)
        await self.change(self.task_id, Op.DOWNGRADE_TO_REFERENCED, self.NEW, WORKING)
        h = Harness(
            registry=ToolRegistry(
                [*sample_specs(), run_in_repository(), *WORKING_SET_TOOL_SPECS]
            ),
            registrations=self.registrations,
            use_gate=self.service,
            directory=self.h.directory,
        )
        for tool, arguments in (
            ("repo.write_file", {"path": f"{ROOT}/new/x", "content": "1"}),
            ("tests.run_in", {"path": f"{ROOT}/new"}),
        ):
            with self.subTest(tool=tool):
                decision = await h.broker.request(
                    make_call(tool, arguments, context=stale)
                )
                self.assertEqual(decision.reason.value, "repository_role_insufficient")
        read = await h.broker.request(
            make_call("repo.read_file", {"path": f"{ROOT}/new/x"}, context=stale)
        )
        self.assertTrue(read.allowed)

    async def test_a_stale_decision_does_not_change_the_working_set(self):
        # The scope still shows the repository as absent, but it joined meanwhile.
        context = await self.context()
        await self.change(self.task_id, Op.ADD_REFERENCED, self.NEW, None)
        invocation_decision = await self.h.broker.request(
            make_call(
                "task.working_set.add_referenced",
                {"repository": str(self.NEW)},
                context=context,
            )
        )
        self.assertTrue(invocation_decision.allowed)  # decided on the stale scope
        with self.assertRaises(WorkingSetConflictError):
            await self.executor.execute(invocation_decision.invocation)
