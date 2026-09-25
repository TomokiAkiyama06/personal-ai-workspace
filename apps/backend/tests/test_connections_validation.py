"""Argument validation: every public method x argument x bad value, before any I/O.

The service in these tests is wired to a ``Database`` that is NOT configured: any
statement would raise ``DatabaseNotConfiguredError``, so a test that sees
``InvalidConnectionInputError`` proves the arguments were refused before the
database was touched, and one that sees ``DatabaseNotConfiguredError`` proves a
valid call got as far as the database.
"""

import math
import unittest
import uuid
from types import SimpleNamespace
from typing import Any

from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal, SystemRole
from paw_backend.authz.policy import Reason
from paw_backend.connections import (
    UNLIMITED,
    AdapterRegistry,
    AdapterRequest,
    ConnectionKind,
    ConnectionPermissionDeniedError,
    ConnectionRequest,
    ConnectionService,
    ConnectionStatus,
    InputProblem,
    InvalidConnectionInputError,
    QuotaMetric,
    QuotaPeriod,
    UsagePurpose,
)
from paw_backend.connections.store import ConnectionStore
from paw_backend.connections.validation import (
    validate_bool,
    validate_enum,
    validate_handle,
    validate_model,
    validate_page,
    validate_prompt,
    validate_quota_limit,
    validate_seconds,
    validate_uuid,
)
from paw_backend.db import Database, DatabaseNotConfiguredError

from .connections_support import CANARY, FakeAdapter, FakeResolver, handle
from .support import make_settings

ADMIN = Principal(uuid.uuid4(), SystemRole.ADMIN)
USER_ID = uuid.uuid4()


class StrSubclass(str):
    pass


NOT_A_UUID = [
    None,
    1,
    b"00000000-0000-0000-0000-000000000001",
    "not-a-uuid",
    "",
    " " + str(uuid.uuid4()),
    str(uuid.uuid4()).upper(),
    uuid.uuid4().hex,
    "urn:uuid:" + str(uuid.uuid4()),
    "{" + str(uuid.uuid4()) + "}",
    [str(uuid.uuid4())],
    object(),
]


def not_a_member(valid: str) -> list[Any]:
    """Values that are not the member ``valid`` or its exact string."""
    return [
        None,
        1,
        True,
        b"x",
        [valid],
        "",
        valid.upper(),
        valid.title(),
        " " + valid,
        valid + " ",
        valid + "\n",
        StrSubclass(valid),
        ConnectionStatus.CONNECTED,
        UsagePurpose.OTHER,
        object(),
    ]


NOT_A_HANDLE = [
    None,
    1,
    b"x",
    "",
    "sk-" + "ant-" + "a" * 30,  # a plaintext credential is never accepted as a handle
    CANARY,
    "cred_" + "g" * 32,
    "cred_" + "a" * 31,
    "cred_" + "a" * 33,
    "cred_" + "A" * 32,
    "CRED_" + "a" * 32,
    " cred_" + "a" * 32,
    "cred_" + "a" * 32 + "\n",
    "cred-" + "a" * 32,
    StrSubclass(handle(1)),
]
NOT_A_LIMIT = [
    None,
    True,
    False,
    -1,
    10**12 + 1,
    1.5,
    float("nan"),
    float("inf"),
    "5",
    "UNLIMITED",
    "Unlimited",
    " unlimited",
    "unlimited ",
    [],
    object(),
    StrSubclass("unlimited"),
]
NOT_A_LIMIT_OF_PAGE = [0, 201, -1, True, "10", None, 1.5, 10**9]
NOT_AN_OFFSET = [-1, 100_001, True, "0", None, 1.5]


def make_service() -> ConnectionService:
    database = Database(make_settings())  # no URL: every statement fails
    sink = InMemoryAuditSink()
    adapters = AdapterRegistry()
    for kind in ConnectionKind:
        adapters.register(FakeAdapter(kind))
    service = ConnectionService(
        database, Authorizer(sink), sink, adapters, FakeResolver()
    )
    service.audit_events = sink.events  # type: ignore[attr-defined]
    return service


def context() -> Any:
    from .connections_support import PostgresConnectionTestCase

    return PostgresConnectionTestCase.context(uuid.uuid4(), ADMIN.user_id, uuid.uuid4())


def request() -> ConnectionRequest:
    return ConnectionRequest("model-1", UsagePurpose.CODING, "prompt")


