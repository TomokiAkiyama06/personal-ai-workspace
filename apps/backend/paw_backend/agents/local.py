"""The local main model's tool loop (Decision 0083, 2, 5, 7, 8 and 10; #208).

:class:`LocalAgentRuntime` is an orchestrator ``AgentRuntime`` that runs one node
attempt on the local main model (vLLM's OpenAI-compatible API) as a tool loop.
The composition wraps it in ``HybridRuntime`` (``LocalRuntime``): the compute
lease, the GPU time, the placement and ``local_usage`` are the wrapper's.

**What the model sees is the benchmark harness's** (Decision 0074's numbers were
measured that way; Decision 0083, 2): its system prompt (the workspace's place
is the only change), its four tools ``bash`` / ``str_replace`` / ``write_file`` /
``submit`` with the same arguments (``submit`` adds ``changed_files``),
``tool_choice: auto``, tool outputs cut to 12,000 characters (head and tail),
a nudge for an answer without a tool call (the third ends the attempt), 60
steps, ``max_tokens`` 16,384 per answer, 120,000 prompt tokens, and the model's
own sampling defaults. A node without a worktree (Planner, Researcher,
Reviewer, a reading Worker) gets no edit tools and a read-only ``bash``; the
Planner submits a plan (``submit_plan``).

**What runs is the backend's.** Every tool runs through ``NodeTools.call`` and
so the Tool Broker (``paw_backend.sandbox``: ``repo.shell.run`` /
``repo.shell.read``, ``repo.file.write``, ``repo.file.str_replace``; the
sandbox's ``git`` is a shim). ``submit`` is the runtime's own tool:

* a Worker's ``changed_files`` are checked against its branch's changes since its
  base (``repo.git.changes``); a mismatch is told back to the model (three
  times, then ``InvalidNodeResultError``); then the declared paths that are not
  committed yet are committed (``repo.git.commit``, fixed author, the node's
  ``PAW-Node`` trailer). ``NodeResult.commit`` is the branch's head (empty when
  it is still the base); with several repositories each repository's commit is
  an artifact ``commit:<repo id>:<sha>``. ``test_result`` is never the model's.

Around the loop:

* **Reasoning** (Decision 0076, 1): before every request the history goes
  through the ``ReasoningHistoryPolicy`` with this request's prompt estimate
  (:func:`~.reasoning.estimate_prompt_tokens`), and what it returns is the
  history from then on. Reasoning is never logged or stored.
* **The KV reservation** (Decision 0076, 3): before every request the lease
  (``compute.current_lease``) is resized to the prompt estimate plus
  ``max_tokens``; a request longer than the model takes (``CONTEXT_TOO_LONG``)
  ends the attempt as a context limit (a higher rung may solve it).
* **Tokens** (Decision 0083, 5): every answer's ``prompt_tokens +
  completion_tokens`` is charged at once (``NodeBudget.charge(TOKENS)``); an
  answer without ``usage`` is charged an estimate (the request's bytes / 3 plus
  its ``max_tokens``), never 0. GPU time is ``HybridRuntime``'s.
* **Approvals** (Decision 0083, 7): a call that needs one is not waited for: the
  model is told it did not run; the same call later goes with its approval id;
  what is still unapproved at the end is listed in ``unresolved_questions``
  (tool name and approval id only).
* **Time** (Decision 0083, 8): one HTTP call takes at most the time left of
  ``node_seconds`` (the orchestrator's node timeout) and 15 minutes. A
  cancellation passes; ``NodeStopped`` passes.
* **Failures** (Decision 0083, 4): the closed table of ``failures.py``.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from paw_backend.agents.chat import (
    MAX_CALL_SECONDS,
    ChatCompletion,
    ChatCompletionsClient,
    ToolCall,
)
from paw_backend.agents.failures import ErrorClassifier, Failure, RuntimeFailure
from paw_backend.agents.reasoning import (
    MAIN_REASONING_POLICY,
    REASONING_FIELDS,
    ReasoningHistoryPolicy,
    check_reasoning_policy,
    estimate_prompt_tokens,
    reasoning_chars,
)
from paw_backend.agents.toolkit import (
    BASH_TOOL,
    PLAN_TOOL,
    READER_SUBMIT_TOOL,
    STR_REPLACE_TOOL,
    WORKER_SUBMIT_TOOL,
    WRITE_FILE_TOOL,
    NodeToolkit,
    ToolError,
    ToolkitLimits,
    truncate,
)
from paw_backend.compute.domain import Refusal
from paw_backend.compute.errors import ComputeUnavailableError
from paw_backend.compute.limits import BYTES_PER_TOKEN_ESTIMATE
from paw_backend.compute.runtimes import current_lease
from paw_backend.orchestrator.config import RoleAssignment
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import InvalidNodeResultError
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.tasks.queueing import BudgetKind

logger = logging.getLogger(__name__)

# The benchmark harness's prompt (``coding_harness.py`` of paw-bench, the one
# Decision 0074's numbers were measured with). Only the workspace's place is
# filled in.
WORKER_PROMPT = """You are an autonomous software engineering agent working on a Git repository.
The repository is checked out at {workspace} (the current directory of every command).
Environment: Linux container, Python 3.13 with the repository's CI dependencies installed (ruff, jsonschema, fastapi, sqlalchemy, psycopg, ...), git, ripgrep (rg). There is NO network access and no PostgreSQL server (tests that need PostgreSQL are skipped).

