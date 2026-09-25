"""The adapter interface, its validation up front and its registry."""

import asyncio
import unittest
from typing import Any

from paw_backend.connections import (
    AdapterFailure,
    AdapterInterfaceError,
    AdapterRegistry,
    AdapterRequest,
    AdapterResult,
    ConnectionKind,
    ConnectionStatus,
    DuplicateAdapterError,
    FailureCode,
    InputProblem,
    InvalidConnectionInputError,
    Secret,
    UnknownAdapterError,
    validate_adapter,
)

from .connections_fakes import FakeAdapter


class GoodAdapter:
    kind = ConnectionKind.CODEX

    async def check_health(self, secret: Secret) -> ConnectionStatus:
        return ConnectionStatus.CONNECTED

    async def run(self, secret: Secret, request: AdapterRequest) -> AdapterResult:
        return AdapterResult("x")


def adapter_with(**members: Any) -> object:
    """A copy of ``GoodAdapter`` whose members are replaced or (``...``) removed."""
    attributes = {
        name: getattr(GoodAdapter, name) for name in ("kind", "check_health", "run")
    }
    for name, value in members.items():
        if value is ...:
            del attributes[name]
        else:
            attributes[name] = value
    return type("Custom", (), attributes)()


class ValidateAdapterTest(unittest.TestCase):
    def test_a_good_adapter_reports_its_kind(self):
        self.assertIs(validate_adapter(GoodAdapter()), ConnectionKind.CODEX)
        self.assertIs(
            validate_adapter(FakeAdapter(ConnectionKind.CLAUDE)), ConnectionKind.CLAUDE
        )

    def test_a_bad_adapter_reports_the_first_failing_member(self):
        async def wrong_arity(self, secret, request, extra):
            return None

        def not_async(self, secret, request):
            return AdapterResult("x")

        cases = {
            "kind missing": (adapter_with(kind=...), "kind"),
            "kind is a plain string": (adapter_with(kind="codex"), "kind"),
            "kind is None": (adapter_with(kind=None), "kind"),
            "kind is another enum": (
                adapter_with(kind=ConnectionStatus.CONNECTED),
                "kind",
            ),
            "kind is an int": (adapter_with(kind=1), "kind"),
            "check_health missing": (adapter_with(check_health=...), "check_health"),
            "check_health is not callable": (
                adapter_with(check_health="x"),
                "check_health",
            ),
            "check_health is not async": (
                adapter_with(check_health=lambda self, secret: None),
                "check_health",
            ),
            "check_health takes none": (
                adapter_with(check_health=lambda self: None),
                "check_health",
            ),
            "run missing": (adapter_with(run=...), "run"),
            "run is not async": (adapter_with(run=not_async), "run"),
            "run takes three": (adapter_with(run=wrong_arity), "run"),
            "run is a string": (adapter_with(run="run"), "run"),
        }
        for label, (adapter, member) in cases.items():
            with self.subTest(label):
                with self.assertRaises(AdapterInterfaceError) as caught:
                    validate_adapter(adapter)
                self.assertEqual(caught.exception.member, member)
                self.assertEqual(
                    str(caught.exception),
                    f"Adapter does not satisfy ConnectionAdapter: {member}",
                )

    def test_things_that_are_not_adapters_are_refused(self):
        for value in (None, 1, "codex", object(), [], {}, type):
            with self.subTest(value=repr(value)):
                with self.assertRaises(AdapterInterfaceError):
                    validate_adapter(value)

    def test_an_async_generator_or_a_partial_signature_is_not_a_coroutine(self):
        async def generator(self, secret, request):
            yield 1

        with self.assertRaises(AdapterInterfaceError) as caught:
            validate_adapter(adapter_with(run=generator))
        self.assertEqual(caught.exception.member, "run")

    def test_only_the_members_are_read_nothing_is_called(self):
        calls = []

        class Tracking(GoodAdapter):
            async def check_health(self, secret):  # pragma: no cover
                calls.append("check_health")

            async def run(self, secret, request):  # pragma: no cover
                calls.append("run")

        validate_adapter(Tracking())
        self.assertEqual(calls, [])

    def test_adapter_code_that_raises_is_a_fixed_error_without_a_chain(self):
        for exception in (
            RuntimeError("secret detail"),
            KeyboardInterrupt(),
            SystemExit(3),
            asyncio.CancelledError(),
            GeneratorExit(),
        ):

            def hostile(error):
                class Hostile:
                    @property
                    def kind(self):
                        raise error

                return Hostile()

            with self.subTest(exception=type(exception).__name__):
                with self.assertRaises(AdapterInterfaceError) as caught:
                    validate_adapter(hostile(exception))
                error = caught.exception
                self.assertEqual(error.member, "kind")
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                self.assertNotIn("secret detail", repr(error))

    def test_a_hostile_method_read_is_a_fixed_error_of_that_member(self):
        class Hostile:
            kind = ConnectionKind.CODEX

            @property
            def check_health(self):
                raise RuntimeError("boom")

        with self.assertRaises(AdapterInterfaceError) as caught:
            validate_adapter(Hostile())
        self.assertEqual(caught.exception.member, "check_health")

    def test_a_hostile_signature_is_a_fixed_error(self):
        class Boom:
            @property
            def __signature__(self):
                raise RuntimeError("boom")

            def __call__(self, *args):  # pragma: no cover
                return None

        with self.assertRaises(AdapterInterfaceError) as caught:
            validate_adapter(adapter_with(check_health=Boom()))
        self.assertEqual(caught.exception.member, "check_health")

    def test_an_adapter_that_asks_to_cancel_the_task_is_rejected_and_retracted(self):
        class Cancelling:
            @property
            def kind(self):
                asyncio.current_task().cancel()
                return ConnectionKind.CODEX

            check_health = GoodAdapter.check_health
            run = GoodAdapter.run

        async def register():
            with self.assertRaises(AdapterInterfaceError) as caught:
                validate_adapter(Cancelling())
            self.assertEqual(caught.exception.member, "kind")
            # The request was taken back: the next await is not cancelled.
            await asyncio.sleep(0)
            return "alive"

        self.assertEqual(asyncio.run(register()), "alive")

    def test_the_error_class_only_names_the_three_members(self):
        with self.assertRaises(ValueError):
            AdapterInterfaceError("name")
        for member in ("kind", "check_health", "run"):
            self.assertEqual(AdapterInterfaceError(member).member, member)