# method -> (a valid call as keyword arguments after ``principal``, bad values per
# argument). Every argument of every public method appears here.
CASES: dict[str, tuple[dict[str, Any], dict[str, list[Any]]]] = {
    "connect": (
        {"kind": ConnectionKind.CODEX, "secret_handle": handle(1)},
        {
            "kind": not_a_member("codex"),
            "secret_handle": NOT_A_HANDLE,
        },
    ),
    "replace_credential": (
        {"kind": ConnectionKind.CODEX, "secret_handle": handle(1)},
        {"kind": not_a_member("codex"), "secret_handle": NOT_A_HANDLE},
    ),
    "disable": ({"kind": ConnectionKind.CODEX}, {"kind": not_a_member("codex")}),
    "enable": ({"kind": ConnectionKind.CODEX}, {"kind": not_a_member("codex")}),
    "disconnect": ({"kind": ConnectionKind.CODEX}, {"kind": not_a_member("codex")}),
    "get_connection": (
        {"kind": ConnectionKind.CODEX},
        {"kind": not_a_member("codex")},
    ),
    "list_connections": ({}, {}),
    "availability": ({}, {}),
    "set_quota": (
        {
            "user_id": USER_ID,
            "kind": ConnectionKind.CODEX,
            "metric": QuotaMetric.REQUESTS,
            "period": QuotaPeriod.DAY,
            "limit": 5,
        },
        {
            "user_id": NOT_A_UUID,
            "kind": not_a_member("codex"),
            "metric": not_a_member("requests"),
            "period": not_a_member("day"),
            "limit": NOT_A_LIMIT,
        },
    ),
    "remove_quota": (
        {
            "user_id": USER_ID,
            "kind": ConnectionKind.CODEX,
            "metric": QuotaMetric.REQUESTS,
            "period": QuotaPeriod.DAY,
        },
        {
            "user_id": NOT_A_UUID,
            "kind": not_a_member("codex"),
            "metric": not_a_member("requests"),
            "period": not_a_member("day"),
        },
    ),
    "quota_status": (
        {"user_id": USER_ID, "kind": ConnectionKind.CLAUDE},
        {
            "user_id": NOT_A_UUID,
            "kind": [v for v in not_a_member("claude") if v is not None],
        },
    ),
    "list_usage": (
        {"user_id": USER_ID, "kind": None, "limit": 10, "offset": 0},
        {
            "user_id": NOT_A_UUID,
            "kind": [v for v in not_a_member("claude") if v is not None],
            "limit": NOT_A_LIMIT_OF_PAGE,
            "offset": NOT_AN_OFFSET,
        },
    ),
    "execute": (
        {"context": None, "kind": ConnectionKind.CODEX, "request": None},
        {
            "context": [
                None,
                object(),
                SimpleNamespace(task_id=uuid.uuid4()),
                "context",
                {"task_id": uuid.uuid4()},
            ],
            "kind": not_a_member("codex"),
            "request": [
                None,
                "prompt",
                object(),
                SimpleNamespace(model="m", purpose="coding", prompt="p"),
                AdapterRequest("m", "p", 1.0),
                {"model": "m"},
            ],
        },
    ),
}


def valid_arguments(method: str) -> dict[str, Any]:
    arguments = dict(CASES[method][0])
    if method == "execute":
        arguments["context"] = context()
        arguments["request"] = request()
    return arguments


class EveryArgumentIsCheckedTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = make_service()

    async def test_the_table_covers_every_public_method_of_the_service(self):
        public = {
            name
            for name in dir(ConnectionService)
            if not name.startswith("_") and callable(getattr(ConnectionService, name))
        }
        self.assertEqual(public, set(CASES) | {"check_health"})

    async def test_a_valid_call_gets_as_far_as_the_database(self):
        for method in CASES:
            with self.subTest(method=method):
                with self.assertRaises(DatabaseNotConfiguredError):
                    await getattr(self.service, method)(
                        ADMIN, **valid_arguments(method)
                    )
        with self.assertRaises(DatabaseNotConfiguredError):
            await self.service.check_health(ConnectionKind.CODEX)

    async def test_a_bad_argument_is_refused_before_anything_else_happens(self):
        checked = 0
        for method, (_, bad) in CASES.items():
            for argument, values in bad.items():
                for value in values:
                    arguments = valid_arguments(method)
                    arguments[argument] = value
                    with self.subTest(
                        method=method, argument=argument, value=repr(value)[:40]
                    ):
                        with self.assertRaises(InvalidConnectionInputError) as caught:
                            await getattr(self.service, method)(ADMIN, **arguments)
                        self.assertEqual(caught.exception.field, argument)
                        self.assertIsInstance(caught.exception.problem, InputProblem)
                        checked += 1
        self.assertGreater(checked, 300)
        # Neither the Authorizer nor the audit sink was reached.
        self.assertEqual(self.service.audit_events, [])

    async def test_a_bad_kind_of_check_health_is_refused(self):
        for value in not_a_member("codex"):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(InvalidConnectionInputError) as caught:
                    await self.service.check_health(value)
                self.assertEqual(caught.exception.field, "kind")

    async def test_an_error_names_the_field_but_never_echoes_the_value(self):
        secret_like = "sk-" + "ant-" + "z" * 40
        with self.assertRaises(InvalidConnectionInputError) as caught:
            await self.service.connect(ADMIN, ConnectionKind.CODEX, secret_like)
        text = f"{caught.exception!s} {caught.exception!r} {caught.exception.args}"
        self.assertNotIn(secret_like, text)
        self.assertNotIn("zzzz", text)
        self.assertEqual(str(caught.exception), "Invalid secret_handle: not_a_handle")

    async def test_a_missing_argument_is_a_type_error_not_a_hidden_default(self):
        with self.assertRaises(TypeError):
            await self.service.connect(ADMIN, ConnectionKind.CODEX)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            await self.service.set_quota(ADMIN, USER_ID, ConnectionKind.CODEX)  # type: ignore[call-arg]

    async def test_an_unknown_keyword_is_rejected(self):
        with self.assertRaises(TypeError):
            await self.service.connect(  # type: ignore[call-arg]
                ADMIN, ConnectionKind.CODEX, handle(1), secret="plaintext"
            )

    async def test_the_string_form_of_an_enum_is_normalised_to_the_member(self):
        with self.assertRaises(DatabaseNotConfiguredError):
            await self.service.set_quota(
                ADMIN, str(USER_ID), "codex", "requests", "day", "unlimited"
            )


class ActorIsCheckedFirstTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = make_service()

    async def test_anything_but_a_principal_is_unauthenticated_for_every_method(self):
        class Impostor(Principal):
            pass

        impostors = [
            None,
            "admin",
            object(),
            SimpleNamespace(user_id=uuid.uuid4(), system_role=SystemRole.OWNER),
            Impostor(uuid.uuid4(), SystemRole.OWNER),
            uuid.uuid4(),
        ]
        for method in CASES:
            for impostor in impostors:
                with self.subTest(method=method, actor=type(impostor).__name__):
                    with self.assertRaises(ConnectionPermissionDeniedError) as caught:
                        await getattr(self.service, method)(
                            impostor, **valid_arguments(method)
                        )
                    self.assertEqual(caught.exception.reason, Reason.UNAUTHENTICATED)

    async def test_the_actor_is_refused_before_a_bad_argument_is_looked_at(self):
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.connect(None, "not a kind", "not a handle")  # type: ignore[arg-type]

    async def test_nothing_is_written_for_an_unauthenticated_caller(self):
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.service.connect(None, ConnectionKind.CODEX, handle(1))  # type: ignore[arg-type]
        self.assertEqual(self.service.audit_events, [])


