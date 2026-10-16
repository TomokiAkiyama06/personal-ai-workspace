"""The local main model's tool loop (Decision 0083, 2, 5, 7, 8 and 10): ``local.py``.

A fake OpenAI-compatible server (``httpx.MockTransport``) answers from a
script, and fake ``NodeTools`` / ``NodeBudget`` record what the runtime asks
the Tool Broker and charges. What is pinned:

* the requests are the benchmark harness's (prompt, tools, ``tool_choice``,
  ``max_tokens``), ``submit`` with ``changed_files`` being the only change;
* every tool goes through ``NodeTools.call`` with the worktree's host paths;
  paths outside ``/workspace`` or into ``.git`` are refused first;
* the limits (steps, nudges, malformed answers, the prompt limit) end the
  attempt with the failure table's rows;
* tokens are charged per answer (an estimate when ``usage`` is missing), the
  reasoning policy is given the prompt estimate and its answer is kept, the
  compute lease is resized before every request;
* approvals are not waited for; ``submit`` is checked against the branch's
  changes and the uncommitted declared paths are committed.
"""

import asyncio
import json
import unittest
import uuid

import httpx

from paw_backend.agents.chat import ChatCompletionsClient
from paw_backend.agents.local import (
    BASH_TOOL,
    NUDGE,
    WORKER_PROMPT,
    LocalAgentLimits,
    LocalAgentRuntime,
    truncate,
)
from paw_backend.agents.reasoning import KeepAllReasoning
from paw_backend.compute.domain import Refusal
from paw_backend.compute.errors import ComputeUnavailableError
from paw_backend.compute.runtimes import _LOCAL_LEASE
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.orchestrator.runtime import NodeAssignment
from paw_backend.orchestrator.workspaces import NodeWorktree
from paw_backend.tasks.queueing import BudgetKind
from paw_backend.tools import (
    BrokerDecision,
    BrokerReason,
    ExecutionStatus,
    ToolOutcome,
    Verdict,
)

BASE = "http://127.0.0.1:18080/v1"
REPO = uuid.UUID("11111111-1111-1111-1111-111111111111")
WORKTREE = "/home/alice/workspaces/.paw-worktrees/t/1/r/a"
CHECKOUT = "/home/alice/workspaces/project/repo"
BASE_COMMIT = "b" * 40


def call(name, arguments, call_id=None):
    return {
        "id": call_id or f"call-{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments
            if isinstance(arguments, str)
            else json.dumps(arguments),
        },
    }


