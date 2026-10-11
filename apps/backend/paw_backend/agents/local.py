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

import copy
import json
import logging
import math
import posixpath
import time
import uuid
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
from paw_backend.compute.domain import Refusal
from paw_backend.compute.errors import ComputeUnavailableError
from paw_backend.compute.limits import BYTES_PER_TOKEN_ESTIMATE
from paw_backend.compute.runtimes import current_lease
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import InvalidNodeResultError, ResultReason
from paw_backend.orchestrator.limits import (
    MAX_CHANGED_FILES,
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    MAX_SUMMARY_CHARS,
)
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.sandbox.tools import (
    REPO_FILE_STR_REPLACE,
    REPO_FILE_WRITE,
    REPO_GIT_CHANGES,
    REPO_GIT_COMMIT,
    REPO_SHELL_READ,
    REPO_SHELL_RUN,
)
from paw_backend.tasks.queueing import BudgetKind
from paw_backend.tools import ExecutionStatus, ToolOutcome, Verdict

logger = logging.getLogger(__name__)

WORKSPACE = "/workspace"

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

PLANNER_PROMPT = """You are the planner of an autonomous software engineering team, with read-only access to a Git repository.
The repository is checked out at {workspace} (the current directory of every command); you cannot change it.
Environment: Linux container, Python 3.13, git (read-only: status, diff, log, show), ripgrep (rg). There is NO network access.

Break the task given by the user into nodes of a plan and call the submit_plan tool with it: {{"nodes": [{{"key": "impl", "role": "worker", "title": "...", "goal": "...", "depends_on": []}}, ...]}}. Roles: worker (changes the code), researcher, reviewer. Explore the repository with bash first when it helps. Always use a tool in every turn until you submit."""  # noqa: E501

NUDGE = (
    "Continue. Use the tools to work on the task, and call submit when you are done."
)
NOT_APPROVED = (
    "Tool error: this call needs a human's approval and was not run "
    "(approval {approval}). Continue with other work, or call it again later."
)


def _function(name: str, description: str, properties: dict, required: list[str]):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


