"""Every argument of every public ``TaskService`` method is validated up front.

A wrong type or value must raise the typed ``InvalidCommandArgumentError``
BEFORE the database is used: never an ``AttributeError`` / ``TypeError`` (a value
the code tried to use), a SQLAlchemy ``StatementError`` or a ``DBAPIError`` (a
value that only failed when it was flushed), and never a silent success (a falsy
``invocation_id`` that was replaced by a new one). The error message names the
rule, never the value.

The tables below list, for each method and each argument, the values that are
wrong (``None`` where the argument is required, the wrong type, ``bool`` where an
``int`` is meant, ``bytes``, empty text, an unknown enum value, a bare
``object()``, out-of-range numbers, ...). Each one is passed with every OTHER
argument valid, and the test asserts

* the typed error and no echo of the value,
* not a single SQL statement was sent and no database session was opened, and
* every table of the task lifecycle is exactly as it was (so nothing was written).

``test_every_baseline_call_is_valid`` runs each baseline (all arguments valid) so
that a bad baseline cannot make a case pass for the wrong reason.

Needs ``PAW_TEST_DATABASE_URL`` (skipped otherwise), except for the domain tests
at the end, which run everywhere.
"""

import enum
import inspect
import types
import unittest
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest import mock

from sqlalchemy import text

from paw_backend.tasks import (
    Actor,
    ActorKind,
    EvaluationResult,
    IllegalTransitionError,
    InvalidCommandArgumentError,
    LogLevel,
    PullRequestInfo,
    PullRequestState,
    ReviewState,
    ReviewStatus,
    StepInfo,
    StepStatus,
    TaskCommand,
    TaskConflictError,
    TaskRun,
    TaskState,
    TaskStepError,
    ToolInvocationInfo,
    ToolInvocationStatus,
    WaitReason,
    WorktreeState,
    plan_transition,
)
from paw_backend.tasks.service import (
    MAX_LOG_MESSAGE_LENGTH,
    MAX_NAME_LENGTH,
    MAX_REASON_LENGTH,
    MAX_TITLE_LENGTH,
)

from .task_support import FIRST_RUN, PostgresTaskTestCase, requires_postgres

C = TaskCommand
S = TaskState

# A marker inside the wrong values: it must never come back in an error message.
CANARY = "CANARY-5b1f7c"

# The tables the task lifecycle writes and the column that orders each of them.
TABLES = (
    ("tasks", "id"),
    ("task_attempts", "id"),
    ("task_steps", "id"),
    ("task_tool_invocations", "id"),
    ("task_logs", "seq"),
    ("task_events", "seq"),
)


class TextSubclass(str):
    """A ``str`` whose methods could say anything: refused like any other non-text."""


# -- the wrong values ------------------------------------------------------------


def not_a_uuid(*, allow_none: bool = False) -> list[Any]:
    real = uuid.uuid4()
    values: list[Any] = [
        CANARY,
        "bogus",
        "",
        str(real),  # a UUID as text is refused: ids are uuid.UUID objects
        real.hex,
        real.bytes,
        real.int,
        0,
        True,
        1.5,
        object(),
        [real],
        (real,),
        {"id": real},
    ]
    return [*values, None] if not allow_none else values


def not_a_bigint_id() -> list[Any]:
    """A step id is an integer from 1 to 2**63 - 1 (``BIGINT`` identity)."""
    return [
        None,
        CANARY,
        "1",
        "",
        b"1",
        True,
        False,
        1.0,
        object(),
        [1],
        0,
        -1,
        2**63,
        2**64,
    ]


def not_an_integer(low: int, high: int, *, allow_none: bool = False) -> list[Any]:
    values: list[Any] = [
        CANARY,
        "1",
        "",
        b"1",
        True,
        False,
        1.0,
        object(),
        [1],
        low - 1,
        high + 1,
        2**63,
    ]
    return values if allow_none else [None, *values]


