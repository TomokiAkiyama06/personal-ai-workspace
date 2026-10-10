"""The Inferred Preference interpreter on the local main model (#38, #208).

Decision 0083 (1 and 11) wires the model interpreter of Decision 0081 for real:

* ``ModelPreferenceInterpreter``: one request without tools through
  ``ChatCompletionsClient``; the person's text and the candidate go as data, the
  answer is the contract's JSON (a reasoning block and a code fence are removed);
* ``compute.ScheduledInterpreter``: the call runs only under an ``INTERACTIVE``
  lease of the scheduler on the main model; without one (or when the lease is
  revoked) it raises ``InterpreterUnavailableError`` at once and the rule
  interpreter answers. Nothing is charged to a task;
* ``create_app(compute=..., local_model=LocalModelSetup(...))`` puts it at
  ``app.state.preference_interpreter`` and closes its client at shutdown.

The model server is a fake (``httpx.MockTransport``) and the GPU is the fake of
``compute_support``: nothing here touches a network or a GPU.
"""

import asyncio
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from paw_backend.agents import (
    ChatCompletionsClient,
    Failure,
    LocalModelSetup,
    ModelPreferenceInterpreter,
    RuntimeFailure,
    build_preference_interpreter,
)
from paw_backend.agents.preferences import SYSTEM_PROMPT
from paw_backend.app import create_app
from paw_backend.compute import (
    DeploymentState,
    InterpreterUnavailableError,
    ResourceClass,
    ScheduledInterpreter,
)
from paw_backend.memory.preferences import (
    InterpretedScope,
    Interpreter,
    MemoryCandidateRef,
    RiskLevel,
    parse_interpreter_output,
)
from paw_backend.memory.preferences.interpretation import check_interpreter

from .compute_support import build, main_spec, memory_spec, settle
from .preference_support import PostgresPreferenceTestCase, requires_postgres
from .support import FakeDatabase, make_settings
from .test_compute_app import QUIET, fake_gpu
from .test_scratch_janitor_lifespan import configured

BASE = "http://127.0.0.1:8000/v1"
CONTRACT_ANSWER = {
    "scope": "user",
    "apply_to": None,
    "rule": "use tabs in Go",
    "exceptions": ["not in YAML"],
    "strength": "default",
    "risk_level": "low",
    "expires_at": None,
}


def completion(content):
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class Server:
    """A fake model server answering ``content`` (or a response) every time."""

    def __init__(self, content=None, *, response=None):
        self.requests = []
        self.content = json.dumps(CONTRACT_ANSWER) if content is None else content
        self.response = response

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        if self.response is not None:
            return self.response
        return httpx.Response(200, json=completion(self.content))

    def transport(self):
        return httpx.MockTransport(self)


def model(server, **options):
    chat = ChatCompletionsClient(BASE, model="main", transport=server.transport())
    options.setdefault("now", lambda: datetime(2026, 10, 10, 3, tzinfo=UTC))
    return ModelPreferenceInterpreter(chat, **options)


class ModelInterpreterTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_request_without_tools_the_text_as_data(self):
        server = Server()
        answer = await model(server).interpret("Goだけ。YAMLは除く", "use tabs")
        self.assertEqual(json.loads(answer), CONTRACT_ANSWER)
        (body,) = server.requests
        self.assertEqual(body["model"], "main")
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)
        self.assertEqual(body["max_tokens"], 2_048)
        # No reasoning block for a short answer.
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        system, user = body["messages"]
        self.assertEqual(system["role"], "system")
        self.assertIn("preference-interpretation-v1", system["content"])
        self.assertIn("Today is 2026-10-10 (UTC)", system["content"])
        self.assertNotIn("{today}", system["content"])
        # The person's words are a JSON document, never part of the instructions.
        self.assertEqual(user["role"], "user")
        self.assertEqual(
            json.loads(user["content"]),
            {"text": "Goだけ。YAMLは除く", "candidate": "use tabs"},
        )
        self.assertNotIn("Goだけ", system["content"])

    async def test_the_answer_is_one_the_contract_parser_takes(self):
        for content in (
            json.dumps(CONTRACT_ANSWER),
            "<think>\nhmm\n</think>\n\n" + json.dumps(CONTRACT_ANSWER),
            "```json\n" + json.dumps(CONTRACT_ANSWER) + "\n```",
            "<think>x</think>```\n" + json.dumps(CONTRACT_ANSWER) + "\n```\n",
        ):
            with self.subTest(content=content[:20]):
                answer = await model(Server(content)).interpret("x", None)
                preference, risk = parse_interpreter_output(answer)
                self.assertIs(preference.scope, InterpretedScope.USER)
                self.assertIs(risk, RiskLevel.LOW)

    async def test_anything_else_is_left_for_the_contract_parser_to_refuse(self):
        answer = await model(Server("Sure! " + json.dumps(CONTRACT_ANSWER))).interpret(
            "x", None
        )
        with self.assertRaises(ValueError):
            parse_interpreter_output(answer)

    async def test_an_answer_without_text_fails(self):
        server = Server()
        server.response = httpx.Response(200, json=completion(None))
        with self.assertRaises(RuntimeFailure) as raised:
            await model(server).interpret("x", None)
        self.assertIs(raised.exception.failure, Failure.MALFORMED_RESPONSE)

    async def test_a_server_failure_is_a_runtime_failure(self):
        server = Server(response=httpx.Response(503, json={"message": "loading"}))
        with self.assertRaises(RuntimeFailure) as raised:
            await model(server).interpret("x", None)
        self.assertIs(raised.exception.failure, Failure.SERVER_UNAVAILABLE)

    async def test_it_checks_what_it_is_given(self):
        server = Server()
        chat = ChatCompletionsClient(BASE, model="main", transport=server.transport())
        with self.assertRaises(TypeError):
            ModelPreferenceInterpreter(object())
        for value in (0, 32_769, True):
            with self.subTest(output=value), self.assertRaises(ValueError):
                ModelPreferenceInterpreter(chat, output_tokens=value)
        for value in (0, 121, float("nan"), True):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                ModelPreferenceInterpreter(chat, timeout_seconds=value)
        with self.assertRaises(TypeError):
            await ModelPreferenceInterpreter(chat).interpret(1, None)
        self.assertEqual(server.requests, [])
        check_interpreter(ModelPreferenceInterpreter(chat))
        self.assertGreater(
            ModelPreferenceInterpreter.prompt_bytes(), len(SYSTEM_PROMPT)
        )


