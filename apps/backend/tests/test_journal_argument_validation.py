"""Every public method x argument x bad value of the journal, queue and consolidator.

No database. Each call is made with a ``Database`` that is not configured (any use
raises ``DatabaseNotConfiguredError``) and an authorizer that fails the test if it is
asked anything, so an ``InvalidJournalInputError`` proves that the argument was
refused BEFORE the authorizer and before any database access. The problem is the
closed code of ``InputProblem``; the message never contains the value.
"""

import inspect
import math
import unittest
from uuid import UUID, uuid4

from paw_backend.authz import (
    ALL_PROJECTS,
    AgentGrant,
    Capability,
    Principal,
    SystemRole,
)
from paw_backend.db import Database
from paw_backend.memory.journal import (
    Backoff,
    ConsolidationQueue,
    Consolidator,
    FailureKind,
    InputProblem,
    InvalidJournalInputError,
    MemoryJournal,
    Priority,
    limits,
)
from paw_backend.memory.journal.validation import (
    validate_bool,
    validate_enum,
    validate_int,
    validate_seconds,
    validate_text,
    validate_uuid,
    validate_worker_id,
)
from paw_backend.memory.models import MessageRole
from paw_backend.memory.shared import AgentActor

from .support import make_settings

P = InputProblem
SECRET = "SECRET-MARKER-" + "5b2c"


class HighStr(str):
    """A ``str`` subclass that claims to be a member (``__eq__`` always true)."""

    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class SyncWorker:
    def extract(self, input_text: str) -> str:
        return "{}"


class AsyncWorker:
    async def extract(self, input_text: str) -> str:
        return '{"memories": []}'