Solve the issue given by the user by editing the repository. Work like a careful engineer:
1. Explore the repository to find the relevant code (use the bash tool: ls, rg, sed -n, cat -n ...).
2. Make the change with the edit tools (str_replace for targeted edits, write_file for new files or full rewrites).
3. Run the relevant tests / checks with bash (for example `python -m unittest discover -s benchmarks/tests -p "test_*.py"` or `python -m ruff check benchmarks`) and fix problems.
4. When you are done, call the submit tool with a short summary. Your final repository state (the diff against the starting commit) is what gets evaluated, by hidden tests you cannot see.

Rules: do not delete or disable existing tests or weaken their expectations; do not commit (just leave the changes in the working tree); keep changes focused on the issue. Always use a tool in every turn until you submit."""  # noqa: E501

READER_PROMPT = """You are an autonomous software engineering agent with read-only access to a Git repository.
The repository is checked out at {workspace} (the current directory of every command); you cannot change it.
Environment: Linux container, Python 3.13, git (read-only: status, diff, log, show), ripgrep (rg). There is NO network access.

Do the {role} task given by the user. Explore the repository with the bash tool (ls, rg, sed -n, cat -n ...), then call the submit tool with a summary of what you found. Always use a tool in every turn until you submit."""  # noqa: E501

# What a planner is told about a node's key and title (Decision 0098, 8): the
# backend refuses a plan with one that the result redaction would change
# (``tools.credentials.would_redact``), since the DAG and the board show them as
# they are. Both planner prompts below carry it word for word.
PLAN_TEXT_RULE = (
    "Keep each node's key and title a short plain description: never write an "
    "assignment (NAME=value, name: value), a command-line option with a value "
    "(--option value) or a secret in them, or the plan is refused."
)

PLANNER_PROMPT = """You are the planner of an autonomous software engineering team, with read-only access to a Git repository.
The repository is checked out at {workspace} (the current directory of every command); you cannot change it.
Environment: Linux container, Python 3.13, git (read-only: status, diff, log, show), ripgrep (rg). There is NO network access.

Break the task given by the user into nodes of a plan and call the submit_plan tool with it: {{"nodes": [{{"key": "impl", "role": "worker", "title": "...", "goal": "...", "depends_on": []}}, ...]}}. Roles: worker (changes the code), researcher, reviewer. Keep each node's key and title a short plain description: never write an assignment (NAME=value, name: value), a command-line option with a value (--option value) or a secret in them, or the plan is refused. Explore the repository with bash first when it helps. Always use a tool in every turn until you submit."""  # noqa: E501