def not_text(limit: int, *, allow_none: bool = False) -> list[Any]:
    """Wrong text: not ``str``, too long, blank, or unstorable."""
    values: list[Any] = [
        1,
        True,
        b"name",
        bytearray(b"name"),
        object(),
        ["name"],
        TextSubclass("name"),
        "x" * (limit + 1),
        CANARY + "\x00",  # NUL: PostgreSQL text cannot hold it
        CANARY + "\ud800",  # a lone surrogate: not valid Unicode
    ]
    values += ["", " ", "\t\n", "\u3000"]  # blank
    return values if allow_none else [*values, None]


def lookalike(enum_class: type[enum.StrEnum]) -> Any:
    """A member of ANOTHER enum with the serialised value of ``enum_class``'s first."""
    other = enum.StrEnum("Lookalike", {"FIRST": next(iter(enum_class)).value})
    return other.FIRST


def not_an_enum(enum_class: type[enum.StrEnum], *, allow_none: bool = False) -> list:
    first = next(iter(enum_class))
    values: list[Any] = [
        CANARY,
        "bogus",
        "",
        first.name.upper() + "X",
        1,
        0,
        True,
        first.value.encode(),  # bytes
        object(),
        [first.value],
        {},
        TextSubclass(first.value),
        lookalike(enum_class),
    ]
    if first.name != first.value:  # the member's NAME is not its value
        values.append(first.name)
    return values if allow_none else [None, *values]


def not_a_finishing_status(enum_class: type[enum.StrEnum], unfinished: str) -> list:
    """The unknown values, and the one valid status that does not finish anything."""
    return [*not_an_enum(enum_class), enum_class(unfinished), unfinished]


def not_a_run() -> list[Any]:
    return [
        None,
        1,
        0,
        True,
        "1",
        "",
        CANARY,
        b"",
        (1, 0),
        [1, 0],
        {"attempt": 1, "retry_count": 0},
        object(),
        types.SimpleNamespace(attempt=1, retry_count=0),  # looks like a run
    ]


def not_an_actor() -> list[Any]:
    user_id = uuid.uuid4()
    return [
        None,
        CANARY,
        "user",
        "",
        b"user",
        1,
        True,
        object(),
        user_id,
        ActorKind.USER,
        (ActorKind.USER, user_id),
        {"kind": "user", "id": user_id},
        types.SimpleNamespace(kind=ActorKind.SYSTEM, id=None),  # looks like an actor
    ]


def not_a_json_object() -> list[Any]:
    return [
        CANARY,
        "",
        b"{}",
        0,
        True,
        [],
        (),
        object(),
        {1: "x"},
        {"a": object()},
        {"a": (1, 2)},
        {"a": {1, 2}},
        {"a": float("nan")},
        {"a": CANARY + "\x00"},
    ]


def not_a_worktree() -> list[Any]:
    """Not a ``WorktreeState``, and ones whose fields cannot be stored."""
    groups: list[Any] = [
        CANARY,
        "",
        1,
        True,
        b"",
        object(),
        (None, None, None),
        {"branch": "main"},
        types.SimpleNamespace(branch="b", path="/p", head_commit="c"),
    ]
    for field, limit in (("branch", 255), ("path", 1024), ("head_commit", 64)):
        groups += [
            WorktreeState(**{field: value})
            for value in not_text(limit, allow_none=True)
        ]
    return groups


def not_a_review() -> list[Any]:
    groups: list[Any] = [
        CANARY,
        "",
        1,
        True,
        b"",
        object(),
        (ReviewStatus.APPROVED, EvaluationResult.PASSED),
        {"review_status": "approved"},
        types.SimpleNamespace(review_status="approved", evaluation_result="passed"),
    ]
    groups += [ReviewState(review_status=v) for v in not_an_enum(ReviewStatus)]
    groups += [ReviewState(evaluation_result=v) for v in not_an_enum(EvaluationResult)]
    return groups