class AdapterRegistryTest(unittest.TestCase):
    def test_it_starts_empty(self):
        registry = AdapterRegistry()
        self.assertEqual(len(registry), 0)
        self.assertEqual(registry.kinds(), ())
        self.assertNotIn(ConnectionKind.CODEX, registry)

    def test_each_kind_takes_one_adapter_and_kinds_come_in_declaration_order(self):
        registry = AdapterRegistry()
        claude, codex = (
            FakeAdapter(ConnectionKind.CLAUDE),
            FakeAdapter(ConnectionKind.CODEX),
        )
        self.assertIs(registry.register(claude), ConnectionKind.CLAUDE)
        self.assertIs(registry.register(codex), ConnectionKind.CODEX)
        self.assertEqual(
            registry.kinds(), (ConnectionKind.CODEX, ConnectionKind.CLAUDE)
        )
        self.assertEqual(len(registry), 2)
        self.assertIs(registry.get(ConnectionKind.CODEX), codex)
        self.assertIs(registry.find(ConnectionKind.CLAUDE), claude)
        self.assertIn(ConnectionKind.CLAUDE, registry)

    def test_a_second_adapter_of_a_kind_is_refused_and_nothing_is_replaced(self):
        registry = AdapterRegistry()
        first = FakeAdapter(ConnectionKind.CODEX)
        registry.register(first)
        with self.assertRaises(DuplicateAdapterError) as caught:
            registry.register(FakeAdapter(ConnectionKind.CODEX))
        self.assertIs(caught.exception.kind, ConnectionKind.CODEX)
        self.assertIs(registry.get(ConnectionKind.CODEX), first)
        self.assertEqual(len(registry), 1)

    def test_a_wrong_adapter_changes_nothing(self):
        registry = AdapterRegistry()
        with self.assertRaises(AdapterInterfaceError):
            registry.register(adapter_with(run=...))  # type: ignore[arg-type]
        self.assertEqual(len(registry), 0)

    def test_an_unknown_kind_is_an_error_for_get_and_none_for_find(self):
        registry = AdapterRegistry()
        with self.assertRaises(UnknownAdapterError) as caught:
            registry.get(ConnectionKind.CLAUDE)
        self.assertIs(caught.exception.kind, ConnectionKind.CLAUDE)
        self.assertIsNone(registry.find(ConnectionKind.CLAUDE))

    def test_a_string_is_not_a_kind_for_get_find_or_contains(self):
        registry = AdapterRegistry()
        registry.register(FakeAdapter(ConnectionKind.CODEX))
        with self.assertRaises(TypeError):
            registry.get("codex")  # type: ignore[arg-type]
        self.assertIsNone(registry.find("codex"))  # type: ignore[arg-type]
        self.assertNotIn("codex", registry)