class ForbiddenAuthorizer:
    """Fails the test if it is asked anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authorize(self, *args, **kwargs):
        self.calls.append("authorize")
        raise AssertionError("the authorizer was asked before validation")

    async def authorize_agent_action(self, *args, **kwargs):
        self.calls.append("authorize_agent_action")
        raise AssertionError("the authorizer was asked before validation")


UUID_BAD = [
    (None, P.REQUIRED),
    ("00000000-0000-0000-0000-000000000001", P.WRONG_TYPE),
    (1, P.WRONG_TYPE),
    (b"0" * 16, P.WRONG_TYPE),
    (object(), P.WRONG_TYPE),
]
ENUM_BAD = [
    (None, P.REQUIRED),
    (5, P.WRONG_TYPE),
    (object(), P.WRONG_TYPE),
    ("HIGH", P.UNKNOWN_VALUE),
    ("urgent", P.UNKNOWN_VALUE),
    ("", P.UNKNOWN_VALUE),
    (" high", P.UNKNOWN_VALUE),
    (HighStr("nonsense"), P.WRONG_TYPE),
]


def int_bad(low: int, high: int):
    return [
        (None, P.REQUIRED),
        (True, P.WRONG_TYPE),
        (1.0, P.WRONG_TYPE),
        ("5", P.WRONG_TYPE),
        (low - 1, P.OUT_OF_RANGE),
        (high + 1, P.OUT_OF_RANGE),
    ]


def text_bad(maximum: int):
    return [
        (None, P.REQUIRED),
        (5, P.WRONG_TYPE),
        (b"bytes", P.WRONG_TYPE),
        ("", P.BLANK),
        (" \t\n", P.BLANK),
        ("a\x00b", P.INVALID_CHARACTERS),
        ("a\ud800b", P.INVALID_CHARACTERS),
        ("a" * (maximum + 1), P.TOO_LONG),
    ]


WORKER_ID_BAD = [
    (None, P.REQUIRED),
    (5, P.WRONG_TYPE),
    ("", P.INVALID_FORMAT),
    ("-worker", P.INVALID_FORMAT),
    ("a b", P.INVALID_FORMAT),
    ("w\n", P.INVALID_FORMAT),
    ("wörker", P.INVALID_FORMAT),
    ("a" * (limits.MAX_WORKER_ID_CHARS + 1), P.TOO_LONG),
]
ACTOR_BAD = [
    (None, P.WRONG_TYPE),
    ("user", P.WRONG_TYPE),
    (uuid4(), P.WRONG_TYPE),
    (object(), P.WRONG_TYPE),
]


class ArgumentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = Database(make_settings())  # not configured: any use raises
        self.authorizer = ForbiddenAuthorizer()
        self.user = Principal(uuid4(), SystemRole.USER)
        self.agent = AgentActor(
            uuid4(),
            AgentGrant(uuid4(), frozenset({Capability.MEMORY_USE}), ALL_PROJECTS),
        )
        self.journal = MemoryJournal(self.database, self.authorizer)
        self.queue = ConsolidationQueue(self.database)

    async def assert_rejected(self, call, field: str, value, problem: InputProblem):
        with self.assertRaises(InvalidJournalInputError) as raised:
            result = call()
            if inspect.isawaitable(result):
                await result
        error = raised.exception
        self.assertEqual((error.field, error.problem), (field, problem), repr(value))
        self.assertEqual(str(error), f"Invalid {field}: {problem.value}")
        self.assertEqual(self.authorizer.calls, [])

    async def run_table(self, build, field: str, bad_values):
        """``build(value)`` makes the call with ``value`` as the argument ``field``."""
        for value, problem in bad_values:
            with self.subTest(field=field, value=repr(value)[:40]):
                await self.assert_rejected(
                    lambda v=value: build(v), field, value, problem
                )


class MemoryJournalArgumentsTest(ArgumentTestCase):
    def record(self, **overrides):
        arguments = {
            "actor": self.user,
            "conversation_id": uuid4(),
            "content": "Use tabs.",
            "turn_id": None,
            "priority": Priority.NORMAL,
        }
        arguments.update(overrides)
        actor = arguments.pop("actor")
        conversation_id = arguments.pop("conversation_id")
        content = arguments.pop("content")
        return self.journal.record_user_message(
            actor, conversation_id, content, **arguments
        )

    async def test_record_user_message(self):
        await self.run_table(lambda v: self.record(actor=v), "actor", ACTOR_BAD)
        await self.run_table(
            lambda v: self.record(actor=v), "actor", [(self.agent, P.WRONG_TYPE)]
        )
        await self.run_table(
            lambda v: self.record(conversation_id=v), "conversation_id", UUID_BAD
        )
        await self.run_table(
            lambda v: self.record(content=v),
            "content",
            text_bad(limits.MAX_USER_MESSAGE_CHARS),
        )
        await self.run_table(
            lambda v: self.record(turn_id=v),
            "turn_id",
            [bad for bad in UUID_BAD if bad[0] is not None],
        )
        await self.run_table(lambda v: self.record(priority=v), "priority", ENUM_BAD)

    async def test_append_message(self):
        def build(**overrides):
            arguments = {
                "actor": self.user,
                "conversation_id": uuid4(),
                "role": MessageRole.ASSISTANT,
                "content": "ok",
                "turn_id": uuid4(),
            }
            arguments.update(overrides)
            actor = arguments.pop("actor")
            conversation_id = arguments.pop("conversation_id")
            role = arguments.pop("role")
            content = arguments.pop("content")
            return self.journal.append_message(
                actor, conversation_id, role, content, **arguments
            )

        await self.run_table(lambda v: build(actor=v), "actor", ACTOR_BAD)
        await self.run_table(
            lambda v: build(conversation_id=v), "conversation_id", UUID_BAD
        )
        await self.run_table(lambda v: build(role=v), "role", ENUM_BAD)
        # A user's words have one way in: record_user_message.
        await self.run_table(
            lambda v: build(role=v),
            "role",
            [(MessageRole.USER, P.OUT_OF_RANGE), ("user", P.OUT_OF_RANGE)],
        )
        await self.run_table(
            lambda v: build(content=v),
            "content",
            text_bad(limits.MAX_OTHER_MESSAGE_CHARS),
        )
        await self.run_table(lambda v: build(turn_id=v), "turn_id", UUID_BAD)

    async def test_pending_observations(self):
        def build(**overrides):
            arguments = {"actor": self.user, "conversation_id": uuid4(), "limit": 5}
            arguments.update(overrides)
            actor = arguments.pop("actor")
            conversation_id = arguments.pop("conversation_id")
            return self.journal.pending_observations(
                actor, conversation_id, **arguments
            )

        await self.run_table(lambda v: build(actor=v), "actor", ACTOR_BAD)
        await self.run_table(
            lambda v: build(conversation_id=v), "conversation_id", UUID_BAD
        )
        await self.run_table(
            lambda v: build(limit=v), "limit", int_bad(1, limits.MAX_PENDING_LIMIT)
        )
        await self.run_table(
            lambda v: build(limit=v),
            "limit",
            [(0, P.OUT_OF_RANGE), (-5, P.OUT_OF_RANGE)],
        )

    async def test_sync_status(self):
        await self.run_table(
            lambda v: self.journal.sync_status(v, uuid4()), "actor", ACTOR_BAD
        )
        await self.run_table(
            lambda v: self.journal.sync_status(self.user, v),
            "conversation_id",
            UUID_BAD,
        )

    async def test_the_constructor(self):
        def build(**overrides):
            arguments = {
                "database": self.database,
                "authorizer": self.authorizer,
                "lock_timeout_ms": 3000,
            }
            arguments.update(overrides)
            database = arguments.pop("database")
            authorizer = arguments.pop("authorizer")
            return MemoryJournal(database, authorizer, **arguments)

        await self.run_table(
            lambda v: build(database=v),
            "database",
            [(None, P.WRONG_TYPE), ("db", P.WRONG_TYPE), (object(), P.WRONG_TYPE)],
        )
        await self.run_table(
            lambda v: build(authorizer=v),
            "authorizer",
            [(None, P.WRONG_TYPE), (object(), P.WRONG_TYPE), ("auth", P.WRONG_TYPE)],
        )
        await self.run_table(
            lambda v: build(lock_timeout_ms=v),
            "lock_timeout_ms",
            int_bad(1, limits.MAX_LOCK_TIMEOUT_MS) + [(0, P.OUT_OF_RANGE)],
        )

    async def test_an_authorizer_with_only_one_of_the_methods_is_refused(self):
        class OnlyAuthorize:
            async def authorize(self, *args, **kwargs): ...

        with self.assertRaises(InvalidJournalInputError):
            MemoryJournal(self.database, OnlyAuthorize())

    async def test_the_message_never_contains_the_rejected_value(self):
        for call in (
            lambda: self.record(content=SECRET + "\x00"),
            lambda: self.record(content=SECRET * 50_000),
            lambda: self.record(priority=SECRET),
        ):
            with self.subTest():
                with self.assertRaises(InvalidJournalInputError) as raised:
                    await call()
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertNotIn(SECRET, repr(raised.exception))


class QueueArgumentsTest(ArgumentTestCase):
    async def test_the_constructor(self):
        def build(**overrides):
            arguments = {
                "lease_seconds": 60,
                "max_attempts": 5,
                "backoff": Backoff(),
                "lock_timeout_ms": 3000,
            }
            arguments.update(overrides)
            return ConsolidationQueue(self.database, **arguments)

        await self.run_table(
            lambda v: ConsolidationQueue(v),
            "database",
            [(None, P.WRONG_TYPE), ("db", P.WRONG_TYPE)],
        )
        await self.run_table(
            lambda v: build(lease_seconds=v),
            "lease_seconds",
            int_bad(1, limits.MAX_LEASE_SECONDS) + [(0, P.OUT_OF_RANGE)],
        )
        await self.run_table(
            lambda v: build(max_attempts=v),
            "max_attempts",
            int_bad(1, limits.MAX_MAX_ATTEMPTS) + [(0, P.OUT_OF_RANGE)],
        )
        await self.run_table(
            lambda v: build(backoff=v),
            "backoff",
            [(5, P.WRONG_TYPE), ("x", P.WRONG_TYPE), (object(), P.WRONG_TYPE)],
        )
        await self.run_table(
            lambda v: build(lock_timeout_ms=v),
            "lock_timeout_ms",
            int_bad(1, limits.MAX_LOCK_TIMEOUT_MS) + [(0, P.OUT_OF_RANGE)],
        )

    async def test_enqueue(self):
        await self.run_table(lambda v: self.queue.enqueue(v), "entry_id", UUID_BAD)
        await self.run_table(
            lambda v: self.queue.enqueue(uuid4(), v), "priority", ENUM_BAD
        )

    async def test_claim_next(self):
        await self.run_table(
            lambda v: self.queue.claim_next(v), "worker_id", WORKER_ID_BAD
        )

    async def test_heartbeat_fail_and_dead_letter(self):
        job_bad = int_bad(1, limits.MAX_JOB_ID) + [
            (0, P.OUT_OF_RANGE),
            (-1, P.OUT_OF_RANGE),
        ]
        generation_bad = int_bad(1, limits.MAX_CLAIM_COUNT) + [(0, P.OUT_OF_RANGE)]
        calls = {
            "heartbeat": lambda job, worker, generation, failure: self.queue.heartbeat(
                job, worker, generation
            ),
            "dead_letter": lambda job, worker, generation, failure: (
                self.queue.dead_letter(job, worker, generation)
            ),
            "fail": lambda job, worker, generation, failure: self.queue.fail(
                job, worker, generation, failure
            ),
        }
        for name, call in calls.items():
            with self.subTest(name):
                good = (1, "worker-1", 1, FailureKind.WORKER_ERROR)

                def build(position, value, call=call, good=good):
                    arguments = list(good)
                    arguments[position] = value
                    return call(*arguments)

                await self.run_table(lambda v: build(0, v), "job_id", job_bad)
                await self.run_table(lambda v: build(1, v), "worker_id", WORKER_ID_BAD)
                await self.run_table(
                    lambda v: build(2, v), "claim_count", generation_bad
                )
        await self.run_table(
            lambda v: self.queue.fail(1, "worker-1", 1, v), "failure", ENUM_BAD
        )


class ConsolidatorArgumentsTest(ArgumentTestCase):
    def build(self, **overrides):
        arguments = {
            "queue": self.queue,
            "worker": AsyncWorker(),
            "worker_id": "worker-1",
            "worker_timeout_seconds": 10,
            "batch_size": 10,
            "lock_timeout_ms": 3000,
        }
        arguments.update(overrides)
        return Consolidator(
            self.database,
            arguments.pop("queue"),
            arguments.pop("worker"),
            **arguments,
        )

    async def test_the_constructor(self):
        await self.run_table(
            lambda v: Consolidator(v, self.queue, AsyncWorker(), worker_id="w"),
            "database",
            [(None, P.WRONG_TYPE), ("db", P.WRONG_TYPE)],
        )
        await self.run_table(
            lambda v: self.build(queue=v),
            "queue",
            [(None, P.WRONG_TYPE), (object(), P.WRONG_TYPE), ("queue", P.WRONG_TYPE)],
        )
        await self.run_table(
            lambda v: self.build(worker=v),
            "worker",
            [
                (None, P.WRONG_TYPE),
                (object(), P.WRONG_TYPE),
                ("worker", P.WRONG_TYPE),
                (SyncWorker(), P.WRONG_TYPE),  # would block the event loop
            ],
        )
        await self.run_table(
            lambda v: self.build(worker_id=v), "worker_id", WORKER_ID_BAD
        )
        await self.run_table(
            lambda v: self.build(worker_timeout_seconds=v),
            "worker_timeout_seconds",
            [
                (None, P.REQUIRED),
                (True, P.WRONG_TYPE),
                ("5", P.WRONG_TYPE),
                (0, P.OUT_OF_RANGE),
                (-1.5, P.OUT_OF_RANGE),
                (math.nan, P.OUT_OF_RANGE),
                (math.inf, P.OUT_OF_RANGE),
                (limits.MAX_WORKER_TIMEOUT_SECONDS + 1, P.OUT_OF_RANGE),
            ],
        )
        await self.run_table(
            lambda v: self.build(batch_size=v),
            "batch_size",
            int_bad(1, limits.MAX_BATCH_SIZE) + [(0, P.OUT_OF_RANGE)],
        )
        await self.run_table(
            lambda v: self.build(lock_timeout_ms=v),
            "lock_timeout_ms",
            int_bad(1, limits.MAX_LOCK_TIMEOUT_MS) + [(0, P.OUT_OF_RANGE)],
        )

    async def test_the_lease_must_outlive_the_worker_call_with_a_margin(self):
        queue = ConsolidationQueue(self.database, lease_seconds=60)
        self.build(queue=queue, worker_timeout_seconds=30)  # exactly half: fine
        await self.run_table(
            lambda v: self.build(queue=queue, worker_timeout_seconds=v),
            "worker_timeout_seconds",
            [(30.001, P.OUT_OF_RANGE), (60, P.OUT_OF_RANGE), (3600, P.OUT_OF_RANGE)],
        )


class ValidatorsTest(unittest.TestCase):
    """The accepting side of the helpers, at their boundaries."""

    def test_text_is_returned_unchanged_at_the_limit(self):
        for text in ("a", "  padded  ", "日本語", "a" * 10, "line\nbreak"):
            with self.subTest(text):
                self.assertIs(validate_text("t", text, max_chars=10), text)
        with self.assertRaises(InvalidJournalInputError):
            validate_text("t", "a" * 11, max_chars=10)

    def test_integers_at_their_bounds(self):
        self.assertEqual(validate_int("n", 1, low=1, high=3), 1)
        self.assertEqual(validate_int("n", 3, low=1, high=3), 3)

    def test_seconds_accept_int_and_float_and_return_a_float(self):
        for value in (1, 0.5, 3600.0):
            with self.subTest(value):
                result = validate_seconds("s", value, high=3600)
                self.assertIsInstance(result, float)
                self.assertEqual(result, float(value))

    def test_an_enum_is_the_member_or_its_exact_value_and_is_normalised(self):
        self.assertIs(validate_enum("p", Priority.HIGH, Priority), Priority.HIGH)
        self.assertIs(validate_enum("p", "high", Priority), Priority.HIGH)
        self.assertIs(
            validate_enum("r", "assistant", MessageRole), MessageRole.ASSISTANT
        )

    def test_uuids_bools_and_worker_ids(self):
        value = uuid4()
        self.assertIs(validate_uuid("u", value), value)
        self.assertIs(validate_bool("b", True), True)
        self.assertEqual(
            validate_worker_id("w", "host-1.example:9/a@b_c"), "host-1.example:9/a@b_c"
        )
        self.assertIsInstance(value, UUID)
        with self.assertRaises(InvalidJournalInputError):
            validate_bool("b", 1)


if __name__ == "__main__":
    unittest.main()
