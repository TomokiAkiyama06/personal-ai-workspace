"""Task commands, steps, logs and history on a real PostgreSQL.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see test_postgres_integration).
"""

import contextlib
import json
import logging
import sys
import tracemalloc
import unittest
import uuid
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from unittest import mock

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.tasks import (
    ActorKind,
    EvaluationResult,
    IllegalTransitionError,
    Interruption,
    InvalidCommandArgumentError,
    LogLevel,
    PullRequestInfo,
    PullRequestState,
    ReviewState,
    ReviewStatus,
    StepStatus,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskRun,
    TaskService,
    TaskState,
    TaskStepError,
    WaitReason,
    WorktreeState,
)
from paw_backend.tasks import service as service_module
from paw_backend.tasks.service import (
    MAX_INPUT_BYTES,
    MAX_INPUT_DEPTH,
    MAX_INPUT_INTEGER_DIGITS,
    MAX_REASON_LENGTH,
)

from .task_support import (
    FIRST_RUN,
    PostgresTaskTestCase,
    command_reason,
    requires_postgres,
)
from .test_task_domain import EXPECTED

C = TaskCommand
S = TaskState
WORKTREE = WorktreeState("agent/task-1", "/srv/worktrees/task-1", "b" * 40)


@contextlib.contextmanager
def int_str_digits_limit(limit: int):
    """Run with the interpreter's integer <-> text digit limit set (0 = none)."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(limit)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


def encoder_must_not_run():
    """Fail loudly if the input is encoded, which the check has to prevent."""
    return mock.patch.object(
        service_module.json,
        "dumps",
        side_effect=AssertionError("json.dumps was reached"),
    )


@requires_postgres
class CreateTaskTest(PostgresTaskTestCase):
    async def test_new_task_is_queued_at_version_one_with_a_create_event(self):
        event = await self.service.create_task(
            project_id=self.project_id,
            created_by=self.user_id,
            title="Fix the parser",
            input={"prompt": "Make it faster", "files": ["a.py"]},
            starting_commit="a" * 40,
            agent="codex",
            model="model-x",
        )
        self.assertEqual(
            (event.command, event.from_state, event.to_state, event.task_version),
            (C.CREATE, None, S.QUEUED, 1),
        )
        self.assertEqual(
            (event.actor.kind, event.actor.id), (ActorKind.USER, self.user_id)
        )

        snapshot = await self.service.restore(event.task_id)
        self.assertEqual(snapshot.state, S.QUEUED)
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.attempt.number, 1)
        self.assertEqual(snapshot.retry_count, 0)
        self.assertEqual(snapshot.title, "Fix the parser")
        self.assertEqual(
            snapshot.input, {"prompt": "Make it faster", "files": ["a.py"]}
        )
        self.assertEqual(snapshot.starting_commit, "a" * 40)
        self.assertEqual((snapshot.agent, snapshot.model), ("codex", "model-x"))
        self.assertEqual(
            (snapshot.project_id, snapshot.created_by), (self.project_id, self.user_id)
        )
        self.assertIsNone(snapshot.current_step)
        self.assertEqual(snapshot.recent_logs, ())
        self.assertEqual(snapshot.attempt.worktree, WorktreeState())
        self.assertEqual(snapshot.attempt.review, ReviewState())
        self.assertIsNone(snapshot.attempt.pull_request)

    async def test_invalid_creation_arguments_are_rejected_without_echoing_them(self):
        secret = "SECRET-MARKER"
        cases = {
            "blank title": {"title": "   "},
            "long title": {"title": secret * 40},
            "input that is not JSON": {"input": {"x": object()}},
            "oversized input": {"input": {"blob": secret * 40000}},
            "long commit": {"starting_commit": "c" * 65},
        }
        for name, overrides in cases.items():
            with (
                self.subTest(name),
                self.assertRaises(InvalidCommandArgumentError) as caught,
            ):
                await self.create_task(**overrides)
            self.assertNotIn(secret, str(caught.exception))

    async def test_title_at_the_limit_is_accepted(self):
        task_id = await self.create_task(title="t" * 200)
        self.assertEqual((await self.service.restore(task_id)).title, "t" * 200)

    async def assert_input_rejected(self, value, *, secret: str = "") -> None:
        """``input`` is refused with the typed error and nothing is written."""
        count = "SELECT count(*) FROM tasks WHERE project_id = :project_id"
        before = await self.scalar(count, project_id=self.project_id)
        with self.assertRaises(InvalidCommandArgumentError) as caught:
            await self.create_task(input=value)
        if secret:
            self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(await self.scalar(count, project_id=self.project_id), before)

    async def test_non_finite_numbers_in_input_are_rejected_before_the_database(
        self,
    ):
        # json.dumps writes these as NaN / Infinity, which PostgreSQL JSONB
        # refuses at flush time (a DataError), so they must be caught earlier.
        cases = {
            "nan": {"x": float("nan")},
            "infinity": {"x": float("inf")},
            "negative infinity": {"x": float("-inf")},
            "in a list": {"x": [1, 2.5, float("nan")]},
            "deeply nested": {"a": [{"b": [{"c": float("-inf")}]}]},
        }
        for name, value in cases.items():
            with self.subTest(name):
                await self.assert_input_rejected(value)

    async def test_text_that_jsonb_cannot_hold_is_rejected_in_keys_and_values(self):
        # NUL and unpaired surrogates fail at flush time too; an emoji written as
        # two surrogate code points is not valid Unicode text either.
        cases = {
            "NUL in a value": {"x": "a\x00b"},
            "NUL in a key": {"a\x00": 1},
            "NUL nested": {"x": [{"y": "\x00"}]},
            "lone surrogate": {"x": "\ud800"},
            "surrogate in a key": {"\udfff": 1},
            "surrogate pair as code points": {"x": chr(0xD83D) + chr(0xDE00)},
        }
        for name, value in cases.items():
            with self.subTest(name):
                await self.assert_input_rejected(value)

    async def test_input_that_is_not_plain_json_is_rejected_without_coercion(self):
        # json.dumps would silently turn these into JSON (keys into strings,
        # tuples into arrays); the input must already be what it will be stored as.
        cases = {
            "integer key": {1: "a"},
            "boolean key": {True: "a"},
            "none key": {None: "a"},
            "tuple key": {(1, 2): "a"},
            "tuple value": {"x": (1, 2)},
            "set": {"x": {1, 2}},
            "bytes": {"x": b"abc"},
            "decimal": {"x": Decimal("1.5")},
            "datetime": {"x": datetime(2026, 1, 1)},
            "arbitrary object": {"x": object()},
            "dict subclass": OrderedDict(x=1),
            "list at the top": [1, 2],
            "text at the top": "abc",
        }
        for name, value in cases.items():
            with self.subTest(name):
                await self.assert_input_rejected(value)

    async def test_finite_json_values_are_stored_and_restored_unchanged(self):
        value = {
            "prompt": '日本語 \U0001f600 \x01 tab\t quote" back\\slash',
            "numbers": [0, -1, 10**30, 1.5, 0.1, 2.0, 1e-300],
            "flags": [True, False, None],
            "nested": {"a": {"b": [[], {}, [[{"c": "d"}]]]}},
            "": "empty key",
        }
        task_id = await self.create_task(input=value)
        restored = (await self.service.restore(task_id)).input
        self.assertEqual(restored, value)
        # Booleans stay booleans and integers stay integers.
        self.assertIs(restored["flags"][0], True)
        self.assertIsInstance(restored["numbers"][2], int)

    async def test_input_nesting_is_limited_and_cycles_are_rejected(self):
        def nested(levels: int) -> dict:
            value: dict = {}
            for _ in range(levels - 1):
                value = {"a": value}
            return value  # ``levels`` objects inside each other

        at_limit = nested(MAX_INPUT_DEPTH)
        task_id = await self.create_task(input=at_limit)
        self.assertEqual((await self.service.restore(task_id)).input, at_limit)

        cyclic_dict: dict = {}
        cyclic_dict["self"] = cyclic_dict
        cyclic_list: list = []
        cyclic_list.append(cyclic_list)
        cases = {
            "one level too deep": nested(MAX_INPUT_DEPTH + 1),
            "far too deep": nested(5000),
            "lists count as levels": {"a": [[[[[[nested(MAX_INPUT_DEPTH)]]]]]]},
            "cyclic dict": cyclic_dict,
            "cyclic list": {"x": cyclic_list},
        }
        for name, value in cases.items():
            with self.subTest(name):
                await self.assert_input_rejected(value)

    async def test_input_size_is_bounded_at_the_limit_and_by_element_count(self):
        overhead = len(json.dumps({"b": ""}))
        at_limit = {"b": "x" * (MAX_INPUT_BYTES - overhead)}
        self.assertEqual(len(json.dumps(at_limit)), MAX_INPUT_BYTES)
        task_id = await self.create_task(input=at_limit)
        self.assertEqual((await self.service.restore(task_id)).input, at_limit)

        secret = "SECRET-MARKER"
        cases = {
            "one byte over": {"b": "x" * (MAX_INPUT_BYTES - overhead + 1)},
            "many small elements": {"a": [1] * MAX_INPUT_BYTES},
            "many small keys": {str(n): 0 for n in range(MAX_INPUT_BYTES // 2)},
            "escaped text counts as encoded": {"b": "é" * (MAX_INPUT_BYTES // 6)},
            "one huge text": {"b": secret * MAX_INPUT_BYTES},
        }
        for name, value in cases.items():
            with self.subTest(name):
                await self.assert_input_rejected(value, secret=secret)

    async def test_a_huge_shared_structure_is_rejected_before_it_is_encoded(self):
        # 24 shallow levels of ``[x, x]`` hold about 10**8 values that share
        # memory; encoding them would need gigabytes, so the check has to give up
        # after a bounded amount of work and never reach the encoder.
        value = [1] * 10
        for _ in range(24):
            value = [value, value]
        with mock.patch.object(service_module.json, "dumps") as dumps:
            await self.assert_input_rejected({"x": value})
        dumps.assert_not_called()

    async def test_a_repeated_large_integer_is_refused_before_it_is_encoded(self):
        # One 4000-digit integer that is referenced 65000 times costs almost no
        # memory, but the encoder writes its 4000 digits every time (about 260 MB).
        # Each occurrence must be charged the length it will be encoded to.
        big = 10**3999
        cases = {
            "one integer, repeated": {"x": [big] * 65000},
            "negative": {"x": [-big] * 65000},
            "the same integer as many values": {
                "x": {str(n): big for n in range(1000)}
            },
            "shared by two nested levels": {"x": [[big] * 300] * 300},
        }
        for name, value in cases.items():
            with self.subTest(name), encoder_must_not_run():
                await self.assert_input_rejected(value)

    async def test_every_number_is_charged_by_its_encoded_length(self):
        # ``{"k...": items}`` is built so that the check's charge is exactly
        # ``MAX_INPUT_BYTES`` (brackets, the key with its quotes, and the encoded
        # length of every item, measured with json.dumps): that still reaches the
        # encoder, which refuses it on the separators. One character more is
        # refused before anything is encoded.
        def charging(items: list, extra: int) -> dict:
            spent = 2 + 2 + sum(len(json.dumps(item)) for item in items)
            return {"k" * (MAX_INPUT_BYTES - spent - 2 + extra): items}

        cases = {
            "large integers": [10**3999, -(10**3999), 10**1234, -(10**99)] * 15,
            "integers around powers of ten": [
                *(0, 9, 10, 99, 100, 999, 1000, -9, -10, -99, -100),
                *(10**18 - 1, 10**18, 2**63, 2**64, -(2**64)),
            ]
            * 100,
            "booleans and null": [True, False, None] * 1000,
            "floats": [0.0, 1.5, -2.5e-300, 1.7976931348623157e308, 5e-324, 1e16] * 500,
        }
        for name, items in cases.items():
            at_limit, one_over = charging(items, 0), charging(items, 1)
            with self.subTest(name):
                with mock.patch.object(
                    service_module.json, "dumps", wraps=json.dumps
                ) as dumps:
                    await self.assert_input_rejected(at_limit)
                self.assertEqual(dumps.call_count, 1)
                with encoder_must_not_run():
                    await self.assert_input_rejected(one_over)

    async def test_large_integers_within_the_limits_are_stored_and_restored(self):
        with int_str_digits_limit(4300):
            value = {
                "many": [10**3999, -(10**3998)] * 30,
                "at the interpreter's digit limit": [10**4299, -(10**4299)],
            }
            task_id = await self.create_task(input=value)
            self.assertEqual((await self.service.restore(task_id)).input, value)

    async def test_integers_beyond_the_digit_limit_are_refused_before_encoding(self):
        # 4300 digits is the interpreter's default limit for turning an integer
        # into text; the encoder would refuse more (a ValueError), so the check
        # states it as the typed error before any encoding.
        with int_str_digits_limit(4300):
            cases = {
                "one digit too many": {"x": 10**4300},
                "negative": {"x": -(10**4300)},
                "in a list in an object": {"x": [1, [{"y": 10**4300}]]},
                "far more than PostgreSQL can hold": {"x": 2**1_000_000},
            }
            for name, value in cases.items():
                with self.subTest(name), encoder_must_not_run():
                    await self.assert_input_rejected(value)

    async def test_the_interpreters_lower_digit_limit_is_the_limit(self):
        with int_str_digits_limit(640):
            value = {"x": [10**639, -(10**639)]}
            task_id = await self.create_task(input=value)
            self.assertEqual((await self.service.restore(task_id)).input, value)
            with self.assertRaises(InvalidCommandArgumentError) as caught:
                await self.create_task(input={"x": 10**640})
            self.assertEqual(
                str(caught.exception), "input integers must have at most 640 digits"
            )

    async def test_integers_are_limited_to_the_digits_postgresql_holds(self):
        # With the interpreter's own limit lifted, PostgreSQL's ``numeric`` is
        # what limits an integer: MAX_INPUT_INTEGER_DIGITS digits are stored and
        # restored, one more is refused before it is encoded.
        with int_str_digits_limit(0):
            for name, number in {
                "positive": 10 ** (MAX_INPUT_INTEGER_DIGITS - 1),
                "negative": -(10 ** (MAX_INPUT_INTEGER_DIGITS - 1)),
            }.items():
                with self.subTest(name):
                    task_id = await self.create_task(input={"x": number})
                    restored = (await self.service.restore(task_id)).input
                    self.assertEqual(restored, {"x": number})
            for name, number in {
                "positive": 10**MAX_INPUT_INTEGER_DIGITS,
                "negative": -(10**MAX_INPUT_INTEGER_DIGITS),
            }.items():
                with self.subTest(f"one digit too many, {name}"):
                    with self.assertRaises(InvalidCommandArgumentError) as caught:
                        with encoder_must_not_run():
                            await self.create_task(input={"x": number})
                    self.assertEqual(
                        str(caught.exception),
                        f"input integers must have at most "
                        f"{MAX_INPUT_INTEGER_DIGITS} digits",
                    )

    async def test_a_huge_integer_is_refused_without_converting_it_to_text(self):
        # Turning 2**3_000_000 (903 thousand digits) into text would allocate at
        # least 900 kB. The refusal must come from its bit length alone.
        with int_str_digits_limit(0):
            value = {"x": 2**3_000_000}
            tracemalloc.start()
            try:
                tracemalloc.reset_peak()
                with (
                    self.assertRaises(InvalidCommandArgumentError),
                    encoder_must_not_run(),
                ):
                    await self.create_task(input=value)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        self.assertLess(peak, 300_000)

    async def test_postgresql_refuses_more_digits_than_the_limit_states(self):
        # The constant MAX_INPUT_INTEGER_DIGITS is PostgreSQL's own limit for the
        # digits of a ``numeric`` (which every JSONB number is).
        sql = text("SELECT length(CAST(:document AS jsonb)::text)")
        with int_str_digits_limit(0):
            fits = '{"x": 1' + "0" * (MAX_INPUT_INTEGER_DIGITS - 1) + "}"
            too_long = '{"x": 1' + "0" * MAX_INPUT_INTEGER_DIGITS + "}"
            async with self.database.engine.connect() as connection:
                stored = await connection.scalar(sql, {"document": fits})
                self.assertEqual(stored, len(fits))
                await connection.rollback()
                with self.assertRaises(DBAPIError) as caught:
                    await connection.execute(sql, {"document": too_long})
                self.assertEqual(caught.exception.orig.sqlstate, "22003")


@requires_postgres
class UnstorableTextTest(PostgresTaskTestCase):
    """Text that a PostgreSQL text column cannot hold is refused up front.

    NUL fails as a ``DataError`` and a surrogate code point as a
    ``UnicodeEncodeError`` when the row is flushed, which would surface as a
    database or encoding error instead of the typed argument error.
    """

    MARKER = "SECRET-MARKER"
    BAD = {
        "NUL": MARKER + "\x00",
        "lone surrogate": MARKER + chr(0xD800),
        "surrogate pair as code points": MARKER + chr(0xD83D) + chr(0xDE00),
    }

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # One task per starting point the entry points below need.
        self.idle = await self.task_in_state(S.RUNNING)
        self.busy = await self.task_in_state(S.RUNNING)
        self.step = await self.service.begin_step(self.busy, "build", run=FIRST_RUN)
        self.failed = await self.task_in_state(S.FAILED)

    async def assert_refused(self, task_id, call) -> None:
        """The typed error, no echo of the text, and nothing about the task changed."""
        before = await self.service.restore(task_id)
        with self.assertRaises(InvalidCommandArgumentError) as caught:
            await call()
        self.assertNotIn(self.MARKER, str(caught.exception))
        self.assertEqual(await self.service.restore(task_id), before)

    def entry_points(self, bad: str) -> dict:
        service = self.service
        return {
            "begin_step name": (
                self.idle,
                lambda: service.begin_step(self.idle, bad, run=FIRST_RUN),
            ),
            "begin_tool_invocation tool_name": (
                self.busy,
                lambda: service.begin_tool_invocation(
                    self.busy, step_id=self.step.id, tool_name=bad
                ),
            ),
            "add_log message": (
                self.idle,
                lambda: service.add_log(self.idle, bad, run=FIRST_RUN),
            ),
            "add_log message cut off by the length limit": (
                self.idle,
                lambda: service.add_log(self.idle, "a" * 9000 + bad, run=FIRST_RUN),
            ),
            "execute reason": (
                self.idle,
                lambda: service.execute(
                    self.idle, C.CANCEL, actor=self.user, reason=bad
                ),
            ),
            "execute stop now reason": (
                self.idle,
                lambda: service.execute(
                    self.idle, C.STOP_NOW, actor=self.user, reason=bad
                ),
            ),
            "execute agent": (
                self.failed,
                lambda: service.execute(
                    self.failed, C.RETRY, actor=self.user, agent=bad
                ),
            ),
            "execute model": (
                self.failed,
                lambda: service.execute(
                    self.failed, C.RESTART, actor=self.user, model=bad
                ),
            ),
            "update_attempt branch": (
                self.idle,
                lambda: service.update_attempt(
                    self.idle, run=FIRST_RUN, worktree=WorktreeState(branch=bad)
                ),
            ),
            "update_attempt path": (
                self.idle,
                lambda: service.update_attempt(
                    self.idle, run=FIRST_RUN, worktree=WorktreeState(path=bad)
                ),
            ),
            "update_attempt head_commit": (
                self.idle,
                lambda: service.update_attempt(
                    self.idle, run=FIRST_RUN, worktree=WorktreeState(head_commit=bad)
                ),
            ),
            "update_attempt pull request url": (
                self.idle,
                lambda: service.update_attempt(
                    self.idle,
                    run=FIRST_RUN,
                    pull_request=PullRequestInfo(1, bad, PullRequestState.OPEN),
                ),
            ),
        }

    async def test_every_text_entry_point_refuses_nul_and_surrogates(self):
        checked = 0
        for label, bad in self.BAD.items():
            for name, (task_id, call) in self.entry_points(bad).items():
                with self.subTest(f"{name}: {label}"):
                    checked += 1
                    await self.assert_refused(task_id, call)
        self.assertEqual(checked, 12 * len(self.BAD))

    async def test_create_task_refuses_nul_and_surrogates_in_every_text_field(self):
        count = "SELECT count(*) FROM tasks WHERE project_id = :project_id"
        for label, bad in self.BAD.items():
            for field in ("title", "starting_commit", "agent", "model"):
                with self.subTest(f"{field}: {label}"):
                    before = await self.scalar(count, project_id=self.project_id)
                    with self.assertRaises(InvalidCommandArgumentError) as caught:
                        await self.create_task(**{field: bad})
                    self.assertNotIn(self.MARKER, str(caught.exception))
                    self.assertEqual(
                        await self.scalar(count, project_id=self.project_id), before
                    )

    async def test_other_unusual_text_is_still_stored_unchanged(self):
        text = '日本語 \U0001f600 \x01 tab\t newline\n quote" back\\slash é'
        task_id = await self.create_task(
            title=text, agent=text, model=text, starting_commit=text
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.add_log(task_id, text, run=FIRST_RUN)
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            worktree=WorktreeState(text, text, text),
            pull_request=PullRequestInfo(1, text, PullRequestState.OPEN),
        )
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            (snapshot.title, snapshot.agent, snapshot.model, snapshot.starting_commit),
            (text, text, text, text),
        )
        self.assertEqual([log.message for log in snapshot.recent_logs], [text])
        self.assertEqual(snapshot.attempt.worktree, WorktreeState(text, text, text))
        self.assertEqual(snapshot.attempt.pull_request.url, text)


@requires_postgres
class AttemptStateLimitsTest(PostgresTaskTestCase):
    """``update_attempt`` refuses what its columns cannot hold, with a typed error."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.task_id = await self.task_in_state(S.RUNNING)

    async def assert_refused(self, **groups) -> None:
        before = await self.service.restore(self.task_id)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.update_attempt(self.task_id, run=FIRST_RUN, **groups)
        self.assertEqual(await self.service.restore(self.task_id), before)

    async def test_worktree_and_pull_request_text_is_accepted_at_the_column_limit(
        self,
    ):
        worktree = WorktreeState("b" * 255, "p" * 1024, "c" * 64)
        pull_request = PullRequestInfo(12, "u" * 2048, PullRequestState.OPEN)
        await self.service.update_attempt(
            self.task_id, run=FIRST_RUN, worktree=worktree, pull_request=pull_request
        )
        attempt = (await self.service.restore(self.task_id)).attempt
        self.assertEqual(attempt.worktree, worktree)
        self.assertEqual(attempt.pull_request, pull_request)

    async def test_limits_count_characters_not_bytes(self):
        worktree = WorktreeState("日" * 255, "é" * 1024, "😀" * 64)
        await self.service.update_attempt(
            self.task_id, run=FIRST_RUN, worktree=worktree
        )
        self.assertEqual(
            (await self.service.restore(self.task_id)).attempt.worktree, worktree
        )

    async def test_one_character_over_the_column_limit_is_refused(self):
        secret = "SECRET-MARKER"
        cases = {
            "branch": {"worktree": WorktreeState(branch="b" * 256)},
            "path": {"worktree": WorktreeState(path="p" * 1025)},
            "head_commit": {"worktree": WorktreeState(head_commit="c" * 65)},
            "url": {
                "pull_request": PullRequestInfo(12, "u" * 2049, PullRequestState.OPEN)
            },
            "long text that carries a marker": {
                "worktree": WorktreeState(branch=secret * 100)
            },
        }
        for name, groups in cases.items():
            with self.subTest(name):
                before = await self.service.restore(self.task_id)
                with self.assertRaises(InvalidCommandArgumentError) as caught:
                    await self.service.update_attempt(
                        self.task_id, run=FIRST_RUN, **groups
                    )
                self.assertNotIn(secret, str(caught.exception))
                self.assertEqual(await self.service.restore(self.task_id), before)

    async def test_pull_request_number_must_fit_the_integer_column(self):
        for number in (1, 2**31 - 1):
            with self.subTest(f"accepted {number}"):
                pull_request = PullRequestInfo(
                    number, "https://example.test/pr", PullRequestState.OPEN
                )
                await self.service.update_attempt(
                    self.task_id, run=FIRST_RUN, pull_request=pull_request
                )
                attempt = (await self.service.restore(self.task_id)).attempt
                self.assertEqual(attempt.pull_request, pull_request)

        # A number is a positive integer of at most 2**31 - 1: neither a bool
        # (which is an int in Python) nor text nor a float is accepted.
        for number in (0, -1, 2**31, 2**63, True, "12", 12.0):
            with self.subTest(f"refused {number!r}"):
                await self.assert_refused(
                    pull_request=PullRequestInfo(
                        number, "https://example.test/other", PullRequestState.OPEN
                    )
                )