def not_a_pull_request() -> list[Any]:
    url = "https://example.test/pr/1"
    open_ = PullRequestState.OPEN
    groups: list[Any] = [
        CANARY,
        "",
        1,
        True,
        b"",
        object(),
        (1, url, open_),
        {"number": 1, "url": url, "state": "open"},
        types.SimpleNamespace(number=1, url=url, state=open_),
    ]
    for number in (None, "1", True, False, 1.0, 0, -1, 2**31, 2**63, object()):
        groups.append(PullRequestInfo(number, url, open_))
    groups += [
        PullRequestInfo(1, value, open_) for value in not_text(2048, allow_none=False)
    ]
    groups += [
        PullRequestInfo(1, url, value) for value in not_an_enum(PullRequestState)
    ]
    return groups


# -- the fixture and the baselines --------------------------------------------------


@dataclass
class Fixture:
    """Tasks in the states the methods below need (all of them belong to one test)."""

    running: uuid.UUID  # running, with a running step and a started tool call
    step: StepInfo
    call: ToolInvocationInfo
    idle: uuid.UUID  # running, no step yet
    failed: uuid.UUID
    user: Actor


Baseline = Callable[[Fixture], dict[str, Any]]


def create_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(
        project_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        title="Fix the parser",
        input={"k": ["v", 1, 2.5, True, None]},
        starting_commit="abc123",
        agent="codex",
        model="gpt",
    )


def wait_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(
        task_id=fx.running,
        command=C.WAIT,
        actor=fx.user,
        expected_version=None,
        reason="needs a decision",
        wait_reason=WaitReason.USER,
        agent=None,
        model=None,
    )


def retry_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(
        task_id=fx.failed,
        command=C.RETRY,
        actor=fx.user,
        expected_version=None,
        reason="again",
        wait_reason=None,
        agent="claude",
        model="opus",
    )


def begin_step_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(task_id=fx.idle, name="build", run=FIRST_RUN)


def finish_step_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(task_id=fx.running, step_id=fx.step.id, status=StepStatus.SUCCEEDED)


def begin_tool_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(
        task_id=fx.running,
        step_id=fx.step.id,
        tool_name="git",
        invocation_id=uuid.uuid4(),
    )


def finish_tool_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(
        task_id=fx.running,
        invocation_id=fx.call.id,
        status=ToolInvocationStatus.SUCCEEDED,
    )


def add_log_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(task_id=fx.running, message="line", run=FIRST_RUN, level=LogLevel.INFO)


def update_attempt_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(
        task_id=fx.running,
        run=FIRST_RUN,
        worktree=WorktreeState("agent/x", "/srv/worktrees/x", "b" * 40),
        review=ReviewState(ReviewStatus.APPROVED, EvaluationResult.PASSED),
        pull_request=PullRequestInfo(
            1, "https://example.test/pr/1", PullRequestState.OPEN
        ),
    )


def restore_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(task_id=fx.running, log_limit=10)


def history_baseline(fx: Fixture) -> dict[str, Any]:
    return dict(task_id=fx.running, after_seq=0, limit=10)


BASELINES: dict[str, tuple[str, Baseline]] = {
    "create_task": ("create_task", create_baseline),
    "execute (wait)": ("execute", wait_baseline),
    "execute (retry)": ("execute", retry_baseline),
    "begin_step": ("begin_step", begin_step_baseline),
    "finish_step": ("finish_step", finish_step_baseline),
    "begin_tool_invocation": ("begin_tool_invocation", begin_tool_baseline),
    "finish_tool_invocation": ("finish_tool_invocation", finish_tool_baseline),
    "add_log": ("add_log", add_log_baseline),
    "update_attempt": ("update_attempt", update_attempt_baseline),
    "restore": ("restore", restore_baseline),
    "history": ("history", history_baseline),
}