class ScheduledInterpreterTest(unittest.IsolatedAsyncioTestCase):
    class Inner:
        def __init__(self):
            self.calls = []

        async def interpret(self, text, candidate):
            self.calls.append((text, candidate))
            return "{}"

    async def test_it_runs_under_an_interactive_lease_of_the_main_model(self):
        scheduler, *_ = build()
        await scheduler.refresh()
        inner = self.Inner()
        seen = []
        original = scheduler.try_acquire

        async def spy(request):
            seen.append(request)
            return await original(request)

        scheduler.try_acquire = spy
        interpreter = ScheduledInterpreter(
            inner, scheduler, deployment="main", prompt_bytes=4_000, output_tokens=100
        )
        self.assertEqual(await interpreter.interpret("abcd", "efgh"), "{}")
        self.assertEqual(inner.calls, [("abcd", "efgh")])
        (request,) = seen
        self.assertIs(request.resource_class, ResourceClass.INTERACTIVE)
        self.assertEqual(request.deployment, "main")
        # (4,000 + 8 bytes) / 3 bytes per token + the answer's 100 tokens.
        self.assertEqual(request.context_tokens, 1_336 + 100)
        self.assertEqual(scheduler.status().leases[ResourceClass.INTERACTIVE], 0)
        self.assertEqual(repr(interpreter), "ScheduledInterpreter(deployment='main')")

    async def test_without_room_the_rule_interpreter_answers_at_once(self):
        scheduler, *_ = build(
            (main_spec(initial=DeploymentState.UNLOADED), memory_spec()),
            control=False,
        )
        await scheduler.refresh()
        inner = self.Inner()
        interpreter = ScheduledInterpreter(inner, scheduler, deployment="main")
        with self.assertRaises(InterpreterUnavailableError) as raised:
            await interpreter.interpret("x", None)
        self.assertIsNotNone(raised.exception.reason)
        self.assertEqual(inner.calls, [])

    async def test_a_revoked_lease_stops_the_call(self):
        scheduler, *_ = build()
        await scheduler.refresh()
        started = asyncio.Event()
        stopped = asyncio.Event()

        class Hung:
            async def interpret(self, text, candidate):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        interpreter = ScheduledInterpreter(Hung(), scheduler, deployment="main")
        call = asyncio.create_task(interpreter.interpret("x", None))
        await started.wait()
        # Revoke the lease the call holds (as the scheduler does to unload).
        scheduler._revoke(lambda lease: True)
        await settle()
        with self.assertRaises(InterpreterUnavailableError) as raised:
            await call
        self.assertIsNone(raised.exception.reason)
        self.assertTrue(stopped.is_set())
        self.assertEqual(scheduler.status().leases[ResourceClass.INTERACTIVE], 0)

    async def test_it_checks_what_it_is_given(self):
        scheduler, *_ = build()
        with self.assertRaises(TypeError):
            ScheduledInterpreter(object(), scheduler, deployment="main")
        with self.assertRaises(TypeError):
            ScheduledInterpreter(self.Inner(), object(), deployment="main")
        with self.assertRaises(ValueError):
            ScheduledInterpreter(
                self.Inner(),
                scheduler,
                deployment="main",
                resource_class=ResourceClass.EXCLUSIVE,
            )
        for name in ("prompt_bytes", "output_tokens"):
            for value in (-1, True, 1.5, 10**16):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    ScheduledInterpreter(
                        self.Inner(), scheduler, deployment="main", **{name: value}
                    )
        with self.assertRaises(Exception):  # noqa: B017 - the scheduler's own error
            ScheduledInterpreter(self.Inner(), scheduler, deployment="nope")
        check_interpreter(
            ScheduledInterpreter(self.Inner(), scheduler, deployment="main")
        )


class WiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_local_model_answers_under_the_scheduler(self):
        scheduler, *_ = build()
        await scheduler.refresh()
        server = Server()
        interpreter, client = build_preference_interpreter(
            LocalModelSetup(BASE, transport=server.transport()), scheduler
        )
        self.assertIsInstance(interpreter, ScheduledInterpreter)
        answer = await interpreter.interpret("x", "use tabs")
        self.assertEqual(json.loads(answer), CONTRACT_ANSWER)
        self.assertEqual(server.requests[0]["model"], "main")
        await client.aclose()

    def test_the_setup_is_checked_at_start(self):
        scheduler, *_ = build()
        with self.assertRaises(TypeError):
            build_preference_interpreter(object(), scheduler)
        with self.assertRaises(ValueError):
            build_preference_interpreter(LocalModelSetup("ftp://x/v1"), scheduler)
        with self.assertRaises(Exception):  # noqa: B017 - the scheduler's own error
            build_preference_interpreter(
                LocalModelSetup(BASE, deployment="nope"), scheduler
            )
        self.assertNotIn("transport", repr(LocalModelSetup(BASE)))


class AppTest(unittest.IsolatedAsyncioTestCase):
    def test_without_a_local_model_the_rule_interpreter_answers(self):
        setup, *_ = fake_gpu()
        app = create_app(make_settings(), database=FakeDatabase(), compute=setup)
        self.assertIsNone(app.state.preference_interpreter)
        app = create_app(make_settings(), database=FakeDatabase())
        self.assertIsNone(app.state.preference_interpreter)

    def test_a_local_model_needs_compute(self):
        with self.assertRaises(TypeError):
            create_app(
                make_settings(),
                database=FakeDatabase(),
                local_model=LocalModelSetup(BASE),
            )

    async def test_the_application_asks_the_model_and_closes_it_at_shutdown(self):
        setup, *_ = fake_gpu()
        settings, database = configured(**QUIET)
        server = Server()
        app = create_app(
            settings,
            database=database,
            compute=setup,
            local_model=LocalModelSetup(BASE, transport=server.transport()),
        )
        interpreter = app.state.preference_interpreter
        self.assertIsInstance(interpreter, ScheduledInterpreter)
        closed = []
        original = ChatCompletionsClient.aclose

        async def aclose(client):
            closed.append(client)
            await original(client)

        with patch.object(ChatCompletionsClient, "aclose", aclose):
            async with app.router.lifespan_context(app):
                await app.state.compute.scheduler.refresh()
                answer = await interpreter.interpret("x", None)
                self.assertEqual(json.loads(answer), CONTRACT_ANSWER)
        self.assertEqual(len(closed), 1)


@requires_postgres
class ServiceTest(PostgresPreferenceTestCase):
    async def test_the_local_model_answers_first_under_the_scheduler(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        scheduler, *_ = build()
        await scheduler.refresh()
        server = Server()
        interpreter, client = build_preference_interpreter(
            LocalModelSetup(BASE, transport=server.transport()), scheduler
        )
        self.addAsyncCleanup(client.aclose)
        preview = await self.new_preferences(interpreter=interpreter).interpret(
            me, MemoryCandidateRef(memory_id, 1), "Goだけ。YAMLは除く"
        )
        self.assertIs(preview.interpreted_by, Interpreter.MODEL)
        self.assertEqual(preview.preference.rule, "use tabs in Go")
        self.assertEqual(
            json.loads(server.requests[0]["messages"][1]["content"]),
            {"text": "Goだけ。YAMLは除く", "candidate": "use tabs"},
        )

    async def test_without_room_or_with_a_failing_server_the_rules_answer(self):
        me = self.user()
        memory_id = self.seed_candidate(me.user_id, "k", "use tabs")
        busy, *_ = build(
            (main_spec(initial=DeploymentState.UNLOADED), memory_spec()),
            control=False,
        )
        free, *_ = build()
        for scheduler in (busy, free):
            await scheduler.refresh()
        for scheduler, server in (
            (busy, Server()),
            (
                free,
                Server(response=httpx.Response(500, json={"m": "CUDA out of memory"})),
            ),
            (free, Server("not json")),
        ):
            with self.subTest(server=server.content[:10]):
                interpreter, client = build_preference_interpreter(
                    LocalModelSetup(BASE, transport=server.transport()), scheduler
                )
                self.addAsyncCleanup(client.aclose)
                preview = await self.new_preferences(interpreter=interpreter).interpret(
                    me, MemoryCandidateRef(memory_id, 1), "すべてのProjectで"
                )
                self.assertIs(preview.interpreted_by, Interpreter.RULES)
                self.assertIs(preview.preference.scope, InterpretedScope.USER)
        self.assertEqual(busy.status().leases[ResourceClass.INTERACTIVE], 0)


if __name__ == "__main__":
    unittest.main()