class AdapterRequestTest(unittest.TestCase):
    def valid(self, **overrides):
        arguments = {"model": "gpt-x", "prompt": "hello", "timeout_seconds": 5}
        arguments.update(overrides)
        return arguments

    def test_a_valid_request_is_normalised(self):
        request = AdapterRequest(**self.valid())
        self.assertEqual((request.model, request.prompt), ("gpt-x", "hello"))
        self.assertEqual(request.timeout_seconds, 5.0)
        self.assertIsInstance(request.timeout_seconds, float)

    def test_the_prompt_is_not_in_the_repr(self):
        self.assertNotIn("hello", repr(AdapterRequest(**self.valid(prompt="hello"))))

    def test_bad_values_are_refused_with_the_field_and_a_closed_problem(self):
        cases = [
            ({"model": None}, "model", InputProblem.NOT_A_STRING),
            ({"model": ""}, "model", InputProblem.EMPTY),
            ({"model": "a b"}, "model", InputProblem.INVALID_CHARACTERS),
            ({"prompt": 1}, "prompt", InputProblem.NOT_A_STRING),
            ({"prompt": ""}, "prompt", InputProblem.EMPTY),
            ({"prompt": "a\x00"}, "prompt", InputProblem.INVALID_CHARACTERS),
            ({"timeout_seconds": True}, "timeout_seconds", InputProblem.NOT_A_NUMBER),
            ({"timeout_seconds": "5"}, "timeout_seconds", InputProblem.NOT_A_NUMBER),
            ({"timeout_seconds": 0}, "timeout_seconds", InputProblem.OUT_OF_RANGE),
            ({"timeout_seconds": -1}, "timeout_seconds", InputProblem.OUT_OF_RANGE),
            (
                {"timeout_seconds": float("nan")},
                "timeout_seconds",
                InputProblem.OUT_OF_RANGE,
            ),
            (
                {"timeout_seconds": float("inf")},
                "timeout_seconds",
                InputProblem.OUT_OF_RANGE,
            ),
            ({"timeout_seconds": 86_401}, "timeout_seconds", InputProblem.OUT_OF_RANGE),
        ]
        for overrides, field, problem in cases:
            with self.subTest(overrides=str(overrides)[:40]):
                with self.assertRaises(InvalidConnectionInputError) as caught:
                    AdapterRequest(**self.valid(**overrides))
                self.assertEqual(
                    (caught.exception.field, caught.exception.problem), (field, problem)
                )


class AdapterResultTest(unittest.TestCase):
    def test_a_result_may_leave_the_tokens_unknown(self):
        result = AdapterResult("answer")
        self.assertEqual((result.input_tokens, result.output_tokens), (None, None))

    def test_the_answer_is_not_in_the_repr(self):
        self.assertNotIn("answer", repr(AdapterResult("answer", 1, 2)))

    def test_bad_results_are_refused(self):
        cases = [
            {"text": 1},
            {"text": None},
            {"text": "a\x00"},
            {"text": "x" * 1_000_001},
            {"text": "ok", "input_tokens": True},
            {"text": "ok", "input_tokens": -1},
            {"text": "ok", "input_tokens": 1.5},
            {"text": "ok", "output_tokens": 10**9 + 1},
            {"text": "ok", "output_tokens": "5"},
        ]
        for arguments in cases:
            with self.subTest(arguments=str(arguments)[:50]):
                with self.assertRaises(InvalidConnectionInputError):
                    AdapterResult(**arguments)

    def test_the_largest_accepted_values(self):
        result = AdapterResult("x" * 1_000_000, 10**9, 0)
        self.assertEqual((result.input_tokens, result.output_tokens), (10**9, 0))


class AdapterFailureTest(unittest.TestCase):
    def test_it_carries_a_failure_code_and_no_message(self):
        error = AdapterFailure(FailureCode.RATE_LIMITED)
        self.assertEqual(AdapterFailure.code.__get__(error), FailureCode.RATE_LIMITED)
        self.assertEqual(str(error), "rate_limited")

    def test_only_a_failure_code_is_accepted(self):
        for value in ("rate_limited", None, 1, ConnectionStatus.EXPIRED):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TypeError):
                    AdapterFailure(value)  # type: ignore[arg-type]

    def test_a_subclass_cannot_divert_the_code(self):
        class Sneaky(AdapterFailure):
            @property
            def code(self):  # pragma: no cover - must never be read
                raise RuntimeError("boom")

        error = Sneaky(FailureCode.EXPIRED)
        self.assertIs(AdapterFailure.code.__get__(error), FailureCode.EXPIRED)


if __name__ == "__main__":
    unittest.main()