@dataclass(frozen=True)
class Case:
    """One argument of one method, and the wrong values to pass for it."""

    baseline: str  # a key of BASELINES
    argument: str
    bad: Callable[[Fixture], list[Any]]


def case(baseline: str, argument: str, bad) -> Case:
    """``bad`` is a list, or a function of the fixture (for ids that come from it)."""
    return Case(baseline, argument, bad if callable(bad) else lambda fx: list(bad))


CASES = [
    # -- create_task
    case("create_task", "project_id", not_a_uuid(allow_none=False)),
    case("create_task", "created_by", not_a_uuid(allow_none=False)),
    case("create_task", "title", not_text(MAX_TITLE_LENGTH)),
    case("create_task", "input", not_a_json_object()),
    case("create_task", "starting_commit", not_text(64, allow_none=True)),
    case("create_task", "agent", not_text(MAX_NAME_LENGTH, allow_none=True)),
    case("create_task", "model", not_text(MAX_NAME_LENGTH, allow_none=True)),
    # -- execute
    case("execute (wait)", "task_id", not_a_uuid()),
    case("execute (wait)", "command", not_an_enum(TaskCommand)),
    case("execute (wait)", "actor", not_an_actor()),
    case(
        "execute (wait)",
        "expected_version",
        not_an_integer(1, 2**31 - 1, allow_none=True),
    ),
    case("execute (wait)", "reason", not_text(MAX_REASON_LENGTH, allow_none=True)),
    case("execute (wait)", "wait_reason", not_an_enum(WaitReason, allow_none=True)),
    case("execute (retry)", "agent", not_text(MAX_NAME_LENGTH, allow_none=True)),
    case("execute (retry)", "model", not_text(MAX_NAME_LENGTH, allow_none=True)),
    # -- begin_step
    case("begin_step", "task_id", not_a_uuid()),
    case("begin_step", "name", not_text(MAX_NAME_LENGTH)),
    case("begin_step", "run", not_a_run()),
    # -- finish_step
    case("finish_step", "task_id", not_a_uuid()),
    case(
        "finish_step",
        "step_id",
        lambda fx: [*not_a_bigint_id(), str(fx.step.id), float(fx.step.id)],
    ),
    case(
        "finish_step",
        "status",
        not_a_finishing_status(StepStatus, StepStatus.RUNNING.value),
    ),
    # -- begin_tool_invocation
    case("begin_tool_invocation", "task_id", not_a_uuid()),
    case(
        "begin_tool_invocation",
        "step_id",
        lambda fx: [*not_a_bigint_id(), str(fx.step.id), float(fx.step.id)],
    ),
    case("begin_tool_invocation", "tool_name", not_text(MAX_NAME_LENGTH)),
    # ``invocation_id`` is optional, and a falsy value used to be swapped for a new id.
    case("begin_tool_invocation", "invocation_id", not_a_uuid(allow_none=True)),
    # -- finish_tool_invocation
    case("finish_tool_invocation", "task_id", not_a_uuid()),
    case("finish_tool_invocation", "invocation_id", not_a_uuid()),
    case(
        "finish_tool_invocation",
        "status",
        not_a_finishing_status(
            ToolInvocationStatus, ToolInvocationStatus.STARTED.value
        ),
    ),
    # -- add_log (a blank line is a legitimate log line, so only non-text is refused)
    case("add_log", "task_id", not_a_uuid()),
    case(
        "add_log",
        "message",
        [
            None,
            1,
            True,
            b"line",
            object(),
            ["line"],
            TextSubclass("line"),
            CANARY + "\x00",
            CANARY + "\ud800",
            "a" * (MAX_LOG_MESSAGE_LENGTH + 1) + "\x00",  # NUL in the cut-off part
        ],
    ),
    case("add_log", "run", not_a_run()),
    case("add_log", "level", not_an_enum(LogLevel)),
    # -- update_attempt
    case("update_attempt", "task_id", not_a_uuid()),
    case("update_attempt", "run", not_a_run()),
    case("update_attempt", "worktree", not_a_worktree()),
    case("update_attempt", "review", not_a_review()),
    case("update_attempt", "pull_request", not_a_pull_request()),
    # -- restore
    case("restore", "task_id", not_a_uuid()),
    case("restore", "log_limit", not_an_integer(0, 1000)),
    # -- history
    case("history", "task_id", not_a_uuid()),
    case("history", "after_seq", not_an_integer(0, 2**63 - 1)),
    case("history", "limit", not_an_integer(1, 5000)),
]

