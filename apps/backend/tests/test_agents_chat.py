"""One call of the local model server's chat API (Decision 0083, sections 1 and 4).

``ChatCompletionsClient`` against a fake OpenAI-compatible server
(``httpx.MockTransport``): what it sends, what it reads back (the text, the
reasoning, the tool calls, ``usage``), and how every failure becomes a fixed
failure of the closed table, never the server's words. No network, no GPU.
"""

import asyncio
import json
import unittest

import httpx

from paw_backend.agents import (
    ChatCompletion,
    ChatCompletionsClient,
    ErrorClassifier,
    Failure,
    RuntimeFailure,
    ToolCall,
    Usage,
)
from paw_backend.agents.reasoning import REASONING_FIELD

BASE = "http://127.0.0.1:8000/v1"
MESSAGES = [{"role": "user", "content": "hello"}]
# A secret-shaped text the server echoes in its error body: it must never come
# out of the client.
ECHO = "sk_" + "live_" + "do-not-leak"


def answer(message=None, *, usage=(10, 5), finish="stop", **document):
    body = {
        "id": "x",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"}
                if message is None
                else message,
                "finish_reason": finish,
            }
        ],
        **document,
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        }
    return body


class Server:
    """A fake server: records the requests and answers with ``respond``."""

    def __init__(self, respond=None):
        self.requests: list[httpx.Request] = []
        self.respond = respond or (lambda request: httpx.Response(200, json=answer()))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.respond(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def body(self, index=-1):
        return json.loads(self.requests[index].content)


def client(server, **options):
    return ChatCompletionsClient(
        BASE, model="main", transport=httpx.MockTransport(server), **options
    )


class RequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_non_streaming_request_to_the_chat_api(self):
        server = Server()
        chat = client(server)
        tools = [{"type": "function", "function": {"name": "bash"}}]
        await chat.complete(
            MESSAGES,
            max_tokens=16_384,
            timeout_seconds=30,
            tools=tools,
            tool_choice="auto",
            options={"chat_template_kwargs": {"enable_thinking": True}},
        )
        request = server.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), BASE + "/chat/completions")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(
            server.body(),
            {
                "model": "main",
                "messages": MESSAGES,
                "max_tokens": 16_384,
                "stream": False,
                "tools": tools,
                "tool_choice": "auto",
                "chat_template_kwargs": {"enable_thinking": True},
            },
        )
        await chat.aclose()

    async def test_without_tools_none_are_sent(self):
        server = Server()
        await client(server).complete(MESSAGES, max_tokens=1, timeout_seconds=1)
        self.assertNotIn("tools", server.body())
        self.assertNotIn("tool_choice", server.body())

    async def test_options_cannot_replace_the_fixed_fields(self):
        server = Server()
        chat = client(server)
        for name in ("model", "messages", "max_tokens", "stream", "tools"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                await chat.complete(
                    MESSAGES, max_tokens=1, timeout_seconds=1, options={name: 1}
                )
        self.assertEqual(server.requests, [])

    async def test_wrong_arguments_are_refused_before_anything_is_sent(self):
        server = Server()
        chat = client(server)
        for kwargs, error in (
            (dict(messages=[]), ValueError),
            (dict(messages="hello"), TypeError),
            (dict(messages=[1]), ValueError),
            (dict(max_tokens=0), ValueError),
            (dict(max_tokens=True), ValueError),
            (dict(timeout_seconds=0), ValueError),
            (dict(timeout_seconds=float("nan")), ValueError),
            (dict(timeout_seconds=24 * 3600), ValueError),
            (dict(tools="bash"), TypeError),
            (dict(tool_choice=1), TypeError),
            (dict(messages=[{"content": float("nan")}]), ValueError),
        ):
            arguments = dict(messages=MESSAGES, max_tokens=1, timeout_seconds=1)
            arguments.update(kwargs)
            messages = arguments.pop("messages")
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                await chat.complete(messages, **arguments)
        self.assertEqual(server.requests, [])

    def test_the_setup_is_checked(self):
        for url in (
            "",
            "ftp://host/v1",
            "http://user:pw@host/v1",
            "http://host/v1?x=1",
            "http://host/v1#f",
            "http:///v1",
            "http://host:99999/v1",
            "http://host/v 1",
            1,
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ChatCompletionsClient(url, model="main")
        for model in ("", " main", "x" * 129, "a\nb", None):
            with self.subTest(model=model), self.assertRaises(ValueError):
                ChatCompletionsClient(BASE, model=model)
        with self.assertRaises(TypeError):
            ChatCompletionsClient(BASE, model="main", transport=object())
        with self.assertRaises(ValueError):
            ChatCompletionsClient(BASE, model="main", max_response_bytes=0)

    def test_the_repr_names_the_model_only(self):
        chat = ChatCompletionsClient(BASE + "/", model="main")
        self.assertEqual(repr(chat), "ChatCompletionsClient(model='main')")
        self.assertEqual(chat.model, "main")


class AnswerTest(unittest.IsolatedAsyncioTestCase):
    async def complete(self, body):
        server = Server(lambda request: httpx.Response(200, json=body))
        return await client(server).complete(MESSAGES, max_tokens=1, timeout_seconds=1)

    async def test_the_text_reasoning_tool_calls_and_usage(self):
        message = {
            "role": "assistant",
            "content": None,
            REASONING_FIELD: "let me think",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                }
            ],
        }
        completion = await self.complete(
            answer(message, usage=(120, 30), finish="tool_calls")
        )
        self.assertEqual(
            completion,
            ChatCompletion(
                content=None,
                reasoning="let me think",
                tool_calls=(ToolCall("call_1", "bash", '{"command": "ls"}'),),
                finish_reason="tool_calls",
                usage=Usage(120, 30),
            ),
        )
        self.assertEqual(completion.usage.total, 150)
        # The history's next message, the reasoning included (the policy decides).
        self.assertEqual(
            completion.assistant_message(),
            {
                "role": "assistant",
                "content": None,
                REASONING_FIELD: "let me think",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command": "ls"}',
                        },
                    }
                ],
            },
        )

    async def test_the_newer_reasoning_field_is_read_too(self):
        completion = await self.complete(
            answer({"role": "assistant", "content": "a", "reasoning": "r"})
        )
        self.assertEqual(completion.reasoning, "r")
        self.assertEqual(
            completion.assistant_message(),
            {"role": "assistant", "content": "a", REASONING_FIELD: "r"},
        )

    async def test_an_answer_without_usage_says_none_not_zero(self):
        completion = await self.complete(answer(usage=None))
        self.assertIsNone(completion.usage)
        self.assertEqual(completion.content, "hi")

    async def test_the_text_and_reasoning_are_never_in_the_repr(self):
        completion = await self.complete(
            answer({"role": "assistant", "content": ECHO, REASONING_FIELD: ECHO})
        )
        self.assertNotIn(ECHO, repr(completion))

    async def test_a_broken_answer_is_malformed(self):
        def tool_call(**fields):
            call = {"id": "c", "function": {"name": "bash", "arguments": "{}"}}
            call.update(fields)
            return answer({"role": "assistant", "tool_calls": [call]})

        for body in (
            [],
            {},
            {"choices": []},
            {"choices": [1]},
            {"choices": [{"message": "x"}]},
            answer({"role": "assistant", "content": 1}),
            answer({"role": "assistant", REASONING_FIELD: 1}),
            answer({"role": "assistant", "tool_calls": {}}),
            answer({"role": "assistant", "tool_calls": [1]}),
            tool_call(id=1),
            tool_call(function={"name": "bash", "arguments": {}}),
            tool_call(function={"arguments": "{}"}),
            answer(usage=None) | {"usage": 1},
            answer(usage=(-1, 0)),
            answer(usage=(True, 0)),
            answer() | {"usage": {"prompt_tokens": 1}},
        ):
            with self.subTest(body=body), self.assertRaises(RuntimeFailure) as raised:
                await self.complete(body)
            self.assertIs(raised.exception.failure, Failure.MALFORMED_RESPONSE)

    async def test_an_answer_that_is_not_json_is_malformed(self):
        server = Server(lambda request: httpx.Response(200, content=b"<html>"))
        with self.assertRaises(RuntimeFailure) as raised:
            await client(server).complete(MESSAGES, max_tokens=1, timeout_seconds=1)
        self.assertIs(raised.exception.failure, Failure.MALFORMED_RESPONSE)

    async def test_an_answer_too_large_is_malformed(self):
        body = json.dumps(answer({"role": "assistant", "content": "x" * 5000}))
        server = Server(lambda request: httpx.Response(200, content=body.encode()))
        chat = client(server, max_response_bytes=1024)
        with self.assertRaises(RuntimeFailure) as raised:
            await chat.complete(MESSAGES, max_tokens=1, timeout_seconds=1)
        self.assertIs(raised.exception.failure, Failure.MALFORMED_RESPONSE)