@requires_postgres
class TransitionTest(PostgresTaskTestCase):
    async def test_database_follows_the_transition_table_for_every_state_and_command(
        self,
    ):
        checked = 0
        for state in TaskState:
            for command in TaskCommand:
                with self.subTest(state=state.value, command=command.value):
                    checked += 1
                    task_id = await self.task_in_state(state)
                    before = await self.service.restore(task_id)
                    wait_reason = WaitReason.APPROVAL if command is C.WAIT else None
                    target = EXPECTED[state].get(command)
                    if target is None:
                        with self.assertRaises(IllegalTransitionError):
                            await self.service.execute(
                                task_id,
                                command,
                                actor=self.system,
                                wait_reason=wait_reason,
                                reason=command_reason(command),
                            )
                        after = await self.service.restore(task_id)
                        self.assertEqual(after.state, state)
                        self.assertEqual(after.version, before.version)
                        self.assertEqual(after.last_event, before.last_event)
                    else:
                        event = await self.service.execute(
                            task_id,
                            command,
                            actor=self.system,
                            wait_reason=wait_reason,
                            reason=command_reason(command),
                        )
                        self.assertEqual(
                            (event.from_state, event.to_state), (state, target)
                        )
                        after = await self.service.restore(task_id)
                        self.assertEqual(after.state, target)
                        self.assertEqual(after.version, before.version + 1)
                        self.assertEqual(after.last_event, event)
        self.assertEqual(checked, 8 * 13)

    async def test_completed_task_rejects_every_command(self):
        task_id = await self.task_in_state(S.COMPLETED)
        for command in TaskCommand:
            with (
                self.subTest(command=command.value),
                self.assertRaises(IllegalTransitionError),
            ):
                await self.service.execute(
                    task_id,
                    command,
                    actor=self.system,
                    wait_reason=WaitReason.USER if command is C.WAIT else None,
                    reason=command_reason(command),
                )
        self.assertEqual((await self.service.restore(task_id)).state, S.COMPLETED)

    async def test_history_records_every_transition_with_who_and_what(self):
        task_id = await self.create_task()
        steps = [
            (C.START, self.system, None, None),
            (C.WAIT, self.system, WaitReason.APPROVAL, "needs merge approval"),
            (C.UNBLOCK, self.user, None, "approved"),
            (C.BEGIN_EVALUATION, self.system, None, None),
            (C.COMPLETE, self.system, None, None),
        ]
        for command, actor, wait_reason, reason in steps:
            await self.service.execute(
                task_id, command, actor=actor, wait_reason=wait_reason, reason=reason
            )

        events = await self.service.history(task_id)
        self.assertEqual(
            [event.command for event in events],
            [C.CREATE, C.START, C.WAIT, C.UNBLOCK, C.BEGIN_EVALUATION, C.COMPLETE],
        )
        self.assertEqual(
            [(event.from_state, event.to_state) for event in events],
            [
                (None, S.QUEUED),
                (S.QUEUED, S.RUNNING),
                (S.RUNNING, S.WAITING),
                (S.WAITING, S.RUNNING),
                (S.RUNNING, S.EVALUATING),
                (S.EVALUATING, S.COMPLETED),
            ],
        )
        self.assertEqual([event.task_version for event in events], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [event.seq for event in events], sorted({event.seq for event in events})
        )
        self.assertEqual(
            [(event.actor.kind, event.actor.id) for event in events],
            [
                (ActorKind.USER, self.user_id),
                (ActorKind.SYSTEM, None),
                (ActorKind.SYSTEM, None),
                (ActorKind.USER, self.user_id),
                (ActorKind.SYSTEM, None),
                (ActorKind.SYSTEM, None),
            ],
        )
        self.assertEqual(
            [event.wait_reason for event in events],
            [None, None, WaitReason.APPROVAL, None, None, None],
        )
        self.assertEqual(events[2].reason, "needs merge approval")
        self.assertEqual(events[3].reason, "approved")
        self.assertTrue(all(event.attempt == 1 for event in events))
        self.assertEqual(
            [event.created_at for event in events],
            sorted(event.created_at for event in events),
        )

    async def test_history_can_resume_after_a_sequence_number(self):
        task_id = await self.task_in_state(S.RUNNING)
        events = await self.service.history(task_id)
        later = await self.service.history(task_id, after_seq=events[0].seq)
        self.assertEqual(later, events[1:])
        self.assertEqual(
            await self.service.history(task_id, after_seq=events[-1].seq), []
        )

    async def test_pause_and_resume_keep_the_step_worktree_and_logs(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        await self.service.add_log(task_id, "editing parser.py", run=FIRST_RUN)
        await self.service.update_attempt(task_id, run=FIRST_RUN, worktree=WORKTREE)

        paused = await self.service.execute(
            task_id, C.PAUSE, actor=self.user, reason="lunch"
        )
        self.assertEqual(
            (paused.to_state, paused.interruption), (S.PAUSED, Interruption.GRACEFUL)
        )
        self.assertEqual(paused.step_name, "implement")
        during = await self.service.restore(task_id)

        await self.service.execute(task_id, C.RESUME, actor=self.user)
        after = await self.service.restore(task_id)
        self.assertEqual(after.state, S.RUNNING)
        self.assertEqual(after.attempt, during.attempt)
        self.assertEqual(after.attempt.worktree, WORKTREE)
        self.assertEqual(after.current_step, during.current_step)
        self.assertEqual(after.current_step.name, "implement")
        self.assertEqual(
            [log.message for log in after.recent_logs], ["editing parser.py"]
        )

    async def test_cancel_is_graceful_and_keeps_artifacts_and_the_running_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        await self.service.update_attempt(task_id, run=FIRST_RUN, worktree=WORKTREE)

        event = await self.service.execute(
            task_id, C.CANCEL, actor=self.user, reason="not needed"
        )
        self.assertEqual(
            (event.to_state, event.interruption), (S.CANCELLED, Interruption.GRACEFUL)
        )
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.worktree, WORKTREE)
        # The worker finishes its step itself; Cancel does not touch it.
        self.assertEqual(snapshot.current_step.status, StepStatus.RUNNING)
        finished = await self.service.finish_step(
            task_id, step.id, StepStatus.INTERRUPTED
        )
        self.assertEqual(finished.status, StepStatus.INTERRUPTED)

    async def test_stop_now_interrupts_the_step_immediately_and_logs_why(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "run-tests", run=FIRST_RUN)
        await self.service.update_attempt(task_id, run=FIRST_RUN, worktree=WORKTREE)

        event = await self.service.execute(
            task_id, C.STOP_NOW, actor=self.user, reason="agent loop"
        )
        self.assertEqual(
            (event.to_state, event.interruption), (S.CANCELLED, Interruption.IMMEDIATE)
        )
        self.assertEqual(event.step_name, "run-tests")
        self.assertEqual(event.reason, "agent loop")

        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.current_step.name, "run-tests")
        self.assertEqual(snapshot.current_step.status, StepStatus.INTERRUPTED)
        self.assertIsNotNone(snapshot.current_step.finished_at)
        self.assertEqual(len(snapshot.recent_logs), 1)
        self.assertEqual(snapshot.recent_logs[0].level, LogLevel.WARNING)
        self.assertEqual(
            snapshot.recent_logs[0].message,
            "Stop Now: interrupted step 'run-tests' (reason: agent loop)",
        )
        # Nothing is deleted: the worktree state is still there.
        self.assertEqual(snapshot.attempt.worktree, WORKTREE)

    async def test_stop_now_without_a_running_step_still_logs(self):
        task_id = await self.task_in_state(S.WAITING)
        await self.service.execute(
            task_id, C.STOP_NOW, actor=self.system, reason="operator request"
        )
        snapshot = await self.service.restore(task_id)
        self.assertIsNone(snapshot.current_step)
        self.assertEqual(
            [log.message for log in snapshot.recent_logs],
            ["Stop Now: no step was running (reason: operator request)"],
        )
        self.assertIsNone(snapshot.last_event.step_name)

    async def test_stop_now_after_the_step_finished_does_not_call_it_interrupted(self):
        for finished_as in (
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.INTERRUPTED,
        ):
            with self.subTest(step=finished_as.value):
                task_id = await self.task_in_state(S.RUNNING)
                step = await self.service.begin_step(
                    task_id, "run-tests", run=FIRST_RUN
                )
                await self.service.finish_step(task_id, step.id, finished_as)

                event = await self.service.execute(
                    task_id, C.STOP_NOW, actor=self.user, reason="just in case"
                )
                self.assertEqual(event.to_state, S.CANCELLED)
                self.assertIsNone(event.step_name)
                snapshot = await self.service.restore(task_id)
                # The finished step keeps the outcome its worker recorded.
                self.assertEqual(snapshot.current_step.status, finished_as)
                (log,) = snapshot.recent_logs
                self.assertEqual(
                    log.message, "Stop Now: no step was running (reason: just in case)"
                )
                self.assertNotIn("interrupted", log.message)

    async def test_stop_now_records_its_reason_in_the_event_and_the_log_line(self):
        """Every accepted Stop Now leaves the reason in both places (REQUIREMENTS)."""
        reason = "agent loop: 3rd identical tool call"
        for state, with_step in (
            (S.RUNNING, True),
            (S.RUNNING, False),
            (S.WAITING, False),
            (S.EVALUATING, False),
        ):
            with self.subTest(state=state.value, step=with_step):
                task_id = await self.task_in_state(state)
                if with_step:
                    await self.service.begin_step(task_id, "build", run=FIRST_RUN)
                event = await self.service.execute(
                    task_id, C.STOP_NOW, actor=self.user, reason=reason
                )
                self.assertEqual(event.reason, reason)
                self.assertEqual((await self.service.history(task_id))[-1], event)
                self.assertEqual(
                    await self.scalar(
                        "SELECT reason FROM task_events "
                        "WHERE task_id = :i AND command = 'stop_now'",
                        i=task_id,
                    ),
                    reason,
                )
                expected = (
                    "Stop Now: interrupted step 'build'"
                    if with_step
                    else "Stop Now: no step was running"
                )
                self.assertEqual(
                    await self.scalar(
                        "SELECT message FROM task_logs WHERE task_id = :i", i=task_id
                    ),
                    f"{expected} (reason: {reason})",
                )

    async def assert_stop_now_refused(self, **arguments) -> None:
        """Stop Now with these arguments fails typed and writes nothing at all."""
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "build", run=FIRST_RUN)
        before = await self.service.restore(task_id)
        counts = (
            "SELECT (SELECT count(*) FROM task_events WHERE task_id = :i), "
            "(SELECT count(*) FROM task_logs WHERE task_id = :i)"
        )
        async with self.database.engine.connect() as connection:
            rows_before = (await connection.execute(text(counts), {"i": task_id})).one()

        with self.assertRaises(InvalidCommandArgumentError) as caught:
            await self.service.execute(
                task_id, C.STOP_NOW, actor=self.user, **arguments
            )
        # The message names the argument, never its value.
        self.assertIn("reason", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))

        after = await self.service.restore(task_id)
        self.assertEqual(after, before)  # state, version, last event, logs, steps
        self.assertEqual(after.state, S.RUNNING)
        self.assertEqual(after.current_step.status, StepStatus.RUNNING)
        self.assertEqual(after.recent_logs, ())
        async with self.database.engine.connect() as connection:
            rows_after = (await connection.execute(text(counts), {"i": task_id})).one()
        self.assertEqual(rows_after, rows_before)

    async def test_stop_now_without_a_reason_is_refused_and_writes_nothing(self):
        await self.assert_stop_now_refused()
        await self.assert_stop_now_refused(reason=None)

    async def test_the_serialised_command_needs_a_reason_like_the_member(self):
        # "stop_now" (a plain string) is the same command as C.STOP_NOW.
        task_id = await self.task_in_state(S.RUNNING)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(task_id, "stop_now", actor=self.user)
        self.assertEqual((await self.service.restore(task_id)).state, S.RUNNING)
        event = await self.service.execute(
            task_id, "stop_now", actor=self.user, reason="runaway"
        )
        self.assertEqual((event.command, event.reason), (C.STOP_NOW, "runaway"))
        self.assertIs(event.command, C.STOP_NOW)
        snapshot = await self.service.restore(task_id)
        self.assertTrue(
            any("(reason: runaway)" in log.message for log in snapshot.recent_logs)
        )

    async def test_a_command_that_is_not_a_command_is_refused_with_a_typed_error(self):
        task_id = await self.task_in_state(S.RUNNING)
        for bad in ("bogus", "", "STOP_NOW", None, 1, object()):
            with self.subTest(command=repr(bad)):
                with self.assertRaises(InvalidCommandArgumentError):
                    await self.service.execute(
                        task_id, bad, actor=self.user, reason="r"
                    )
        self.assertEqual((await self.service.restore(task_id)).state, S.RUNNING)

    async def test_stop_now_with_an_empty_or_blank_reason_is_refused(self):
        for blank in ("", " ", "   ", "\t", "\n", " \t\r\n ", "\u3000"):
            with self.subTest(reason=blank):
                await self.assert_stop_now_refused(reason=blank)

    async def test_stop_now_with_a_reason_that_is_not_a_string_is_refused(self):
        for bad in (0, 1, True, 1.5, b"SECRET", ["SECRET"], {"SECRET": 1}, object()):
            with self.subTest(reason=type(bad).__name__):
                await self.assert_stop_now_refused(reason=bad)

    async def test_stop_now_reason_gets_the_same_text_checks_as_other_reasons(self):
        for name, bad in {
            "too long": "SECRET" * 100,
            "NUL": "SECRET\x00",
            "lone surrogate": "SECRET" + chr(0xD800),
        }.items():
            with self.subTest(reason=name):
                await self.assert_stop_now_refused(reason=bad)
        # The length limit is inclusive: exactly the limit is accepted.
        task_id = await self.task_in_state(S.RUNNING)
        event = await self.service.execute(
            task_id, C.STOP_NOW, actor=self.user, reason="r" * MAX_REASON_LENGTH
        )
        self.assertEqual(event.reason, "r" * MAX_REASON_LENGTH)

    async def test_a_missing_reason_is_judged_before_the_state_of_the_task(self):
        """The argument is checked up front, so no state is needed to be refused."""
        for state in (S.QUEUED, S.PAUSED, S.COMPLETED):
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(state)
                with self.assertRaises(InvalidCommandArgumentError):
                    await self.service.execute(task_id, C.STOP_NOW, actor=self.user)
                # With a reason the state decides again.
                with self.assertRaises(IllegalTransitionError):
                    await self.service.execute(
                        task_id, C.STOP_NOW, actor=self.user, reason="agent loop"
                    )

    async def test_other_commands_still_accept_no_reason(self):
        for command, state in (
            (C.PAUSE, S.RUNNING),
            (C.CANCEL, S.RUNNING),
            (C.FAIL, S.RUNNING),
        ):
            with self.subTest(command=command.value):
                task_id = await self.task_in_state(state)
                event = await self.service.execute(task_id, command, actor=self.user)
                self.assertIsNone(event.reason)

    async def test_fail_after_the_step_finished_names_no_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "run-tests", run=FIRST_RUN)
        await self.service.finish_step(task_id, step.id, StepStatus.FAILED)
        event = await self.service.execute(task_id, C.FAIL, actor=self.system)
        self.assertIsNone(event.step_name)
        self.assertEqual(
            (await self.service.restore(task_id)).current_step.status, StepStatus.FAILED
        )

    async def test_other_commands_still_name_the_latest_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "run-tests", run=FIRST_RUN)
        await self.service.finish_step(task_id, step.id, StepStatus.SUCCEEDED)
        event = await self.service.execute(task_id, C.PAUSE, actor=self.user)
        self.assertEqual(event.step_name, "run-tests")

    async def test_cancel_and_stop_now_events_are_distinguishable(self):
        cancelled = await self.task_in_state(S.RUNNING)
        stopped = await self.task_in_state(S.RUNNING)
        cancel_event = await self.service.execute(cancelled, C.CANCEL, actor=self.user)
        stop_event = await self.service.execute(
            stopped, C.STOP_NOW, actor=self.user, reason="agent loop"
        )
        self.assertEqual(cancel_event.to_state, stop_event.to_state)
        self.assertNotEqual(cancel_event.interruption, stop_event.interruption)
        restored = await self.service.restore(stopped)
        self.assertEqual(restored.last_event.command, C.STOP_NOW)
        self.assertEqual(restored.last_event.interruption, Interruption.IMMEDIATE)

    async def test_fail_ends_the_running_step_as_failed(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "run-tests", run=FIRST_RUN)
        event = await self.service.execute(
            task_id, C.FAIL, actor=self.system, reason="tests red"
        )
        self.assertEqual((event.to_state, event.step_name), (S.FAILED, "run-tests"))
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.current_step.status, StepStatus.FAILED)

    async def test_retry_reruns_the_failed_step_in_the_same_attempt(self):
        task_id = await self.create_task(agent="local", model="small")
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        await self.service.update_attempt(task_id, run=FIRST_RUN, worktree=WORKTREE)
        await self.service.add_log(task_id, "compile error", run=FIRST_RUN)
        await self.service.execute(task_id, C.FAIL, actor=self.system)

        event = await self.service.execute(
            task_id, C.RETRY, actor=self.user, agent="codex", reason="escalate"
        )
        self.assertEqual((event.from_state, event.to_state), (S.FAILED, S.QUEUED))
        self.assertEqual(event.step_name, "implement")
        self.assertEqual(event.attempt, 1)
        self.assertEqual(event.detail, {"agent": {"from": "local", "to": "codex"}})

        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.number, 1)
        self.assertEqual(snapshot.previous_attempts, ())
        self.assertEqual(snapshot.retry_count, 1)
        self.assertEqual((snapshot.agent, snapshot.model), ("codex", "small"))
        # Same branch / worktree / logs are reused.
        self.assertEqual(snapshot.attempt.worktree, WORKTREE)
        self.assertEqual(
            [log.message for log in snapshot.recent_logs], ["compile error"]
        )
        # The failed step stays as history; the next run starts a new step row.
        self.assertEqual(snapshot.current_step.status, StepStatus.FAILED)
        started = await self.service.execute(task_id, C.START, actor=self.system)
        # The Retry made a new run of the same attempt; Start hands it to the worker.
        self.assertEqual(started.run, TaskRun(1, 1))
        rerun = await self.service.begin_step(task_id, "implement", run=started.run)
        self.assertEqual(rerun.sequence, 2)

    async def test_retry_history_counts_every_retry(self):
        task_id = await self.task_in_state(S.FAILED)
        for expected in (1, 2, 3):
            await self.service.execute(task_id, C.RETRY, actor=self.user)
            await self.service.execute(task_id, C.START, actor=self.system)
            await self.service.execute(task_id, C.FAIL, actor=self.system)
            self.assertEqual(
                (await self.service.restore(task_id)).retry_count, expected
            )
        events = await self.service.history(task_id)
        self.assertEqual([e.command for e in events].count(C.RETRY), 3)

    async def test_every_event_carries_the_run_the_task_is_in(self):
        task_id = await self.create_task()
        script = [
            (C.START, TaskRun(1, 0)),
            (C.FAIL, TaskRun(1, 0)),
            (C.RETRY, TaskRun(1, 1)),  # the same attempt, run again
            (C.START, TaskRun(1, 1)),
            (C.FAIL, TaskRun(1, 1)),
            (C.RETRY, TaskRun(1, 2)),
            (C.START, TaskRun(1, 2)),
            (C.FAIL, TaskRun(1, 2)),
            (C.RESTART, TaskRun(2, 2)),  # a new attempt; the retry count stays
            (C.START, TaskRun(2, 2)),
        ]
        returned = [
            (
                command,
                (await self.service.execute(task_id, command, actor=self.user)).run,
            )
            for command, _ in script
        ]
        self.assertEqual(returned, script)
        events = await self.service.history(task_id)
        self.assertEqual(
            [(e.command, e.run) for e in events], [(C.CREATE, TaskRun(1, 0)), *script]
        )
        self.assertEqual(
            await self.scalar(
                "SELECT array_agg(retry_count ORDER BY seq) FROM task_events "
                "WHERE task_id = :i",
                i=task_id,
            ),
            [0, 0, 0, 1, 1, 1, 2, 2, 2, 2, 2],
        )
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.run, TaskRun(2, 2))
        self.assertEqual(snapshot.run, snapshot.last_event.run)

    async def test_a_line_the_service_logs_itself_carries_the_run_too(self):
        task_id = await self.task_in_state(S.FAILED)
        await self.service.execute(task_id, C.RETRY, actor=self.user)
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.execute(
            task_id, C.STOP_NOW, actor=self.user, reason="agent loop"
        )
        (line,) = (await self.service.restore(task_id)).recent_logs
        self.assertEqual((line.level, line.run), (LogLevel.WARNING, TaskRun(1, 1)))
        self.assertEqual(
            await self.scalar(
                "SELECT retry_count FROM task_logs WHERE task_id = :i", i=task_id
            ),
            1,
        )

    async def test_restart_starts_a_new_attempt_and_keeps_the_old_one_as_history(self):
        task_id = await self.create_task(
            starting_commit="a" * 40, input={"prompt": "p"}
        )
        await self.service.execute(task_id, C.START, actor=self.system)
        await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        await self.service.add_log(task_id, "attempt one log", run=FIRST_RUN)
        await self.service.update_attempt(
            task_id,
            run=FIRST_RUN,
            worktree=WORKTREE,
            review=ReviewState(ReviewStatus.CHANGES_REQUESTED, EvaluationResult.FAILED),
            pull_request=PullRequestInfo(
                7, "https://example.test/pr/7", PullRequestState.OPEN
            ),
        )
        await self.service.execute(task_id, C.FAIL, actor=self.system)

        event = await self.service.execute(
            task_id, C.RESTART, actor=self.user, model="big"
        )
        self.assertEqual((event.from_state, event.to_state), (S.FAILED, S.QUEUED))
        self.assertEqual(event.attempt, 2)
        self.assertEqual(event.step_name, "implement")
        self.assertEqual(
            event.detail, {"model": {"from": None, "to": "big"}, "previous_attempt": 1}
        )

        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.attempt.number, 2)
        # The new attempt has its own (empty) branch / worktree / review / PR.
        self.assertEqual(snapshot.attempt.worktree, WorktreeState())
        self.assertEqual(snapshot.attempt.review, ReviewState())
        self.assertIsNone(snapshot.attempt.pull_request)
        self.assertIsNone(snapshot.current_step)
        self.assertEqual(snapshot.recent_logs, ())
        # Starting point and input are unchanged, and Restart is not a Retry.
        self.assertEqual(snapshot.starting_commit, "a" * 40)
        self.assertEqual(snapshot.input, {"prompt": "p"})
        self.assertEqual(snapshot.retry_count, 0)
        self.assertEqual(snapshot.model, "big")
        # The old attempt is kept intact.
        (old,) = snapshot.previous_attempts
        self.assertEqual(old.number, 1)
        self.assertEqual(old.worktree, WORKTREE)
        self.assertEqual(old.review.review_status, ReviewStatus.CHANGES_REQUESTED)
        self.assertEqual(old.pull_request.number, 7)
        old_logs = await self.scalar(
            "SELECT count(*) FROM task_logs WHERE task_id = :id AND attempt = 1",
            id=task_id,
        )
        self.assertEqual(old_logs, 1)

    async def test_cancelled_task_can_be_restarted(self):
        task_id = await self.task_in_state(S.CANCELLED)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        snapshot = await self.service.restore(task_id)
        self.assertEqual((snapshot.state, snapshot.attempt.number), (S.QUEUED, 2))
        self.assertEqual(len(snapshot.previous_attempts), 1)

    async def test_agent_or_model_is_only_accepted_by_retry_and_restart(self):
        task_id = await self.task_in_state(S.RUNNING)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(task_id, C.PAUSE, actor=self.user, agent="codex")
        self.assertEqual((await self.service.restore(task_id)).state, S.RUNNING)

    async def test_command_argument_errors_and_unknown_tasks(self):
        task_id = await self.task_in_state(S.RUNNING)
        secret = "SECRET-MARKER"
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(task_id, C.WAIT, actor=self.system)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.execute(
                task_id, C.PAUSE, actor=self.user, wait_reason=WaitReason.USER
            )
        with self.assertRaises(InvalidCommandArgumentError) as caught:
            await self.service.execute(
                task_id, C.PAUSE, actor=self.user, reason=secret * 100
            )
        self.assertNotIn(secret, str(caught.exception))
        await self.service.execute(task_id, C.PAUSE, actor=self.user, reason="r" * 500)
        with self.assertRaises(TaskNotFoundError):
            await self.service.execute(uuid.uuid4(), C.PAUSE, actor=self.user)
        with self.assertRaises(TaskNotFoundError):
            await self.service.restore(uuid.uuid4())
        with self.assertRaises(TaskNotFoundError):
            await self.service.history(uuid.uuid4())

    async def test_illegal_command_does_not_disturb_step_or_history(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        before = await self.service.history(task_id)
        with self.assertRaises(IllegalTransitionError):
            await self.service.execute(task_id, C.RESUME, actor=self.user)
        self.assertEqual(await self.service.history(task_id), before)
        self.assertEqual(
            (await self.service.restore(task_id)).current_step.status,
            StepStatus.RUNNING,
        )


@requires_postgres
class StepAndLogTest(PostgresTaskTestCase):
    async def test_steps_are_numbered_and_only_one_runs_at_a_time(self):
        task_id = await self.task_in_state(S.RUNNING)
        first = await self.service.begin_step(task_id, "plan", run=FIRST_RUN)
        self.assertEqual((first.sequence, first.status), (1, StepStatus.RUNNING))
        with self.assertRaises(TaskStepError):
            await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        done = await self.service.finish_step(task_id, first.id, StepStatus.SUCCEEDED)
        self.assertEqual((done.name, done.status), ("plan", StepStatus.SUCCEEDED))
        self.assertIsNotNone(done.finished_at)
        second = await self.service.begin_step(task_id, "implement", run=FIRST_RUN)
        self.assertEqual(second.sequence, 2)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            (snapshot.current_step.name, snapshot.current_step.sequence),
            ("implement", 2),
        )

    async def test_a_step_may_start_while_running_waiting_or_evaluating_only(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(state)
                if state in (S.RUNNING, S.WAITING, S.EVALUATING):
                    step = await self.service.begin_step(task_id, "work", run=FIRST_RUN)
                    self.assertEqual(step.status, StepStatus.RUNNING)
                else:
                    with self.assertRaises(TaskStepError):
                        await self.service.begin_step(task_id, "work", run=FIRST_RUN)
                    self.assertIsNone(
                        (await self.service.restore(task_id)).current_step
                    )

    async def test_finishing_needs_a_running_step_and_a_final_status(self):
        task_id = await self.task_in_state(S.RUNNING)
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(task_id, 999_999_999, StepStatus.SUCCEEDED)
        step = await self.service.begin_step(task_id, "plan", run=FIRST_RUN)
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.finish_step(task_id, step.id, StepStatus.RUNNING)
        await self.service.finish_step(task_id, step.id, StepStatus.FAILED)
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(task_id, step.id, StepStatus.FAILED)

    async def test_a_step_of_another_task_cannot_be_finished_through_this_one(self):
        mine = await self.task_in_state(S.RUNNING)
        other = await self.task_in_state(S.RUNNING)
        foreign = await self.service.begin_step(other, "plan", run=FIRST_RUN)
        with self.assertRaises(TaskStepError):
            await self.service.finish_step(mine, foreign.id, StepStatus.SUCCEEDED)
        current = (await self.service.restore(other)).current_step
        self.assertEqual(current.status, StepStatus.RUNNING)

    async def test_step_name_is_validated(self):
        task_id = await self.task_in_state(S.RUNNING)
        for name in ("", "  ", "x" * 101):
            with (
                self.subTest(length=len(name)),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                await self.service.begin_step(task_id, name, run=FIRST_RUN)

    async def test_step_and_log_calls_reject_unknown_tasks(self):
        unknown = uuid.uuid4()
        with self.assertRaises(TaskNotFoundError):
            await self.service.begin_step(unknown, "plan", run=FIRST_RUN)
        with self.assertRaises(TaskNotFoundError):
            await self.service.finish_step(unknown, 1, StepStatus.SUCCEEDED)
        with self.assertRaises(TaskNotFoundError):
            await self.service.add_log(unknown, "hello", run=FIRST_RUN)
        with self.assertRaises(TaskNotFoundError):
            await self.service.update_attempt(unknown, run=FIRST_RUN, worktree=WORKTREE)

    async def test_recent_logs_are_the_latest_n_in_order(self):
        task_id = await self.task_in_state(S.RUNNING)
        for number in range(1, 6):
            await self.service.add_log(task_id, f"line {number}", run=FIRST_RUN)
        snapshot = await self.service.restore(task_id, log_limit=3)
        self.assertEqual(
            [log.message for log in snapshot.recent_logs],
            ["line 3", "line 4", "line 5"],
        )
        self.assertEqual(
            [log.seq for log in snapshot.recent_logs],
            sorted(log.seq for log in snapshot.recent_logs),
        )
        self.assertEqual(
            (await self.service.restore(task_id, log_limit=0)).recent_logs, ()
        )
        self.assertEqual(len((await self.service.restore(task_id)).recent_logs), 5)

    async def test_log_limit_is_bounded(self):
        task_id = await self.task_in_state(S.RUNNING)
        for limit in (-1, 1001):
            with (
                self.subTest(limit=limit),
                self.assertRaises(InvalidCommandArgumentError),
            ):
                await self.service.restore(task_id, log_limit=limit)

    async def test_logs_are_accepted_in_every_state_and_keep_their_level(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                task_id = await self.task_in_state(state)
                entry = await self.service.add_log(
                    task_id, "cleanup", run=FIRST_RUN, level=LogLevel.ERROR
                )
                self.assertEqual((entry.level, entry.attempt), (LogLevel.ERROR, 1))
                snapshot = await self.service.restore(task_id)
                self.assertEqual(snapshot.recent_logs, (entry,))

    async def test_a_long_log_line_is_truncated_at_the_limit(self):
        task_id = await self.task_in_state(S.RUNNING)
        exact = await self.service.add_log(task_id, "a" * 8000, run=FIRST_RUN)
        long = await self.service.add_log(task_id, "b" * 9000, run=FIRST_RUN)
        self.assertEqual(len(exact.message), 8000)
        self.assertEqual(len(long.message), 8000)
        self.assertTrue(long.message.endswith("...[truncated]"))

    async def test_attempt_state_updates_replace_only_the_given_groups(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.update_attempt(task_id, run=FIRST_RUN, worktree=WORKTREE)
        review = ReviewState(ReviewStatus.APPROVED, EvaluationResult.PASSED)
        result = await self.service.update_attempt(
            task_id, run=FIRST_RUN, review=review
        )
        self.assertEqual((result.worktree, result.review), (WORKTREE, review))
        self.assertIsNone(result.pull_request)
        pull_request = PullRequestInfo(
            3, "https://example.test/pr/3", PullRequestState.MERGED
        )
        # A pull request can change after the task is finished.
        await self.service.execute(task_id, C.BEGIN_EVALUATION, actor=self.system)
        await self.service.execute(task_id, C.COMPLETE, actor=self.system)
        await self.service.update_attempt(
            task_id, run=FIRST_RUN, pull_request=pull_request
        )
        snapshot = await self.service.restore(task_id)
        self.assertEqual(
            (
                snapshot.attempt.worktree,
                snapshot.attempt.review,
                snapshot.attempt.pull_request,
            ),
            (WORKTREE, review, pull_request),
        )


@requires_postgres
class HistoryAndListenerTest(PostgresTaskTestCase):
    async def test_event_history_cannot_be_updated_or_deleted(self):
        task_id = await self.task_in_state(S.RUNNING)
        before = await self.service.history(task_id)
        for statement in (
            "UPDATE task_events SET reason = 'edited' WHERE task_id = :id",
            "DELETE FROM task_events WHERE task_id = :id",
        ):
            with self.subTest(statement=statement):
                async with self.database.engine.connect() as connection:
                    with self.assertRaises(DBAPIError) as caught:
                        await connection.execute(text(statement), {"id": task_id})
                self.assertIn("append-only", str(caught.exception.orig))
        self.assertEqual(await self.service.history(task_id), before)

    async def test_listener_receives_each_committed_event_after_the_commit(self):
        received = []
        seen_in_database = []

        async def listener(event):
            received.append(event)
            seen_in_database.append(await self.service.history(event.task_id))

        service = TaskService(self.database, listeners=[listener])
        task_id = await self.create_task(service)
        await service.execute(task_id, C.START, actor=self.system)
        await service.execute(task_id, C.PAUSE, actor=self.user, reason="break")

        self.assertEqual(
            [event.command for event in received], [C.CREATE, C.START, C.PAUSE]
        )
        self.assertEqual(received, await service.history(task_id))
        # When a listener runs, its own event is already durable.
        self.assertEqual([len(history) for history in seen_in_database], [1, 2, 3])

    async def test_listener_is_not_called_for_rejected_or_conflicting_commands(self):
        received = []

        async def listener(event):
            received.append(event.command)

        service = TaskService(self.database, listeners=[listener])
        task_id = await self.create_task(service)
        with self.assertRaises(IllegalTransitionError):
            await service.execute(task_id, C.PAUSE, actor=self.user)
        with self.assertRaises(TaskConflictError):
            await service.execute(
                task_id, C.START, actor=self.system, expected_version=9
            )
        self.assertEqual(received, [C.CREATE])

    async def test_failing_listener_does_not_undo_or_fail_the_command(self):
        async def broken(event):
            raise RuntimeError("password=hunter2")

        service = TaskService(self.database, listeners=[broken])
        with self.assertLogs(
            "paw_backend.tasks.service", level=logging.WARNING
        ) as logs:
            task_id = await self.create_task(service)
            await service.execute(task_id, C.START, actor=self.system)
        self.assertEqual((await self.service.restore(task_id)).state, S.RUNNING)
        # Only the exception type is logged, never its message.
        self.assertEqual(len(logs.records), 2)
        for line in logs.output:
            self.assertIn("RuntimeError", line)
            self.assertNotIn("hunter2", line)


if __name__ == "__main__":
    unittest.main()