# Every public method of ``TaskService`` must be covered by at least one case.
PUBLIC_METHODS = {
    "create_task",
    "execute",
    "begin_step",
    "finish_step",
    "begin_tool_invocation",
    "finish_tool_invocation",
    "add_log",
    "update_attempt",
    "restore",
    "history",
}


def describe(value: object) -> str:
    return f"{type(value).__name__}:{value!r:.60}"


@requires_postgres
class ArgumentValidationTest(PostgresTaskTestCase):
    """The tables above, one subtest per (method, argument, wrong value)."""

    async def make_fixture(self) -> Fixture:
        running = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(running, "build", run=FIRST_RUN)
        call = await self.service.begin_tool_invocation(
            running, step_id=step.id, tool_name="git"
        )
        return Fixture(
            running=running,
            step=step,
            call=call,
            idle=await self.task_in_state(S.RUNNING),
            failed=await self.task_in_state(S.FAILED),
            user=self.user,
        )

    async def snapshot(self) -> dict[str, tuple[int, str]]:
        """The row count and a digest of every row of every table the service writes."""
        result = {}
        async with self.database.engine.connect() as connection:
            for table, key in TABLES:
                row = await connection.execute(
                    text(
                        "SELECT count(*), md5(coalesce("
                        f"string_agg(t::text, ',' ORDER BY t.{key}), '')) "
                        f"FROM {table} t"
                    )
                )
                result[table] = tuple(row.one())
        return result

    async def call(self, method: str, arguments: dict[str, Any]) -> Any:
        return await getattr(self.service, method)(**arguments)

    def no_database(self):
        """Opening a session (using the database) fails with an AssertionError."""
        return mock.patch.object(
            self.database, "session", side_effect=AssertionError("a session was opened")
        )

    async def test_every_public_method_has_cases(self):
        public = {
            name
            for name in vars(type(self.service))
            if not name.startswith("_") and callable(getattr(self.service, name))
        }
        self.assertEqual(public, PUBLIC_METHODS)
        self.assertEqual({BASELINES[c.baseline][0] for c in CASES}, PUBLIC_METHODS)

    async def test_every_argument_of_every_method_has_cases(self):
        """Each parameter of each public method appears in the tables."""
        covered: dict[str, set[str]] = {}
        for c in CASES:
            method = BASELINES[c.baseline][0]
            covered.setdefault(method, set()).add(c.argument)
        for method in PUBLIC_METHODS:
            parameters = set(
                inspect.signature(getattr(self.service, method)).parameters
            )
            with self.subTest(method=method):
                self.assertEqual(covered[method], parameters)

    async def test_every_baseline_call_is_valid(self):
        """All arguments valid: the call works. Otherwise a case proves nothing."""
        for label, (method, baseline) in BASELINES.items():
            with self.subTest(baseline=label):
                fixture = await self.make_fixture()
                await self.call(method, baseline(fixture))

    async def test_a_wrong_value_is_refused_before_the_database_is_used(self):
        checked = 0
        for c in CASES:
            method, baseline = BASELINES[c.baseline]
            fixture = await self.make_fixture()
            for value in c.bad(fixture):
                checked += 1
                arguments = {**baseline(fixture), c.argument: value}
                with self.subTest(f"{c.baseline}: {c.argument}={describe(value)}"):
                    before = await self.snapshot()
                    with (
                        self.no_database(),
                        self.captured_statements() as statements,
                        self.assertRaises(InvalidCommandArgumentError) as caught,
                    ):
                        await self.call(method, arguments)
                    error = caught.exception
                    # The typed error itself, not a subclass or a lookalike.
                    self.assertIs(type(error), InvalidCommandArgumentError)
                    self.assertEqual(statements, [])
                    self.assertEqual(await self.snapshot(), before)
                    # The rule is stated; the value never is.
                    message = str(error)
                    self.assertTrue(message)
                    # (Short texts are skipped: "running" is a valid value that the
                    # message may list; the marker and long values prove no echo.)
                    self.assertNotIn(CANARY, message)
                    for needle in (repr(value), str(value)):
                        if len(needle) >= 20:
                            self.assertNotIn(needle, message)
        # A guard against an emptied table: (method, argument, value) triples.
        self.assertGreater(checked, 500)

    async def test_a_wait_reason_that_does_not_fit_the_command_is_refused(self):
        """Wait needs a reason and every other command refuses one.

        This is judged with the task's state (an illegal transition is reported
        first, see ``plan_transition``), so it may read the task; it still raises
        the typed error and writes nothing.
        """
        fixture = await self.make_fixture()
        for command, wait_reason in (
            (C.WAIT, None),
            (C.PAUSE, WaitReason.USER),
            (C.PAUSE, "user"),
        ):
            with self.subTest(command=command.value, wait_reason=wait_reason):
                before = await self.snapshot()
                with self.assertRaises(InvalidCommandArgumentError) as caught:
                    await self.service.execute(
                        fixture.running,
                        command,
                        actor=self.user,
                        wait_reason=wait_reason,
                    )
                self.assertIs(type(caught.exception), InvalidCommandArgumentError)
                self.assertEqual(await self.snapshot(), before)

    async def test_the_command_create_is_a_command_but_never_legal(self):
        fixture = await self.make_fixture()
        before = await self.snapshot()
        with self.assertRaises(IllegalTransitionError):
            await self.service.execute(fixture.running, C.CREATE, actor=self.user)
        self.assertEqual(await self.snapshot(), before)

    async def test_serialised_enum_values_are_accepted_and_normalised_to_members(self):
        """A member or its serialised value: both work, and the member comes back."""
        fixture = await self.make_fixture()
        service = self.service

        event = await service.execute(
            fixture.running, "wait", actor=self.user, wait_reason="approval"
        )
        self.assertIs(event.command, C.WAIT)
        self.assertIs(event.wait_reason, WaitReason.APPROVAL)
        snapshot = await service.restore(fixture.running)
        self.assertIs(snapshot.wait_reason, WaitReason.APPROVAL)
        self.assertIs(snapshot.state, S.WAITING)

        # The tool call first: finishing its step would interrupt it.
        call = await service.finish_tool_invocation(
            fixture.running, fixture.call.id, "interrupted"
        )
        self.assertIs(call.status, ToolInvocationStatus.INTERRUPTED)
        step = await service.finish_step(fixture.running, fixture.step.id, "failed")
        self.assertIs(step.status, StepStatus.FAILED)

        entry = await service.add_log(
            fixture.running, "line", run=FIRST_RUN, level="warning"
        )
        self.assertIs(entry.level, LogLevel.WARNING)

        attempt = await service.update_attempt(
            fixture.running,
            run=FIRST_RUN,
            review=ReviewState("changes_requested", "failed"),
            pull_request=PullRequestInfo(3, "https://example.test/pr/3", "merged"),
        )
        self.assertIs(attempt.review.review_status, ReviewStatus.CHANGES_REQUESTED)
        self.assertIs(attempt.review.evaluation_result, EvaluationResult.FAILED)
        self.assertIs(attempt.pull_request.state, PullRequestState.MERGED)

    async def test_omitted_optional_arguments_still_work(self):
        fixture = await self.make_fixture()
        call = await self.service.begin_tool_invocation(
            fixture.running, step_id=fixture.step.id, tool_name="git"
        )
        self.assertIsInstance(call.id, uuid.UUID)
        event = await self.service.create_task(
            project_id=uuid.uuid4(), created_by=uuid.uuid4(), title="Only a title"
        )
        snapshot = await self.service.restore(event.task_id)
        self.assertEqual((snapshot.input, snapshot.state), ({}, S.QUEUED))
        self.assertEqual(len(await self.service.history(event.task_id)), 1)

    async def test_the_largest_valid_numbers_reach_the_database(self):
        """Values at the top of their range are valid: they get the ordinary errors."""
        fixture = await self.make_fixture()
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(
                fixture.running, 2**63 - 1, StepStatus.SUCCEEDED
            )
        self.assertEqual(
            await self.service.history(fixture.running, after_seq=2**63 - 1), []
        )
        with self.assertRaises(TaskConflictError):
            await self.service.execute(
                fixture.running, C.PAUSE, actor=self.user, expected_version=2**31 - 1
            )
        self.assertEqual(
            (await self.service.restore(fixture.running, log_limit=1000)).state,
            S.RUNNING,
        )