# The planner of a read-only task (issue #279, Decision 0094, 1 A): the backend
# refuses a plan with a worker node, so the planner is told the roles it may use.
READ_ONLY_PLANNER_PROMPT = """You are the planner of an autonomous software engineering team, with read-only access to a Git repository.
The repository is checked out at {workspace} (the current directory of every command); you cannot change it.
Environment: Linux container, Python 3.13, git (read-only: status, diff, log, show), ripgrep (rg). There is NO network access.

This task only reads: nothing may be changed, and a plan with a worker node is refused. Break the task given by the user into nodes of a plan and call the submit_plan tool with it: {{"nodes": [{{"key": "read", "role": "researcher", "title": "...", "goal": "...", "depends_on": []}}, ...]}}. Roles: researcher (finds and reads), reviewer (checks what was found). Keep each node's key and title a short plain description: never write an assignment (NAME=value, name: value), a command-line option with a value (--option value) or a secret in them, or the plan is refused. Explore the repository with bash first when it helps. Always use a tool in every turn until you submit."""  # noqa: E501

NUDGE = (
    "Continue. Use the tools to work on the task, and call submit when you are done."
)

# Decision 0099, 4: the planner may name the agent each node starts on, among
# the ones the backend allows for its role (``NodeAssignment.agent_choices``).
# The roles each planner prompt names, in the order it names them.
_PLANNED_ROLES = (NodeRole.WORKER, NodeRole.RESEARCHER, NodeRole.REVIEWER)
_READ_ONLY_PLANNED_ROLES = (NodeRole.RESEARCHER, NodeRole.REVIEWER)


def agent_choices_text(
    choices: Mapping[NodeRole, RoleAssignment] | None, *, read_only: bool
) -> str:
    """The planner prompt's paragraph on a node's ``agent`` (Decision 0099, 4):
    the agents each role the prompt names may be assigned and its default
    (``""`` without choices). The same text for every planner runtime (the CLI
    runtimes use :func:`node_prompts` too)."""
    if not choices:
        return ""
    roles = _READ_ONLY_PLANNED_ROLES if read_only else _PLANNED_ROLES
    parts = []
    for role in roles:
        entry = choices.get(role)
        if entry is None:
            continue
        allowed = " or ".join(entry.allowed)
        default = f" (default {entry.default})" if entry.default else ""
        parts.append(f"{role.value}: {allowed}{default}")
    if not parts:
        return ""
    return (
        '\n\nA node may name the agent that runs it in "agent" (one of the '
        "labels below, as a string); leave it out for its role's default. The "
        "agents each role may have: " + "; ".join(parts) + ". Any other agent "
        "refuses the whole plan."
    )