class ConstructionTest(unittest.TestCase):
    def parts(self):
        sink = InMemoryAuditSink()
        return (
            Database(make_settings()),
            Authorizer(sink),
            sink,
            AdapterRegistry(),
            FakeResolver(),
        )

    def test_wrong_collaborators_fail_when_the_service_is_built(self):
        database, authorizer, sink, adapters, resolver = self.parts()
        cases = {
            "authorizer": (database, object(), sink, adapters, resolver),
            "adapters": (database, authorizer, sink, [], resolver),
            "collaborator (sink)": (database, authorizer, object(), adapters, resolver),
            "collaborator (resolver)": (database, authorizer, sink, adapters, object()),
            "database": (object(), authorizer, sink, adapters, resolver),
        }
        for label, arguments in cases.items():
            with self.subTest(label):
                with self.assertRaises(InvalidConnectionInputError):
                    ConnectionService(*arguments)

    def test_a_synchronous_resolver_or_sink_is_refused(self):
        database, authorizer, sink, adapters, _ = self.parts()

        class SyncResolver:
            def resolve(self, handle):
                return None

        class SyncSink:
            def record(self, event):
                return None

        with self.assertRaises(InvalidConnectionInputError):
            ConnectionService(database, authorizer, sink, adapters, SyncResolver())
        with self.assertRaises(InvalidConnectionInputError):
            ConnectionService(
                database, authorizer, SyncSink(), adapters, FakeResolver()
            )

    def test_options_are_checked(self):
        database, authorizer, sink, adapters, resolver = self.parts()
        bad = [
            {"budget": object()},
            {"period_timezone": "Mars/Olympus"},
            {"period_timezone": "utc"},
            {"period_timezone": None},
            {"period_timezone": "../etc/passwd"},
            {"period_timezone": ""},
            {"database_timeout_seconds": 0},
            {"database_timeout_seconds": True},
            {"database_timeout_seconds": 61},
            {"database_timeout_seconds": "5"},
            {"health_timeout_seconds": -1},
            {"health_timeout_seconds": 601},
            {"clock": lambda: None},  # a clock needs allow_explicit_clock
            {"clock": "not callable", "allow_explicit_clock": True},
            {"allow_explicit_clock": "yes"},
        ]
        for options in bad:
            with self.subTest(options=str(options)):
                with self.assertRaises(InvalidConnectionInputError):
                    ConnectionService(
                        database, authorizer, sink, adapters, resolver, **options
                    )

    def test_good_options_are_accepted(self):
        database, authorizer, sink, adapters, resolver = self.parts()
        ConnectionService(
            database,
            authorizer,
            sink,
            adapters,
            resolver,
            period_timezone="Asia/Tokyo",
            database_timeout_seconds=1,
            health_timeout_seconds=2.5,
            clock=lambda: None,
            allow_explicit_clock=True,
        )

    def test_the_store_checks_its_own_arguments(self):
        database = Database(make_settings())
        with self.assertRaises(InvalidConnectionInputError):
            ConnectionStore(object())  # type: ignore[arg-type]
        with self.assertRaises(InvalidConnectionInputError):
            ConnectionStore(database, zone="UTC")  # type: ignore[arg-type]
        with self.assertRaises(InvalidConnectionInputError):
            ConnectionStore(database, timeout_seconds=math.inf)