class FailureTest(unittest.IsolatedAsyncioTestCase):
    async def failure(self, respond, **options):
        chat = client(Server(respond))
        arguments = dict(max_tokens=1, timeout_seconds=1)
        arguments.update(options)
        with self.assertRaises(RuntimeFailure) as raised:
            await chat.complete(MESSAGES, **arguments)
        error = raised.exception
        # The server's words never come out: not in the message, not chained.
        self.assertNotIn(ECHO, str(error))
        self.assertNotIn(ECHO, repr(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__ or error.__context__ is None)
        return error.failure

    def status(self, code, text):
        body = {"object": "error", "message": f"{text} ({ECHO})", "code": code}
        return lambda request: httpx.Response(code, json=body)

    async def test_an_unreachable_server_is_unavailable(self):
        def refuse(request):
            raise httpx.ConnectError(f"refused {ECHO}", request=request)

        self.assertIs(await self.failure(refuse), Failure.SERVER_UNAVAILABLE)

    async def test_a_broken_connection_is_unavailable(self):
        def broken(request):
            raise httpx.RemoteProtocolError(f"closed {ECHO}", request=request)

        self.assertIs(await self.failure(broken), Failure.SERVER_UNAVAILABLE)

    async def test_503_is_unavailable(self):
        self.assertIs(
            await self.failure(self.status(503, "loading")),
            Failure.SERVER_UNAVAILABLE,
        )

    async def test_a_cuda_oom_is_out_of_memory(self):
        for code, text in (
            (500, "CUDA out of memory. Tried to allocate 2.00 GiB"),
            (500, "torch.OutOfMemoryError: ..."),
            (503, "CUDA out of memory"),
        ):
            with self.subTest(code=code, text=text):
                self.assertIs(
                    await self.failure(self.status(code, text)),
                    Failure.OUT_OF_MEMORY,
                )
        outcome = ErrorClassifier.outcome(Failure.OUT_OF_MEMORY)
        self.assertEqual(outcome.error_class, "AgentOutOfMemory")

    async def test_the_context_limit_escalates(self):
        for text in (
            "This model's maximum context length is 131072 tokens. However, ...",
            "the prompt is longer than the maximum model length of 131072",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    await self.failure(self.status(400, text)), Failure.CONTEXT_LIMIT
                )
        self.assertTrue(ErrorClassifier.outcome(Failure.CONTEXT_LIMIT).escalate)

    async def test_any_other_error_is_other(self):
        for code, text in ((500, "boom"), (400, "bad request"), (404, "no model")):
            with self.subTest(code=code):
                self.assertIs(
                    await self.failure(self.status(code, text)), Failure.OTHER
                )

    async def test_a_slow_call_times_out_as_a_whole(self):
        async def slow(request):
            await asyncio.sleep(30)
            return httpx.Response(200, json=answer())

        self.assertIs(await self.failure(slow, timeout_seconds=0.05), Failure.TIMEOUT)

    async def test_an_httpx_timeout_is_a_timeout(self):
        def timeout(request):
            raise httpx.ReadTimeout("read", request=request)

        self.assertIs(await self.failure(timeout), Failure.TIMEOUT)

    async def test_a_cancellation_passes_and_closes_the_request(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def hang(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        chat = client(Server(hang))
        call = asyncio.create_task(
            chat.complete(MESSAGES, max_tokens=1, timeout_seconds=60)
        )
        await started.wait()
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()