BASH_TOOL = _function(
    "bash",
    "Run a bash command in /workspace and return its exit code and output "
    "(stdout+stderr, truncated). Timeout 300 s. No network.",
    {"command": {"type": "string", "description": "The bash command to run."}},
    ["command"],
)
STR_REPLACE_TOOL = _function(
    "str_replace",
    "Replace exactly one occurrence of old_str with new_str in a file. Fails if "
    "old_str occurs zero or more than one time.",
    {
        "path": {
            "type": "string",
            "description": "File path relative to /workspace (or absolute under "
            "/workspace).",
        },
        "old_str": {"type": "string"},
        "new_str": {"type": "string"},
    },
    ["path", "old_str", "new_str"],
)
WRITE_FILE_TOOL = _function(
    "write_file",
    "Create or overwrite a file with the given content.",
    {"path": {"type": "string"}, "content": {"type": "string"}},
    ["path", "content"],
)
WORKER_SUBMIT_TOOL = _function(
    "submit",
    "Finish the task. Call this once the change is complete and checked.",
    {
        "summary": {"type": "string"},
        # The only change to the benchmark's tools (Decision 0083, 2 and 10).
        "changed_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every file you changed, created or deleted, relative "
            "to /workspace.",
        },
        # What a later node may use (optional, as the reader's submit).
        "discovered_facts": {"type": "array", "items": {"type": "string"}},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    ["summary", "changed_files"],
)
_FINDINGS = {
    "summary": {"type": "string"},
    "discovered_facts": {"type": "array", "items": {"type": "string"}},
    "unresolved_questions": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "number"},
}
READER_SUBMIT_TOOL = _function(
    "submit", "Finish the task with what you found.", _FINDINGS, ["summary"]
)
PLAN_TOOL = _function(
    "submit_plan",
    "Finish with the plan of the task.",
    {"summary": {"type": "string"}, "plan": {"type": "object"}},
    ["summary", "plan"],
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


def truncate(text: str, limit: int) -> str:
    """The benchmark's cut: head and tail, the number of characters left out."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half]
        + f"\n... [{len(text) - limit} characters truncated] ...\n"
        + text[-half:]
    )


class _ToolError(Exception):
    """A tool call the model gets back as ``Tool error: ...``."""


@dataclass(slots=True)
class _Repository:
    repo_id: uuid.UUID
    root: str
    target: str
    writable: bool
    base: str | None = None


class _Workspace:
    """Where the node's repositories are, as the model sees them (one repository
    at ``/workspace``, several at ``/workspace/<repository id>``: the sandbox's
    own mount places, ``sandbox.plan.mount_target``)."""

    def __init__(self, assignment: NodeAssignment) -> None:
        roots = dict(assignment.roots)
        for repo_id, worktree in assignment.worktrees.items():
            roots[repo_id] = worktree.path
        single = len(roots) == 1
        self.repositories: dict[uuid.UUID, _Repository] = {}
        for repo_id in sorted(roots, key=str):
            worktree = assignment.worktrees.get(repo_id)
            self.repositories[repo_id] = _Repository(
                repo_id,
                roots[repo_id],
                WORKSPACE if single else f"{WORKSPACE}/{repo_id}",
                worktree is not None,
                None if worktree is None else worktree.base_commit,
            )

    @property
    def single(self) -> bool:
        return len(self.repositories) == 1

    def first(self) -> _Repository | None:
        return next(iter(self.repositories.values()), None)

    def describe(self) -> str:
        if self.single or not self.repositories:
            return WORKSPACE
        lines = [f"{WORKSPACE}/<repository id>/ (one directory per repository:"]
        lines.extend(f"  {r.target}" for r in self.repositories.values())
        return "\n".join(lines) + ")"

    def resolve(self, path: object) -> tuple[_Repository, str, str]:
        """``(repository, host path, path relative to its root)`` of a path the
        model gave, or ``_ToolError`` (the benchmark's refusals)."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise _ToolError("ValueError: path is outside /workspace")
        if path.startswith(WORKSPACE):
            path = path[len(WORKSPACE) :].lstrip("/")
        elif path.startswith("/"):
            raise _ToolError("ValueError: path is outside /workspace")
        parts = path.split("/")
        if any(part == ".." for part in parts):
            raise _ToolError("ValueError: path is outside /workspace")
        relative = posixpath.normpath(path) if path else ""
        if relative in ("", "."):
            raise _ToolError("ValueError: path is outside /workspace")
        if self.single:
            repository = self.first()
        else:
            head, _, relative = relative.partition("/")
            repository = next(
                (r for r in self.repositories.values() if str(r.repo_id) == head),
                None,
            )
            if repository is None or not relative:
                raise _ToolError("ValueError: path is outside /workspace")
        assert repository is not None
        if ".git" in relative.split("/"):
            raise _ToolError("ValueError: editing .git is not allowed")
        return repository, f"{repository.root}/{relative}", relative


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


def _texts(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _ToolError(f"ValueError: {label} must be a list of strings")
    if len(value) > MAX_LIST_ITEMS or any(len(v) > MAX_ITEM_CHARS for v in value):
        raise _ToolError(f"ValueError: {label} is too long")
    return tuple(value)


@dataclass(slots=True)
class _Attempt:
    """The state of one node attempt's loop (in memory only)."""

    assignment: NodeAssignment
    workspace: _Workspace
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    started: float
    previous_usage: Any = None
    new_tool_results: list[str] = field(default_factory=list)
    nudges: int = 0
    malformed: int = 0
    corrections: int = 0
    # Approval ids by the call they belong to, and the ones still waiting.
    approvals: dict[tuple[str, str], uuid.UUID] = field(default_factory=dict)
    waiting: dict[uuid.UUID, str] = field(default_factory=dict)


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
        workspace = _Workspace(assignment)
        role = assignment.role
        writes = role is NodeRole.WORKER and any(
            r.writable for r in workspace.repositories.values()
        )
        place = workspace.describe()
        if role is NodeRole.PLANNER:
            prompt = PLANNER_PROMPT.format(workspace=place)
            tools = [BASH_TOOL, PLAN_TOOL]
        elif writes:
            prompt = WORKER_PROMPT.format(workspace=place)
            tools = [BASH_TOOL, STR_REPLACE_TOOL, WRITE_FILE_TOOL, WORKER_SUBMIT_TOOL]
        else:
            prompt = READER_PROMPT.format(workspace=place, role=role.value)
            tools = [BASH_TOOL, READER_SUBMIT_TOOL]
        if not workspace.repositories:
            tools = [t for t in tools if t["function"]["name"] != "bash"]
        elif not workspace.single:
            tools = [_with_repository(t) if t is BASH_TOOL else t for t in tools]
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": _task_text(assignment)},
        ]
        return _Attempt(assignment, workspace, messages, tools, self._clock())

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
                    result, outcome = await self._tool(attempt, call.name, arguments)
                except _ToolError as error:
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

    # -- one tool --------------------------------------------------------------------

    async def _tool(
        self, attempt: _Attempt, name: str, arguments: dict[str, Any]
    ) -> tuple[str, NodeOutcome | None]:
        names = {t["function"]["name"] for t in attempt.tools}
        if name not in names:
            raise _ToolError(f"ValueError: unknown tool {name!r}")
        if name == "bash":
            return await self._bash(attempt, arguments), None
        if name == "str_replace":
            return await self._str_replace(attempt, arguments), None
        if name == "write_file":
            return await self._write_file(attempt, arguments), None
        if name == "submit_plan":
            return self._submit_plan(attempt, arguments)
        return await self._submit(attempt, arguments)

    async def _broker(
        self, attempt: _Attempt, tool: str, arguments: dict[str, Any]
    ) -> object:
        """Run a tool through the broker: its result, or ``_ToolError`` for a
        call that did not run (approval, refusal, executor failure)."""
        key = (tool, json.dumps(arguments, sort_keys=True))
        approval = attempt.approvals.get(key)
        outcome = await attempt.assignment.tools.call(
            tool, arguments, approval_id=approval
        )
        if not isinstance(outcome, ToolOutcome):
            raise _ToolError("RuntimeError: the tool gave no answer")
        decision = outcome.decision
        if outcome.status is ExecutionStatus.COMPLETED:
            if approval is not None:
                attempt.waiting.pop(approval, None)
            return outcome.result
        if decision.verdict is Verdict.NEEDS_APPROVAL:
            if decision.approval_id is not None:
                attempt.approvals[key] = decision.approval_id
                attempt.waiting[decision.approval_id] = tool
            raise _ToolError(
                NOT_APPROVED.format(approval=decision.approval_id or "pending")
            )
        if approval is not None:
            # The approval ended (rejected, revoked, expired): nothing waits.
            attempt.waiting.pop(approval, None)
            attempt.approvals.pop(key, None)
        if outcome.status is ExecutionStatus.FAILED:
            if outcome.error_type in ("SandboxUnavailableError", "PermissionError"):
                raise RuntimeFailure(Failure.SANDBOX_UNAVAILABLE)
            raise _ToolError(f"{outcome.error_type or 'Error'}: the tool failed")
        raise _ToolError(
            f"PermissionError: the call was refused ({decision.reason.value})"
        )

    def _bash_place(self, attempt: _Attempt, arguments: dict[str, Any]):
        workspace = attempt.workspace
        if workspace.single:
            return workspace.first()
        wanted = arguments.get("repository")
        for repository in workspace.repositories.values():
            if str(repository.repo_id) == wanted:
                return repository
        raise _ToolError("ValueError: name the repository (its id) to run in")

    async def _bash(self, attempt: _Attempt, arguments: dict[str, Any]) -> str:
        command = arguments.get("command")
        if not isinstance(command, str):
            raise _ToolError("KeyError: 'command'")
        repository = self._bash_place(attempt, arguments)
        writes = repository.writable and attempt.assignment.role is NodeRole.WORKER
        result = await self._broker(
            attempt,
            REPO_SHELL_RUN if writes else REPO_SHELL_READ,
            {"workdir": repository.root, "command": command},
        )
        if not isinstance(result, Mapping) or not isinstance(
            result.get("exit_code"), int
        ):
            raise _ToolError("RuntimeError: the command gave no answer")
        code, output = result["exit_code"], str(result.get("output", ""))
        if code == 124:
            output += f"\n(command timed out after {self._limits.command_seconds} s)"
        return f"exit code: {code}\n{truncate(output, self._limits.output_chars)}"

    def _writable(self, attempt: _Attempt, path: object) -> tuple[str, str]:
        repository, host, _ = attempt.workspace.resolve(path)
        if not repository.writable:
            raise _ToolError("PermissionError: this repository is read-only")
        return host, str(path)

    async def _str_replace(self, attempt: _Attempt, arguments: dict[str, Any]) -> str:
        old, new = arguments.get("old_str"), arguments.get("new_str")
        if not isinstance(old, str) or not isinstance(new, str):
            raise _ToolError("KeyError: 'old_str'")
        host, shown = self._writable(attempt, arguments.get("path"))
        result = await self._broker(
            attempt,
            REPO_FILE_STR_REPLACE,
            {"path": host, "old_str": old, "new_str": new},
        )
        if isinstance(result, Mapping) and result.get("ok") is True:
            return f"Edited {shown}."
        error = result.get("error") if isinstance(result, Mapping) else None
        if error == "occurrences":
            count = result.get("count")
            raise _ToolError(
                f"ValueError: old_str occurs {count} times in {shown} "
                "(must be exactly 1)"
            )
        if error == "not_found":
            raise _ToolError(
                f"FileNotFoundError: [Errno 2] No such file or directory: {shown!r}"
            )
        raise _ToolError(f"OSError: the file could not be edited ({error})")

    async def _write_file(self, attempt: _Attempt, arguments: dict[str, Any]) -> str:
        content = arguments.get("content")
        if not isinstance(content, str):
            raise _ToolError("KeyError: 'content'")
        host, shown = self._writable(attempt, arguments.get("path"))
        result = await self._broker(
            attempt, REPO_FILE_WRITE, {"path": host, "content": content}
        )
        if isinstance(result, Mapping) and result.get("ok") is True:
            return f"Wrote {shown} ({len(content)} characters)."
        error = result.get("error") if isinstance(result, Mapping) else None
        raise _ToolError(f"OSError: the file could not be written ({error})")

    # -- submit ----------------------------------------------------------------------

    def _unresolved(self, attempt: _Attempt, extra: Sequence[str]) -> tuple[str, ...]:
        waiting = [
            f"approval needed: {tool} (approval {approval_id})"
            for approval_id, tool in attempt.waiting.items()
        ]
        # The backend's own entries (approvals still waiting) come first: the
        # model's questions fill what is left of the list.
        return tuple([*waiting, *extra][:MAX_LIST_ITEMS])

    def _findings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise _ToolError("ValueError: summary is required")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise _ToolError(
                f"ValueError: summary is longer than {MAX_SUMMARY_CHARS} characters"
            )
        confidence = arguments.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise _ToolError("ValueError: confidence must be between 0 and 1")
        return {
            "summary": summary,
            "discovered_facts": _texts(
                arguments.get("discovered_facts"), "discovered_facts"
            ),
            "unresolved_questions": _texts(
                arguments.get("unresolved_questions"), "unresolved_questions"
            ),
            "confidence": None if confidence is None else float(confidence),
        }

    def _correct(self, attempt: _Attempt, text: str) -> None:
        attempt.corrections += 1
        if attempt.corrections > self._limits.submit_corrections:
            raise InvalidNodeResultError(ResultReason.BAD_TYPE)
        raise _ToolError(text)

    def _submit_plan(
        self, attempt: _Attempt, arguments: dict[str, Any]
    ) -> tuple[str, NodeOutcome]:
        findings = self._findings(arguments)
        plan = arguments.get("plan")
        if not isinstance(plan, dict):
            self._correct(attempt, "ValueError: plan must be a JSON object")
        result = NodeResult(
            summary=findings["summary"],
            unresolved_questions=self._unresolved(attempt, ()),
        )
        return "Submitted.", NodeOutcome.succeeded(result, plan=copy.deepcopy(plan))

    async def _submit(
        self, attempt: _Attempt, arguments: dict[str, Any]
    ) -> tuple[str, NodeOutcome | None]:
        findings = self._findings(arguments)
        writing = [r for r in attempt.workspace.repositories.values() if r.writable]
        if attempt.assignment.role is not NodeRole.WORKER or not writing:
            result = NodeResult(
                summary=findings["summary"],
                discovered_facts=findings["discovered_facts"],
                unresolved_questions=self._unresolved(
                    attempt, findings["unresolved_questions"]
                ),
                confidence=findings["confidence"],
            )
            return "Submitted.", NodeOutcome.succeeded(result)
        declared = arguments.get("changed_files")
        if not isinstance(declared, list) or not all(
            isinstance(p, str) for p in declared
        ):
            self._correct(attempt, "ValueError: changed_files must be a list of paths")
        mine: dict[uuid.UUID, set[str]] = {r.repo_id: set() for r in writing}
        for path in declared:
            try:
                repository, _, relative = attempt.workspace.resolve(path)
            except _ToolError as error:
                self._correct(attempt, f"{error} ({path})")
            if not repository.writable:
                self._correct(attempt, f"ValueError: {path} is in a read-only place")
            mine[repository.repo_id].add(relative)
        changes = {}
        for repository in writing:
            found = await self._broker(
                attempt, REPO_GIT_CHANGES, {"workdir": repository.root}
            )
            if not isinstance(found, Mapping) or "changed" not in found:
                raise RuntimeFailure(Failure.OTHER)
            changes[repository.repo_id] = found
        problems = []
        for repository in writing:
            changed = set(changes[repository.repo_id]["changed"])
            claimed = mine[repository.repo_id]
            prefix = "" if attempt.workspace.single else f"{repository.repo_id}/"
            problems += [
                f"not declared: {prefix}{p}" for p in sorted(changed - claimed)
            ]
            problems += [
                f"declared but unchanged: {prefix}{p}"
                for p in sorted(claimed - changed)
            ]
        if problems:
            self._correct(
                attempt,
                "ValueError: changed_files does not match the changes since the "
                "start: " + "; ".join(problems[:50]),
            )
        total = sum(len(changes[r.repo_id]["changed"]) for r in writing)
        if total > MAX_CHANGED_FILES:
            # Checked before anything is committed: the model can narrow it.
            self._correct(
                attempt,
                f"ValueError: {total} files changed; a node may change at most "
                f"{MAX_CHANGED_FILES} (undo the changes that are not needed)",
            )
        commits: dict[uuid.UUID, str] = {}
        for repository in writing:
            found = changes[repository.repo_id]
            uncommitted = sorted(set(found["uncommitted"]))
            head = str(found["head"])
            if uncommitted:
                committed = await self._broker(
                    attempt,
                    REPO_GIT_COMMIT,
                    {"workdir": repository.root, "paths": json.dumps(uncommitted)},
                )
                if not isinstance(committed, Mapping) or "commit" not in committed:
                    raise RuntimeFailure(Failure.OTHER)
                head = str(committed["commit"])
            if head != str(found["base"]):
                commits[repository.repo_id] = head
        changed_files = sorted(
            (p if attempt.workspace.single else f"{r.repo_id}/{p}")
            for r in writing
            for p in changes[r.repo_id]["changed"]
        )
        if len(changed_files) > MAX_CHANGED_FILES:
            raise InvalidNodeResultError(ResultReason.TOO_MANY_ITEMS)
        single = len(writing) == 1 and attempt.workspace.single
        result = NodeResult(
            summary=findings["summary"],
            changed_files=tuple(changed_files),
            commit=(commits.get(writing[0].repo_id) if single else None),
            discovered_facts=findings["discovered_facts"],
            unresolved_questions=self._unresolved(
                attempt, findings["unresolved_questions"]
            ),
            confidence=findings["confidence"],
            artifacts=(
                ()
                if single
                else tuple(f"commit:{rid}:{sha}" for rid, sha in commits.items())
            ),
        )
        return "Submitted.", NodeOutcome.succeeded(result)


def _with_repository(tool: dict[str, Any]) -> dict[str, Any]:
    """``bash`` for a node with several repositories: it names the one to run in
    (each runs in its own sandbox)."""
    tool = copy.deepcopy(tool)
    parameters = tool["function"]["parameters"]
    parameters["properties"]["repository"] = {
        "type": "string",
        "description": "The id of the repository to run in (its directory under "
        "/workspace).",
    }
    parameters["required"] = ["command", "repository"]
    return tool


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
    "LocalAgentLimits",
    "LocalAgentRuntime",
    "truncate",
]