class ValidatorTest(unittest.TestCase):
    def test_uuid_accepts_a_uuid_and_its_canonical_string_only(self):
        value = uuid.uuid4()
        self.assertEqual(validate_uuid("id", value), value)
        self.assertEqual(validate_uuid("id", str(value)), value)
        self.assertIsInstance(validate_uuid("id", str(value)), uuid.UUID)
        for bad in NOT_A_UUID:
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises(InvalidConnectionInputError) as caught:
                    validate_uuid("id", bad)
                self.assertEqual(caught.exception.problem, InputProblem.NOT_A_UUID)

    def test_enum_accepts_the_member_and_its_exact_string(self):
        self.assertIs(
            validate_enum("k", ConnectionKind.CLAUDE, ConnectionKind),
            ConnectionKind.CLAUDE,
        )
        self.assertIs(
            validate_enum("k", "claude", ConnectionKind), ConnectionKind.CLAUDE
        )
        for bad in not_a_member("claude"):
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises(InvalidConnectionInputError):
                    validate_enum("k", bad, ConnectionKind)

    def test_bool_accepts_only_a_bool(self):
        self.assertIs(validate_bool("b", True), True)
        for bad in (1, 0, "true", None, []):
            with self.assertRaises(InvalidConnectionInputError):
                validate_bool("b", bad)

    def test_handle_accepts_exactly_the_issued_shape(self):
        self.assertEqual(validate_handle("h", handle(255)), handle(255))
        for bad in NOT_A_HANDLE:
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises(InvalidConnectionInputError):
                    validate_handle("h", bad)

    def test_model_names(self):
        for good in (
            "gpt-5",
            "claude-opus-4",
            "vendor/model:tag",
            "a" * 100,
            "A1._:/-",
        ):
            self.assertEqual(validate_model("m", good), good)
        cases = [
            (None, InputProblem.NOT_A_STRING),
            ("", InputProblem.EMPTY),
            ("a" * 101, InputProblem.TOO_LONG),
            ("-leading-dash", InputProblem.INVALID_CHARACTERS),
            (".dot", InputProblem.INVALID_CHARACTERS),
            ("has space", InputProblem.INVALID_CHARACTERS),
            ("new\nline", InputProblem.INVALID_CHARACTERS),
            ("tab\t", InputProblem.INVALID_CHARACTERS),
            ("trailing\n", InputProblem.INVALID_CHARACTERS),
            ("モデル", InputProblem.INVALID_CHARACTERS),
            ("fullwidth１", InputProblem.INVALID_CHARACTERS),
            ("a\x00b", InputProblem.INVALID_CHARACTERS),
            ("prompt: ignore previous", InputProblem.INVALID_CHARACTERS),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(InvalidConnectionInputError) as caught:
                    validate_model("m", value)
                self.assertEqual(caught.exception.problem, problem)

    def test_prompts(self):
        self.assertEqual(validate_prompt("p", "a\nb\tc 日本"), "a\nb\tc 日本")
        self.assertEqual(len(validate_prompt("p", "x" * 500_000)), 500_000)
        cases = [
            (None, InputProblem.NOT_A_STRING),
            (b"x", InputProblem.NOT_A_STRING),
            ("", InputProblem.EMPTY),
            ("x" * 500_001, InputProblem.TOO_LONG),
            ("a\x00", InputProblem.INVALID_CHARACTERS),
            ("a\udc80", InputProblem.INVALID_CHARACTERS),
        ]
        for value, problem in cases:
            with self.subTest(value=repr(value)[:30]):
                with self.assertRaises(InvalidConnectionInputError) as caught:
                    validate_prompt("p", value)
                self.assertEqual(caught.exception.problem, problem)

    def test_seconds(self):
        self.assertEqual(validate_seconds("s", 1, 10), 1.0)
        self.assertEqual(validate_seconds("s", 10, 10), 10.0)
        self.assertEqual(validate_seconds("s", 0.001, 10), 0.001)
        for bad in (True, "1", None, 0, -0.5, 10.001, math.nan, math.inf, -math.inf):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidConnectionInputError):
                    validate_seconds("s", bad, 10)

    def test_quota_limits(self):
        self.assertEqual(validate_quota_limit("l", 0), 0)
        self.assertEqual(validate_quota_limit("l", 10**12), 10**12)
        self.assertIs(validate_quota_limit("l", UNLIMITED), UNLIMITED)
        self.assertIs(validate_quota_limit("l", "unlimited"), UNLIMITED)
        for bad in NOT_A_LIMIT:
            with self.subTest(bad=repr(bad)[:30]):
                with self.assertRaises(InvalidConnectionInputError):
                    validate_quota_limit("l", bad)

    def test_pages(self):
        self.assertEqual(validate_page(1, 0), (1, 0))
        self.assertEqual(validate_page(200, 100_000), (200, 100_000))
        for bad in NOT_A_LIMIT_OF_PAGE:
            with self.assertRaises(InvalidConnectionInputError):
                validate_page(bad, 0)
        for bad in NOT_AN_OFFSET:
            with self.assertRaises(InvalidConnectionInputError):
                validate_page(10, bad)


class RecordValidationTest(unittest.TestCase):
    def test_a_connection_request_normalises_and_checks_every_field(self):
        good = ConnectionRequest("m-1", "review", "text", 5)
        self.assertEqual(
            (good.model, good.purpose, good.prompt, good.timeout_seconds),
            ("m-1", UsagePurpose.REVIEW, "text", 5.0),
        )
        bad = [
            {"model": "a b", "purpose": "coding", "prompt": "p"},
            {"model": "m", "purpose": "Coding", "prompt": "p"},
            {"model": "m", "purpose": "free text about the prompt", "prompt": "p"},
            {"model": "m", "purpose": None, "prompt": "p"},
            {"model": "m", "purpose": "coding", "prompt": ""},
            {"model": "m", "purpose": "coding", "prompt": "p", "timeout_seconds": 0},
            {"model": "m", "purpose": "coding", "prompt": "p", "timeout_seconds": True},
        ]
        for arguments in bad:
            with self.subTest(arguments=str(arguments)[:60]):
                with self.assertRaises(InvalidConnectionInputError):
                    ConnectionRequest(**arguments)

    def test_the_prompt_is_not_in_the_repr_of_a_request(self):
        text = repr(ConnectionRequest("m", "coding", "the private prompt"))
        self.assertNotIn("private", text)

    def test_the_default_timeout_is_ten_minutes(self):
        self.assertEqual(ConnectionRequest("m", "coding", "p").timeout_seconds, 600.0)


if __name__ == "__main__":
    unittest.main()