def answer(*calls, content=None, reasoning="thinking", usage=(1000, 100)):
    message = {"role": "assistant", "content": content, "reasoning_content": reasoning}
    if calls:
        message["tool_calls"] = list(calls)
    document = {"choices": [{"message": message, "finish_reason": "stop"}]}
    if usage is not None:
        document["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1]}
    return document


class Server:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.bodies = []

    async def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        if not self.answers:
            return httpx.Response(200, json=answer())
        item = self.answers.pop(0)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def allowed(result):
    return ToolOutcome(
        BrokerDecision(Verdict.ALLOW, BrokerReason.SCOPED_AUTO, uuid.uuid4()),
        ExecutionStatus.COMPLETED,
        result=result,
    )


class Tools:
    """Fake ``NodeTools``: a handler per tool name, every call recorded."""

    def __init__(self, **handlers):
        self.handlers = handlers
        self.calls = []

    async def call(self, tool, arguments, *, approval_id=None):
        self.calls.append((tool, dict(arguments), approval_id))
        handler = self.handlers.get(tool)
        if handler is None:
            return allowed({"exit_code": 0, "output": "", "truncated": False})
        result = handler(dict(arguments), approval_id)
        if isinstance(result, ToolOutcome):
            return result
        return allowed(result)


class Budget:
    def __init__(self, stop_after=None):
        self.charges = []
        self.stop_after = stop_after

    async def charge(self, kind, amount):
        self.charges.append((kind, amount))
        if self.stop_after is not None and len(self.charges) >= self.stop_after:
            raise NodeStopped(StopReason.BUDGET_EXCEEDED)

    async def remaining(self):
        return {}


def assignment(tools, budget=None, *, role=NodeRole.WORKER, worktree=True):
    worktrees = (
        {REPO: NodeWorktree(REPO, WORKTREE, "paw/t/1/a", base_commit=BASE_COMMIT)}
        if worktree
        else {}
    )
    return NodeAssignment(
        task_id=uuid.uuid4(),
        node_key="a",
        role=role,
        title="Fix the parser",
        goal="Make the parser accept empty input.",
        input={"issue": 7},
        upstream={},
        agent="local",
        attempt=1,
        approach=0,
        tools=tools,
        budget=budget or Budget(),
        worktrees=worktrees,
        roots={REPO: WORKTREE if worktree else CHECKOUT},
    )


def runtime(server, **options):
    client = ChatCompletionsClient(
        BASE, model="main", transport=httpx.MockTransport(server)
    )
    options.setdefault("reasoning_history", KeepAllReasoning())
    return LocalAgentRuntime(client, **options)


def submit(summary="done", changed=()):
    return call("submit", {"summary": summary, "changed_files": list(changed)})


def changes(changed=(), uncommitted=(), head=BASE_COMMIT, nested=None):
    answer = {
        "base": BASE_COMMIT,
        "head": head,
        "changed": list(changed),
        "uncommitted": list(uncommitted),
    }
    if nested is not None:
        answer["nested"] = list(nested)
    return lambda arguments, approval: answer


class RequestShapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_requests_are_the_benchmarks(self):
        server = Server(answer(submit()))
        tools = Tools(**{"repo.git.changes": changes()})
        outcome = await runtime(server).run_node(assignment(tools))
        self.assertIsNotNone(outcome.result, outcome.error_class)
        body = server.bodies[0]
        self.assertEqual(body["model"], "main")
        self.assertEqual(body["max_tokens"], 16_384)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertFalse(body["stream"])
        system, user = body["messages"]
        self.assertEqual(
            system["content"], WORKER_PROMPT.format(workspace="/workspace")
        )
        self.assertIn("Make the parser accept empty input.", user["content"])
        names = [t["function"]["name"] for t in body["tools"]]
        self.assertEqual(names, ["bash", "str_replace", "write_file", "submit"])
        self.assertEqual(body["tools"][0], BASH_TOOL)
        submit_tool = body["tools"][3]["function"]["parameters"]
        self.assertEqual(submit_tool["required"], ["summary", "changed_files"])
        # No sampling option of our own: the model's defaults.
        self.assertNotIn("temperature", body)

    async def test_the_harness_prompt_is_kept_word_for_word(self):
        prompt = WORKER_PROMPT.format(workspace="/workspace")
        self.assertTrue(prompt.startswith("You are an autonomous software engineering"))
        self.assertIn(
            "The repository is checked out at /workspace (the current", prompt
        )
        self.assertIn(
            "do not commit (just leave the changes in the working tree)", prompt
        )

    def test_truncate_keeps_head_and_tail(self):
        text = "a" * 10 + "b" * 10
        self.assertEqual(truncate(text, 100), text)
        self.assertEqual(
            truncate(text, 10), "aaaaa\n... [10 characters truncated] ...\nbbbbb"
        )


class ToolTest(unittest.IsolatedAsyncioTestCase):
    async def run_calls(self, *calls, tools=None, **options):
        server = Server(answer(*calls), answer(submit()))
        tools = tools or Tools()
        tools.handlers.setdefault("repo.git.changes", changes())
        outcome = await runtime(server, **options).run_node(assignment(tools))
        tool_messages = [
            m for m in server.bodies[-1]["messages"] if m["role"] == "tool"
        ]
        return outcome, tools, [m["content"] for m in tool_messages]

    async def test_bash_runs_through_the_broker_in_the_worktree(self):
        tools = Tools(
            **{
                "repo.shell.run": lambda a, _: {
                    "exit_code": 2,
                    "output": "x" * 20_000,
                    "truncated": False,
                }
            }
        )
        _, tools, results = await self.run_calls(
            call("bash", {"command": "make test"}), tools=tools
        )
        self.assertEqual(
            tools.calls[0],
            ("repo.shell.run", {"workdir": WORKTREE, "command": "make test"}, None),
        )
        self.assertTrue(results[0].startswith("exit code: 2\nxxxx"))
        self.assertIn("[8000 characters truncated]", results[0])

    async def test_a_timed_out_command_says_so(self):
        tools = Tools(
            **{"repo.shell.run": lambda a, _: {"exit_code": 124, "output": "slow"}}
        )
        _, _, results = await self.run_calls(
            call("bash", {"command": "sleep 999"}), tools=tools
        )
        self.assertEqual(
            results[0], "exit code: 124\nslow\n(command timed out after 300 s)"
        )

    async def test_edits_map_the_workspace_to_the_worktree(self):
        tools = Tools(
            **{
                "repo.file.str_replace": lambda a, _: {
                    "error": "occurrences",
                    "count": 2,
                },
                "repo.file.write": lambda a, _: {"ok": True, "chars": 2},
            }
        )
        _, tools, results = await self.run_calls(
            call("str_replace", {"path": "src/a.py", "old_str": "x", "new_str": "y"}),
            call("write_file", {"path": "/workspace/new.py", "content": "hi"}),
            call("write_file", {"path": "../outside", "content": "x"}),
            call("write_file", {"path": "/etc/passwd", "content": "x"}),
            call(
                "str_replace", {"path": ".git/config", "old_str": "a", "new_str": "b"}
            ),
            tools=tools,
        )
        self.assertEqual(
            [c[:2] for c in tools.calls[:2]],
            [
                (
                    "repo.file.str_replace",
                    {"path": f"{WORKTREE}/src/a.py", "old_str": "x", "new_str": "y"},
                ),
                ("repo.file.write", {"path": f"{WORKTREE}/new.py", "content": "hi"}),
            ],
        )
        self.assertEqual(
            results,
            [
                "Tool error: ValueError: old_str occurs 2 times in src/a.py "
                "(must be exactly 1)",
                "Wrote /workspace/new.py (2 characters).",
                "Tool error: ValueError: path is outside /workspace",
                "Tool error: ValueError: path is outside /workspace",
                "Tool error: ValueError: editing .git is not allowed",
            ],
        )

    async def test_approvals_are_not_waited_for(self):
        approval = uuid.uuid4()

        def needs(arguments, approval_id):
            return ToolOutcome(
                BrokerDecision(
                    Verdict.NEEDS_APPROVAL,
                    BrokerReason.APPROVAL_REQUIRED,
                    uuid.uuid4(),
                    approval_id=approval,
                ),
                ExecutionStatus.NOT_EXECUTED,
            )

        tools = Tools(**{"repo.shell.run": needs})
        bash = {"command": "rm -rf build"}
        outcome, tools, results = await self.run_calls(
            call("bash", bash), call("bash", bash), tools=tools
        )
        self.assertIn(str(approval), results[0])
        self.assertEqual(tools.calls[1][2], approval)  # the same call goes with it
        self.assertEqual(
            outcome.result.unresolved_questions,
            (f"approval needed: repo.shell.run (approval {approval})",),
        )

    async def test_an_approval_that_ended_is_no_longer_listed(self):
        approval = uuid.uuid4()

        def answer_for(arguments, approval_id):
            if approval_id is None:
                return ToolOutcome(
                    BrokerDecision(
                        Verdict.NEEDS_APPROVAL,
                        BrokerReason.APPROVAL_REQUIRED,
                        uuid.uuid4(),
                        approval_id=approval,
                    ),
                    ExecutionStatus.NOT_EXECUTED,
                )
            return ToolOutcome(
                BrokerDecision(Verdict.DENY, BrokerReason.UNKNOWN_TOOL, uuid.uuid4()),
                ExecutionStatus.NOT_EXECUTED,
            )

        bash = {"command": "rm -rf build"}
        outcome, _, _ = await self.run_calls(
            call("bash", bash),
            call("bash", bash),
            tools=Tools(**{"repo.shell.run": answer_for}),
        )
        self.assertEqual(outcome.result.unresolved_questions, ())

    async def test_a_refusal_is_told_to_the_model(self):
        def refused(arguments, approval_id):
            return ToolOutcome(
                BrokerDecision(Verdict.DENY, BrokerReason.UNKNOWN_TOOL, uuid.uuid4()),
                ExecutionStatus.NOT_EXECUTED,
            )

        _, _, results = await self.run_calls(
            call("bash", {"command": "ls"}),
            tools=Tools(**{"repo.shell.run": refused}),
        )
        self.assertEqual(
            results[0],
            "Tool error: PermissionError: the call was refused (unknown_tool)",
        )

    async def test_unknown_tools_and_bad_json_are_tool_errors(self):
        _, _, results = await self.run_calls(
            call("rm", {}), call("bash", "{not json"), call("bash", '{"a": 1, "a": 2}')
        )
        self.assertEqual(results[0], "Tool error: ValueError: unknown tool 'rm'")
        self.assertTrue(results[1].startswith("Tool error: JSONDecodeError"))
        self.assertTrue(results[2].startswith("Tool error: ValueError: duplicate key"))


class LimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_answers_without_a_tool_end_the_attempt(self):
        server = Server(answer(), answer(), answer())
        outcome = await runtime(server).run_node(assignment(Tools()))
        self.assertEqual(
            (outcome.error_class, outcome.escalate), ("RuntimeError", True)
        )
        self.assertEqual(server.bodies[1]["messages"][-1]["content"], NUDGE)

    async def test_the_step_limit(self):
        server = Server(*[answer(call("bash", {"command": "ls"})) for _ in range(3)])
        limits = LocalAgentLimits(max_steps=3)
        outcome = await runtime(server, limits=limits).run_node(assignment(Tools()))
        self.assertEqual(outcome.error_class, "RuntimeError")
        self.assertEqual(outcome.message, "step_limit")
        self.assertTrue(outcome.escalate)

    async def test_three_malformed_answers_in_a_row(self):
        bad = answer(call("bash", "nope"))
        server = Server(bad, bad, bad)
        outcome = await runtime(server).run_node(assignment(Tools()))
        self.assertEqual((outcome.error_class, outcome.escalate), ("ValueError", False))

    async def test_the_prompt_limit(self):
        server = Server(answer(call("bash", {"command": "ls"}), usage=(120_001, 10)))
        outcome = await runtime(server).run_node(assignment(Tools()))
        self.assertEqual(outcome.message, "context_limit")
        self.assertTrue(outcome.escalate)

    async def test_server_failures_are_the_tables(self):
        server = Server(httpx.Response(503))
        outcome = await runtime(server).run_node(assignment(Tools()))
        self.assertEqual(
            (outcome.error_class, outcome.escalate), ("ConnectionError", False)
        )
        server = Server(httpx.Response(500, json={"error": "CUDA out of memory"}))
        outcome = await runtime(server).run_node(assignment(Tools()))
        self.assertEqual(outcome.error_class, "AgentOutOfMemory")

    async def test_a_sandbox_that_cannot_start_ends_the_attempt(self):
        def failed(arguments, approval_id):
            return ToolOutcome(
                BrokerDecision(Verdict.ALLOW, BrokerReason.SCOPED_AUTO, uuid.uuid4()),
                ExecutionStatus.FAILED,
                error_type="SandboxUnavailableError",
            )

        server = Server(answer(call("bash", {"command": "ls"})))
        outcome = await runtime(server).run_node(
            assignment(Tools(**{"repo.shell.run": failed}))
        )
        self.assertEqual(outcome.error_class, "PermissionError")

    async def test_an_oom_kill_in_the_sandbox_ends_the_attempt(self):
        # Decision 0092, 5: the wrapper's oom=1 (the executor's
        # SandboxOutOfMemoryError) is AgentOutOfMemory, no escalation.
        def killed(arguments, approval_id):
            return ToolOutcome(
                BrokerDecision(Verdict.ALLOW, BrokerReason.SCOPED_AUTO, uuid.uuid4()),
                ExecutionStatus.FAILED,
                error_type="SandboxOutOfMemoryError",
            )

        server = Server(answer(call("bash", {"command": "ls"})))
        outcome = await runtime(server).run_node(
            assignment(Tools(**{"repo.shell.run": killed}))
        )
        self.assertEqual(
            (outcome.error_class, outcome.message, outcome.escalate),
            ("AgentOutOfMemory", "out_of_memory", False),
        )

    async def test_node_stopped_passes(self):
        server = Server(answer(call("bash", {"command": "ls"})))
        with self.assertRaises(NodeStopped):
            await runtime(server).run_node(assignment(Tools(), Budget(stop_after=1)))


class AccountingTest(unittest.IsolatedAsyncioTestCase):
    async def test_tokens_are_charged_per_answer(self):
        server = Server(
            answer(call("bash", {"command": "ls"}), usage=(1000, 50)),
            answer(submit(), usage=None),
        )
        budget = Budget()
        tools = Tools(**{"repo.git.changes": changes()})
        await runtime(server).run_node(assignment(tools, budget))
        self.assertEqual(budget.charges[0], (BudgetKind.TOKENS, 1050))
        # No usage: an estimate (the request / 3 and its max_tokens), never 0.
        kind, estimate = budget.charges[1]
        self.assertEqual(kind, BudgetKind.TOKENS)
        self.assertGreater(estimate, 16_384)

    async def test_the_policy_gets_the_estimate_and_its_answer_is_kept(self):
        seen = []

        class Dropping:
            def prepare(self, messages, *, prompt_tokens=None):
                seen.append(prompt_tokens)
                kept = [dict(m) for m in messages]
                for message in kept:
                    message.pop("reasoning_content", None)
                    message.pop("reasoning", None)
                return kept

        tools = Tools(
            **{
                "repo.shell.run": lambda a, _: {"exit_code": 0, "output": "abc"},
                "repo.git.changes": changes(),
            }
        )
        server = Server(
            answer(call("bash", {"command": "ls"}), usage=(1000, 50)), answer(submit())
        )
        await runtime(server, reasoning_history=Dropping()).run_node(assignment(tools))
        self.assertIsNone(seen[0])
        # previous prompt + completion + new tool result bytes / 3
        result = "exit code: 0\nabc"
        self.assertEqual(seen[1], 1000 + 50 + len(result.encode()) // 3)
        sent = server.bodies[1]["messages"]
        self.assertFalse(any("reasoning_content" in m for m in sent))

    async def test_the_reservation_leaves_out_the_dropped_reasoning(self):
        # Codex review of #224 (P1): the estimate is of the history before the
        # policy; the reasoning it dropped is neither sent nor reserved.
        class DropAll:
            def prepare(self, messages, *, prompt_tokens=None):
                kept = [dict(m) for m in messages]
                if prompt_tokens is not None:
                    for message in kept:
                        message.pop("reasoning_content", None)
                        message.pop("reasoning", None)
                return kept

        class Lease:
            sizes = []

            def resize(self, tokens):
                self.sizes.append(tokens)

        lease = Lease()
        reasoning = "r" * 30_000
        tools = Tools(**{"repo.git.changes": changes()})
        server = Server(
            answer(
                call("bash", {"command": "ls"}), reasoning=reasoning, usage=(2000, 100)
            ),
            answer(submit()),
        )
        token = _LOCAL_LEASE.set(lease)
        try:
            await runtime(server, reasoning_history=DropAll()).run_node(
                assignment(tools)
            )
        finally:
            _LOCAL_LEASE.reset(token)
        without = 2000 + 100 + len(b"exit code: 0\n") // 3 - 30_000 // 3
        self.assertEqual(lease.sizes[1], max(0, without) + 16_384)

    async def test_a_nudge_counts_in_the_next_estimate(self):
        # Codex review of #224 (P2): the nudge is part of the next prompt.
        seen = []

        class Recording:
            def prepare(self, messages, *, prompt_tokens=None):
                seen.append(prompt_tokens)
                return [dict(m) for m in messages]

        tools = Tools(**{"repo.git.changes": changes()})
        server = Server(answer(usage=(1000, 50)), answer(submit()))
        await runtime(server, reasoning_history=Recording()).run_node(assignment(tools))
        self.assertEqual(seen[1], 1000 + 50 + len(NUDGE.encode()) // 3)

    async def test_the_lease_is_resized_before_every_request(self):
        class Lease:
            def __init__(self, refuse=False):
                self.sizes = []
                self.refuse = refuse

            def resize(self, tokens):
                if self.refuse:
                    raise ComputeUnavailableError(Refusal.CONTEXT_TOO_LONG)
                self.sizes.append(tokens)

        lease = Lease()
        tools = Tools(**{"repo.git.changes": changes()})
        server = Server(
            answer(call("bash", {"command": "ls"}), usage=(2000, 100)), answer(submit())
        )
        token = _LOCAL_LEASE.set(lease)
        try:
            await runtime(server).run_node(assignment(tools))
        finally:
            _LOCAL_LEASE.reset(token)
        self.assertEqual(len(lease.sizes), 2)
        self.assertGreater(lease.sizes[0], 16_384)
        self.assertGreaterEqual(lease.sizes[1], 2100 + 16_384)

        token = _LOCAL_LEASE.set(Lease(refuse=True))
        try:
            outcome = await runtime(Server()).run_node(assignment(Tools()))
        finally:
            _LOCAL_LEASE.reset(token)
        self.assertEqual((outcome.message, outcome.escalate), ("context_limit", True))


class SubmitTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_mismatch_is_corrected_then_committed(self):
        committed = []
        tools = Tools(
            **{
                "repo.git.changes": changes(
                    changed=("a.py", "b.py"), uncommitted=("b.py",), head="c" * 40
                ),
                "repo.git.commit": lambda a, _: (
                    committed.append(json.loads(a["paths"])) or {"commit": "d" * 40}
                ),
            }
        )
        server = Server(
            answer(submit(changed=["a.py"])),
            answer(submit(changed=["a.py", "/workspace/b.py"])),
        )
        outcome = await runtime(server).run_node(assignment(tools))
        correction = server.bodies[1]["messages"][-1]["content"]
        self.assertIn("not declared: b.py", correction)
        self.assertEqual(committed, [["b.py"]])
        result = outcome.result
        self.assertEqual(result.changed_files, ("a.py", "b.py"))
        self.assertEqual(result.commit, "d" * 40)
        self.assertIsNone(result.test_result)
        commit_call = [c for c in tools.calls if c[0] == "repo.git.commit"][0]
        self.assertEqual(commit_call[1]["workdir"], WORKTREE)

    async def test_a_worker_reports_its_findings_too(self):
        tools = Tools(**{"repo.git.changes": changes()})
        found = call(
            "submit",
            {
                "summary": "s",
                "changed_files": [],
                "discovered_facts": ["the parser is in src/p.py"],
                "unresolved_questions": ["is the API stable?"],
                "confidence": 0.8,
            },
        )
        server = Server(answer(found))
        outcome = await runtime(server).run_node(assignment(tools))
        properties = server.bodies[0]["tools"][3]["function"]["parameters"][
            "properties"
        ]
        self.assertIn("discovered_facts", properties)
        self.assertEqual(
            outcome.result.discovered_facts, ("the parser is in src/p.py",)
        )
        self.assertEqual(outcome.result.unresolved_questions, ("is the API stable?",))
        self.assertEqual(outcome.result.confidence, 0.8)

    async def test_too_many_changed_files_are_corrected_before_any_commit(self):
        many = [f"f{i}.py" for i in range(201)]
        tools = Tools(**{"repo.git.changes": changes(changed=many, uncommitted=many)})
        server = Server(*[answer(submit(changed=many)) for _ in range(4)])
        outcome = await runtime(server).run_node(assignment(tools))
        self.assertEqual(outcome.error_class, "InvalidNodeResultError")
        self.assertIn("201 files changed", server.bodies[1]["messages"][-1]["content"])
        self.assertFalse(any(c[0] == "repo.git.commit" for c in tools.calls))

    async def test_an_unacceptable_result_is_corrected_before_any_commit(self):
        # Codex review of #224 (P2): the whole NodeResult is checked first.
        long_path = "d/" * 300 + "f.py"
        tools = Tools(
            **{
                "repo.git.changes": changes(
                    changed=(long_path,), uncommitted=(long_path,)
                )
            }
        )
        server = Server(*[answer(submit(changed=[long_path])) for _ in range(4)])
        outcome = await runtime(server).run_node(assignment(tools))
        self.assertEqual(outcome.error_class, "InvalidNodeResultError")
        self.assertIn(
            "the result is not acceptable", server.bodies[1]["messages"][-1]["content"]
        )
        self.assertFalse(any(c[0] == "repo.git.commit" for c in tools.calls))

    async def test_a_nested_repository_is_corrected_before_any_commit(self):
        # Decision 0090 (risks): the sandbox's real git can make a repository
        # inside the worktree; a node's result never contains one.
        nested = {"changed": ["a.py", "sub/"], "uncommitted": ["a.py", "sub/"]}
        answers = iter(
            [
                {**nested, "nested": ["sub/"]},
                {**nested, "nested": ["sub/"]},
                {"changed": ["a.py"], "uncommitted": ["a.py"], "nested": []},
            ]
        )
        committed = []
        tools = Tools(
            **{
                "repo.git.changes": lambda a, _: {
                    "base": BASE_COMMIT,
                    "head": BASE_COMMIT,
                    **next(answers),
                },
                "repo.git.commit": lambda a, _: (
                    committed.append(json.loads(a["paths"])) or {"commit": "d" * 40}
                ),
            }
        )
        server = Server(
            answer(submit(changed=["a.py"])),
            answer(submit(changed=["a.py", "sub/"])),
            answer(submit(changed=["a.py"])),
        )
        outcome = await runtime(server).run_node(assignment(tools))
        for body in server.bodies[1:]:
            correction = body["messages"][-1]["content"]
            self.assertIn("nested git repository", correction)
            self.assertIn("sub/", correction)
        self.assertEqual(committed, [["a.py"]])
        self.assertEqual(outcome.result.changed_files, ("a.py",))

    async def test_nested_repositories_are_listed_with_the_repository(self):
        tools = Tools(
            **{
                "repo.git.changes": changes(
                    changed=("x/",), uncommitted=("x/",), nested=("x/",)
                )
            }
        )
        server = Server(*[answer(submit(changed=["x/"])) for _ in range(4)])
        outcome = await runtime(server).run_node(assignment(tools))
        self.assertEqual(outcome.error_class, "InvalidNodeResultError")
        self.assertIn(
            "nested git repository: x/", server.bodies[1]["messages"][-1]["content"]
        )
        self.assertFalse(any(c[0] == "repo.git.commit" for c in tools.calls))

    async def test_nothing_to_commit_and_nothing_changed(self):
        tools = Tools(**{"repo.git.changes": changes()})
        outcome = await runtime(Server(answer(submit()))).run_node(assignment(tools))
        self.assertIsNone(outcome.result.commit)
        self.assertEqual(outcome.result.changed_files, ())
        self.assertFalse(any(c[0] == "repo.git.commit" for c in tools.calls))

    async def test_three_corrections_then_an_invalid_result(self):
        tools = Tools(**{"repo.git.changes": changes(changed=("a.py",))})
        server = Server(*[answer(submit()) for _ in range(4)])
        outcome = await runtime(server).run_node(assignment(tools))
        self.assertEqual(outcome.error_class, "InvalidNodeResultError")

    async def test_a_reader_submits_its_findings(self):
        server = Server(
            answer(
                call(
                    "submit",
                    {"summary": "found", "discovered_facts": ["x"], "confidence": 0.5},
                )
            )
        )
        tools = Tools()
        outcome = await runtime(server).run_node(
            assignment(tools, role=NodeRole.REVIEWER, worktree=False)
        )
        names = [t["function"]["name"] for t in server.bodies[0]["tools"]]
        self.assertEqual(names, ["bash", "submit"])
        self.assertEqual(outcome.result.discovered_facts, ("x",))
        self.assertEqual(outcome.result.confidence, 0.5)

    async def test_a_readers_bash_is_read_only(self):
        server = Server(
            answer(call("bash", {"command": "ls"})),
            answer(call("submit", {"summary": "s"})),
        )
        tools = Tools()
        await runtime(server).run_node(
            assignment(tools, role=NodeRole.RESEARCHER, worktree=False)
        )
        self.assertEqual(
            tools.calls[0][:2],
            ("repo.shell.read", {"workdir": CHECKOUT, "command": "ls"}),
        )

    async def test_the_planner_submits_a_plan(self):
        plan = {"nodes": [{"key": "impl", "role": "worker", "title": "t", "goal": "g"}]}
        server = Server(answer(call("submit_plan", {"summary": "p", "plan": plan})))
        outcome = await runtime(server).run_node(
            assignment(Tools(), role=NodeRole.PLANNER, worktree=False)
        )
        self.assertEqual(outcome.plan, plan)
        self.assertEqual(outcome.result.summary, "p")


class CancelTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancellation_passes(self):
        started = asyncio.Event()

        async def slow(request):
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(runtime(slow).run_node(assignment(Tools())))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
