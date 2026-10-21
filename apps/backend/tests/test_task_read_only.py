"""Read-only tasks (issue #279, Decision 0094, 1 A): the lifecycle and the
Working Set of a task created with ``read_only=True``.

The first class needs no server (the transition table); the PostgreSQL class
(skipped unless ``PAW_TEST_DATABASE_URL`` is set) creates, starts and
completes real tasks.
"""

import unittest
import uuid

from paw_backend.tasks import (
    IllegalTransitionError,
    InvalidCommandArgumentError,
    RepoRole,
    TaskCommand,
    TaskState,
    WorkingSetEntry,
    WorktreeState,
    allowed_commands,
    plan_transition,
)
from paw_backend.tasks.domain import WorkingSetOperation
from paw_backend.tasks.errors import (
    CompletionRequirementsNotMetError,
    ReadOnlyTaskError,
)

from .task_support import (
    BASELINE,
    FIRST_RUN,
    PostgresTaskTestCase,
    requires_postgres,
    single_target,
)

S = TaskState
C = TaskCommand


def referenced(*repository_ids: uuid.UUID) -> list[WorkingSetEntry]:
    return [
        WorkingSetEntry(repository_id, RepoRole.REFERENCED, BASELINE)
        for repository_id in repository_ids
    ]


class ReadOnlyTransitionTest(unittest.TestCase):
    def test_a_read_only_task_completes_from_running(self):
        plan = plan_transition(S.RUNNING, C.COMPLETE, read_only=True)
        self.assertEqual(plan.target, S.COMPLETED)
        self.assertIn(C.COMPLETE, allowed_commands(S.RUNNING, read_only=True))

    def test_a_read_only_task_is_never_evaluated(self):
        with self.assertRaises(IllegalTransitionError):
            plan_transition(S.RUNNING, C.BEGIN_EVALUATION, read_only=True)
        self.assertNotIn(
            C.BEGIN_EVALUATION, allowed_commands(S.RUNNING, read_only=True)
        )

    def test_any_other_task_is_unchanged(self):
        with self.assertRaises(IllegalTransitionError):
            plan_transition(S.RUNNING, C.COMPLETE)
        self.assertEqual(
            plan_transition(S.RUNNING, C.BEGIN_EVALUATION).target, S.EVALUATING
        )
        self.assertNotIn(C.COMPLETE, allowed_commands(S.RUNNING))

    def test_every_other_pair_is_the_same_for_a_read_only_task(self):
        # Never evaluating, so Complete has no other source.
        changed = {
            (S.RUNNING, C.COMPLETE),
            (S.RUNNING, C.BEGIN_EVALUATION),
            (S.EVALUATING, C.COMPLETE),
        }
        for state in TaskState:
            for command in TaskCommand:
                if (state, command) in changed or command is C.WAIT:
                    continue
                with self.subTest(state=state, command=command):
                    try:
                        normal = plan_transition(state, command).target
                    except IllegalTransitionError:
                        normal = None
                    try:
                        read_only = plan_transition(
                            state, command, read_only=True
                        ).target
                    except IllegalTransitionError:
                        read_only = None
                    self.assertEqual(normal, read_only)

    def test_the_flag_must_be_a_bool(self):
        with self.assertRaises(InvalidCommandArgumentError):
            plan_transition(S.RUNNING, C.COMPLETE, read_only=1)