class DomainArgumentTest(unittest.TestCase):
    """The domain value objects and the transition planner (no database)."""

    def test_an_actor_of_another_kind_or_with_another_id_is_refused(self):
        for kind, actor_id in (
            (None, None),
            ("bogus", None),
            ("", None),
            (1, None),
            (True, None),
            (b"system", None),
            (object(), None),
            (lookalike(ActorKind), None),
            (TextSubclass("system"), None),
            (ActorKind.USER, "not-a-uuid"),
            (ActorKind.USER, str(uuid.uuid4())),
            (ActorKind.USER, 1),
            (ActorKind.USER, object()),
            (ActorKind.USER, None),
            (ActorKind.SYSTEM, uuid.uuid4()),
            (ActorKind.POLICY, uuid.uuid4()),
            (ActorKind.SYSTEM, "not-a-uuid"),
            ("user", None),
            ("system", uuid.uuid4()),
        ):
            with (
                self.subTest(kind=describe(kind), id=describe(actor_id)),
                self.assertRaises(InvalidCommandArgumentError) as caught,
            ):
                Actor(kind, actor_id)
            self.assertIs(type(caught.exception), InvalidCommandArgumentError)
            self.assertNotIn(CANARY, str(caught.exception))

    def test_an_actor_kind_may_be_given_as_its_serialised_value(self):
        user_id = uuid.uuid4()
        for kind, actor_id in (("user", user_id), ("system", None), ("policy", None)):
            with self.subTest(kind=kind):
                actor = Actor(kind, actor_id)
                self.assertIs(type(actor.kind), ActorKind)
                self.assertEqual(actor, Actor(ActorKind(kind), actor_id))

    def test_plan_transition_checks_the_wait_reason_itself(self):
        for bad in not_an_enum(WaitReason):
            if bad is None:
                continue
            with (
                self.subTest(wait_reason=describe(bad)),
                self.assertRaises(InvalidCommandArgumentError) as caught,
            ):
                plan_transition(S.RUNNING, C.WAIT, wait_reason=bad)
            self.assertNotIn(CANARY, str(caught.exception))
        plan = plan_transition(S.RUNNING, C.WAIT, wait_reason="resource")
        self.assertIs(plan.wait_reason, WaitReason.RESOURCE)

    def test_run_counters_are_still_checked_at_construction(self):
        for attempt, retry_count in ((0, 0), (1, -1), (True, 0), (1, False), ("1", 0)):
            with (
                self.subTest(attempt=attempt, retry_count=retry_count),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                TaskRun(attempt, retry_count)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