@dataclass(frozen=True, slots=True)
class LocalAgentLimits:
    """The loop's limits: the benchmark's (Decision 0083, 2 and 8)."""

    max_steps: int = 60
    max_tokens: int = 16_384
    max_prompt_tokens: int = 120_000
    output_chars: int = 12_000
    nudges: int = 3
    malformed_answers: int = 3
    submit_corrections: int = 3
    # The orchestrator's node timeout (Decision 0021, 3): the outer limit.
    node_seconds: float = 1800.0
    call_seconds: float = MAX_CALL_SECONDS
    # The sandbox's command limit, as the benchmark reported it.
    command_seconds: int = 300

    def __post_init__(self) -> None:
        for name in (
            "max_steps",
            "max_tokens",
            "max_prompt_tokens",
            "output_chars",
            "nudges",
            "malformed_answers",
            "submit_corrections",
            "command_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(name)
        for name in ("node_seconds", "call_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int | float) or not 0 < value <= 86_400:
                raise ValueError(name)


def _strict_json(text: str) -> object:
    """JSON as ``benchmarks.json_input`` reads it: no duplicate key, no NaN."""

    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def constant(name):
        raise ValueError(f"{name} is not JSON")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


@dataclass(slots=True)
class _Attempt:
    """The state of one node attempt's loop (in memory only)."""

    assignment: NodeAssignment
    toolkit: NodeToolkit
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    started: float
    previous_usage: Any = None
    new_tool_results: list[str] = field(default_factory=list)
    nudges: int = 0
    malformed: int = 0


class LocalAgentRuntime:
    """The local main model's tool loop (see the module)."""

    def __init__(
        self,
        client: ChatCompletionsClient,
        *,
        reasoning_history: ReasoningHistoryPolicy = MAIN_REASONING_POLICY,
        limits: LocalAgentLimits | None = None,
        options: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(client, ChatCompletionsClient):
            raise TypeError("client must be a ChatCompletionsClient")
        check_reasoning_policy(reasoning_history)
        self._client = client
        self._policy = reasoning_history
        self._limits = limits or LocalAgentLimits()
        self._options = dict(options or {})
        self._clock = clock

    def __repr__(self) -> str:
        return f"LocalAgentRuntime(model={self._client.model!r})"

    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome:
        try:
            return await self._run(assignment)
        except RuntimeFailure as failure:
            return ErrorClassifier.outcome(failure.failure)
        except InvalidNodeResultError:
            return ErrorClassifier.outcome(Failure.INVALID_RESULT)
        except Exception as error:
            # ``NodeStopped`` passes (``from_error`` raises it again); anything
            # else is an adapter error, its text never read.
            return ErrorClassifier.from_error(error)

    # -- the loop --------------------------------------------------------------------

    def _start(self, assignment: NodeAssignment) -> _Attempt:
        limits = self._limits
        toolkit = NodeToolkit(
            assignment,
            ToolkitLimits(
                output_chars=limits.output_chars,
                submit_corrections=limits.submit_corrections,
                command_seconds=limits.command_seconds,
            ),
        )
        system, user = node_prompts(toolkit)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return _Attempt(assignment, toolkit, messages, toolkit.tools, self._clock())

    async def _run(self, assignment: NodeAssignment) -> NodeOutcome:
        attempt = self._start(assignment)
        limits = self._limits
        for _ in range(limits.max_steps):
            answer = await self._ask(attempt)
            message: dict[str, Any] = {
                "role": "assistant",
                "content": answer.content or "",
            }
            if answer.reasoning:
                # As the harness: the reasoning under both of its names.
                for name in REASONING_FIELDS:
                    message[name] = answer.reasoning
            if answer.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in answer.tool_calls
                ]
            attempt.messages.append(message)
            if not answer.tool_calls:
                attempt.nudges += 1
                if attempt.nudges >= limits.nudges:
                    raise RuntimeFailure(Failure.NO_TOOL_CALLS)
                attempt.messages.append({"role": "user", "content": NUDGE})
                # Part of the next prompt too (the estimate adds what was added
                # after the last answer).
                attempt.new_tool_results.append(NUDGE)
                continue
            outcome = await self._calls(attempt, answer.tool_calls)
            if outcome is not None:
                return outcome
        raise RuntimeFailure(Failure.STEP_LIMIT)

    async def _ask(self, attempt: _Attempt) -> ChatCompletion:
        """One request: the history through the reasoning policy, the lease
        resized, the call, the tokens charged."""
        limits = self._limits
        estimate = estimate_prompt_tokens(
            attempt.previous_usage, attempt.new_tool_results
        )
        prepared = self._policy.prepare(attempt.messages, prompt_tokens=estimate)
        # The estimate was of the history before the policy: what it dropped (an
        # older step's reasoning) is no longer sent, nor reserved.
        dropped = _reasoning_bytes(attempt.messages) - _reasoning_bytes(prepared)
        attempt.messages = prepared  # what was sent is the history from now on
        attempt.new_tool_results = []
        # The whole request's input: the messages and the tool definitions.
        size = len(
            json.dumps([prepared, attempt.tools], ensure_ascii=False).encode("utf-8")
        )
        if estimate is None:
            prompt = _tokens(size)
        else:
            prompt = max(0, estimate - max(0, dropped) // BYTES_PER_TOKEN_ESTIMATE)
        if prompt > limits.max_prompt_tokens:
            raise RuntimeFailure(Failure.CONTEXT_LIMIT)
        lease = current_lease()
        if lease is not None:
            try:
                lease.resize(prompt + limits.max_tokens)
            except ComputeUnavailableError as error:
                if error.reason is Refusal.CONTEXT_TOO_LONG:
                    raise RuntimeFailure(Failure.CONTEXT_LIMIT) from None
                raise
        left = limits.node_seconds - (self._clock() - attempt.started)
        if left <= 0:
            raise RuntimeFailure(Failure.TIMEOUT)
        logger.debug(
            "Local request: %d messages, %d reasoning characters",
            len(prepared),
            reasoning_chars(prepared),
        )
        answer = await self._client.complete(
            prepared,
            max_tokens=limits.max_tokens,
            timeout_seconds=min(left, limits.call_seconds),
            tools=attempt.tools,
            tool_choice="auto",
            options=self._options or None,
        )
        used = (
            answer.usage.total
            if answer.usage is not None
            else _tokens(size) + limits.max_tokens
        )
        await attempt.assignment.budget.charge(BudgetKind.TOKENS, used)
        attempt.previous_usage = answer.usage
        if answer.usage is not None and (
            answer.usage.prompt_tokens > limits.max_prompt_tokens
        ):
            raise RuntimeFailure(Failure.CONTEXT_LIMIT)
        return answer

    async def _calls(
        self, attempt: _Attempt, calls: Sequence[ToolCall]
    ) -> NodeOutcome | None:
        malformed = False
        outcome: NodeOutcome | None = None
        for call in calls:
            try:
                arguments = _strict_json(call.arguments or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as error:
                malformed = True
                result = f"Tool error: {type(error).__name__}: {error}"
            else:
                try:
                    result, outcome = await attempt.toolkit.run(call.name, arguments)
                except ToolError as error:
                    result = f"Tool error: {error}"
            attempt.messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )
            attempt.new_tool_results.append(result)
            if outcome is not None:
                break
        attempt.malformed = attempt.malformed + 1 if malformed else 0
        if attempt.malformed >= self._limits.malformed_answers:
            raise RuntimeFailure(Failure.MALFORMED_RESPONSE)
        return outcome


def _reasoning_bytes(messages: Sequence[Mapping[str, Any]]) -> int:
    """The UTF-8 bytes of the reasoning in ``messages`` (each message counted
    once, whatever name its reasoning has)."""
    total = 0
    for message in messages:
        for name in REASONING_FIELDS:
            text = message.get(name)
            if isinstance(text, str):
                total += len(text.encode("utf-8"))
                break
    return total


def _tokens(size_bytes: int) -> int:
    return math.ceil(size_bytes / BYTES_PER_TOKEN_ESTIMATE)


def node_prompts(toolkit: NodeToolkit) -> tuple[str, str]:
    """The system prompt and the task of a node attempt (the benchmark
    harness's; the CLI runtimes use the same, Decision 0083, 2)."""
    assignment = toolkit.assignment
    role = assignment.role
    place = toolkit.workspace.describe()
    if role is NodeRole.PLANNER and assignment.read_only_task:
        system = READ_ONLY_PLANNER_PROMPT.format(workspace=place) + agent_choices_text(
            assignment.agent_choices, read_only=True
        )
    elif role is NodeRole.PLANNER:
        system = PLANNER_PROMPT.format(workspace=place) + agent_choices_text(
            assignment.agent_choices, read_only=False
        )
    elif toolkit.writes:
        system = WORKER_PROMPT.format(workspace=place)
    else:
        system = READER_PROMPT.format(workspace=place, role=role.value)
    return system, _task_text(assignment)


def _task_text(assignment: NodeAssignment) -> str:
    """The user message: the node's title, goal, input and the results of the
    nodes it depends on (JSON)."""
    parts = [f"# {assignment.title}", "", assignment.goal]
    if assignment.input:
        parts += [
            "",
            "## Input",
            json.dumps(dict(assignment.input), ensure_ascii=False),
        ]
    if assignment.upstream:
        upstream = {
            key: {
                "summary": result.summary,
                "changed_files": list(result.changed_files),
                "discovered_facts": list(result.discovered_facts),
            }
            for key, result in assignment.upstream.items()
        }
        parts += [
            "",
            "## Results of the nodes this one depends on",
            json.dumps(upstream, ensure_ascii=False),
        ]
    return "\n".join(parts)


__all__ = [
    "BASH_TOOL",
    "PLAN_TOOL",
    "READER_SUBMIT_TOOL",
    "STR_REPLACE_TOOL",
    "WORKER_SUBMIT_TOOL",
    "WRITE_FILE_TOOL",
    "LocalAgentLimits",
    "LocalAgentRuntime",
    "node_prompts",
    "truncate",
]