@requires_postgres
class ReadOnlyTaskServiceTest(PostgresTaskTestCase):
    async def read_only_task(self, **overrides) -> uuid.UUID:
        arguments = {"repositories": referenced(self.repository_id), "read_only": True}
        arguments.update(overrides)
        return await self.create_task(**arguments)

    async def test_the_flag_is_stored_and_restored(self):
        task_id = await self.read_only_task()
        snapshot = await self.service.restore(task_id, log_limit=0)
        self.assertTrue(snapshot.read_only)
        (event,) = await self.service.history(task_id)
        self.assertEqual(event.detail["read_only"], True)
        other = await self.create_task()
        self.assertFalse((await self.service.restore(other, log_limit=0)).read_only)
        (event,) = await self.service.history(other)
        self.assertNotIn("read_only", event.detail)

    async def test_it_starts_with_referenced_repositories_only(self):
        task_id = await self.read_only_task()
        event = await self.service.execute(task_id, C.START, actor=self.system)
        self.assertEqual(event.to_state, S.RUNNING)

    async def test_it_takes_referenced_repositories_only(self):
        for role in (RepoRole.TARGET, RepoRole.WORKING):
            with self.subTest(role=role):
                with self.assertRaises(InvalidCommandArgumentError):
                    await self.read_only_task(
                        repositories=[
                            *referenced(uuid.uuid4()),
                            WorkingSetEntry(self.repository_id, role, BASELINE),
                        ]
                    )
        with self.assertRaises(InvalidCommandArgumentError):
            await self.read_only_task(repositories=[])

    async def test_the_flag_must_be_a_bool(self):
        for value in (1, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(InvalidCommandArgumentError):
                    await self.read_only_task(read_only=value)

    async def test_it_completes_from_running_with_its_result(self):
        task_id = await self.read_only_task()
        await self.service.execute(task_id, C.START, actor=self.system)
        result = {"nodes": [{"key": "read", "summary": "The parser is LL(1)."}]}

        event = await self.service.execute(
            task_id, C.COMPLETE, actor=self.system, result=result
        )

        self.assertEqual((event.from_state, event.to_state), (S.RUNNING, S.COMPLETED))
        self.assertEqual(event.detail, {"result": result})
        snapshot = await self.service.restore(task_id, log_limit=0)
        self.assertEqual(snapshot.state, S.COMPLETED)
        self.assertEqual(snapshot.last_event.detail["result"], result)

    async def test_it_is_never_evaluated(self):
        task_id = await self.read_only_task()
        await self.service.execute(task_id, C.START, actor=self.system)
        with self.assertRaises(IllegalTransitionError):
            await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)

    async def test_a_changed_repository_still_keeps_it_from_completing(self):
        # Nothing may write to a referenced repository; if anything was recorded
        # as changed, the requirements of Decision 0030 (5) still hold.
        task_id = await self.read_only_task()
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            repository_id=self.repository_id,
            worktree=WorktreeState("paw/read", "/srv/worktree", "1" * 40),
        )
        with self.assertRaises(CompletionRequirementsNotMetError):
            await self.service.execute(task_id, C.COMPLETE, actor=self.system)

    async def test_only_complete_takes_a_result_and_it_is_json(self):
        task_id = await self.read_only_task()
        await self.service.execute(task_id, C.START, actor=self.system)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(
                task_id, C.PAUSE, actor=self.system, result={"nodes": []}
            )
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(
                task_id, C.COMPLETE, actor=self.system, result={"x": float("nan")}
            )
        self.assertEqual(
            (await self.service.restore(task_id, log_limit=0)).state, S.RUNNING
        )

    async def test_any_other_task_still_needs_its_target_and_evaluation(self):
        task_id = await self.create_task(repositories=single_target(self.repository_id))
        await self.service.execute(task_id, C.START, actor=self.system)
        with self.assertRaises(IllegalTransitionError):
            await self.service.execute(task_id, C.COMPLETE, actor=self.system)

    async def test_its_working_set_never_gains_a_writable_repository(self):
        task_id = await self.read_only_task()
        other = uuid.uuid4()
        for operation, repository, expected in (
            (WorkingSetOperation.SET_WORKING, self.repository_id, RepoRole.REFERENCED),
            (WorkingSetOperation.SET_TARGET, self.repository_id, RepoRole.REFERENCED),
            (WorkingSetOperation.SET_WORKING, other, None),
            (WorkingSetOperation.SET_TARGET, other, None),
        ):
            with self.subTest(operation=operation, repository=repository):
                with self.assertRaises(ReadOnlyTaskError):
                    await self.service.change_working_set(
                        task_id,
                        operation,
                        repository,
                        actor=self.user,
                        expected_role=expected,
                        starting_commit=BASELINE,
                    )
        # Another repository to read may join it.
        await self.service.change_working_set(
            task_id,
            WorkingSetOperation.ADD_REFERENCED,
            other,
            actor=self.user,
            expected_role=None,
            starting_commit=BASELINE,
        )
        snapshot = await self.service.restore(task_id, log_limit=0)
        self.assertEqual(
            [member.role for member in snapshot.working_set],
            [RepoRole.REFERENCED, RepoRole.REFERENCED],
        )


if __name__ == "__main__":
    unittest.main()
