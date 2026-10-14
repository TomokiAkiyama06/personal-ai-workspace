"""The tools of a node attempt, as a model sees and calls them (Decision 0083, 2,
6, 7 and 10; #208).

:class:`NodeToolkit` is what the local main model's loop (``local.py``) and a
CLI session's MCP bridge (``bridge.py``) give a model: the benchmark harness's
``bash`` / ``str_replace`` / ``write_file`` and ``submit`` (a Planner's
``submit_plan``), each run through ``NodeTools.call`` and so the Tool Broker.
One toolkit per node attempt; it keeps the attempt's approvals (a call that
needs one is told it did not run, and goes with its approval id the next
time: Decision 0083, 7) and the corrections of ``submit``.

``run(name, arguments)`` answers the model's text, and with a successful
``submit`` the attempt's ``NodeOutcome``. A refused call is
:class:`ToolError` (its text is the model's answer); ``RuntimeFailure``,
``InvalidNodeResultError`` (too many wrong submits) and ``NodeStopped`` end
the attempt.
"""

import copy
import json
import math
import posixpath
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from paw_backend.agents.failures import Failure, RuntimeFailure
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
from paw_backend.tools import ExecutionStatus, ToolOutcome, Verdict

WORKSPACE = "/workspace"

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


class ToolError(Exception):
    """A tool call the model gets back as ``Tool error: ...``."""


@dataclass(frozen=True, slots=True)
class ToolkitLimits:
    """The toolkit's part of the loop's limits (the benchmark's)."""

    output_chars: int = 12_000
    submit_corrections: int = 3
    command_seconds: int = 300


class NodeToolkit:
    """The tools of one node attempt (see the module)."""

    def __init__(
        self, assignment: NodeAssignment, limits: ToolkitLimits | None = None
    ) -> None:
        self.assignment = assignment
        self.workspace = _Workspace(assignment)
        self._limits = limits or ToolkitLimits()
        self._corrections = 0
        # Approval ids by the call they belong to, and the ones still waiting.
        self._approvals: dict[tuple[str, str], uuid.UUID] = {}
        self._waiting: dict[uuid.UUID, str] = {}
        role = assignment.role
        self.writes = role is NodeRole.WORKER and any(
            r.writable for r in self.workspace.repositories.values()
        )
        if role is NodeRole.PLANNER:
            tools = [BASH_TOOL, PLAN_TOOL]
        elif self.writes:
            tools = [BASH_TOOL, STR_REPLACE_TOOL, WRITE_FILE_TOOL, WORKER_SUBMIT_TOOL]
        else:
            tools = [BASH_TOOL, READER_SUBMIT_TOOL]
        if not self.workspace.repositories:
            tools = [t for t in tools if t["function"]["name"] != "bash"]
        elif not self.workspace.single:
            tools = [_with_repository(t) if t is BASH_TOOL else t for t in tools]
        self.tools: list[dict[str, Any]] = tools

    def __repr__(self) -> str:
        return f"NodeToolkit(node={self.assignment.node_key!r})"

    async def run(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, NodeOutcome | None]:
        """Run the model's call ``name(arguments)`` (see the module)."""
        return await self._tool(name, arguments)

    async def _tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, NodeOutcome | None]:
        names = {t["function"]["name"] for t in self.tools}
        if name not in names:
            raise ToolError(f"ValueError: unknown tool {name!r}")
        if name == "bash":
            return await self._bash(arguments), None
        if name == "str_replace":
            return await self._str_replace(arguments), None
        if name == "write_file":
            return await self._write_file(arguments), None
        if name == "submit_plan":
            return self._submit_plan(arguments)
        return await self._submit(arguments)

    async def _broker(self, tool: str, arguments: dict[str, Any]) -> object:
        """Run a tool through the broker: its result, or ``ToolError`` for a
        call that did not run (approval, refusal, executor failure)."""
        key = (tool, json.dumps(arguments, sort_keys=True))
        approval = self._approvals.get(key)
        outcome = await self.assignment.tools.call(
            tool, arguments, approval_id=approval
        )
        if not isinstance(outcome, ToolOutcome):
            raise ToolError("RuntimeError: the tool gave no answer")
        decision = outcome.decision
        if outcome.status is ExecutionStatus.COMPLETED:
            if approval is not None:
                self._waiting.pop(approval, None)
            return outcome.result
        if decision.verdict is Verdict.NEEDS_APPROVAL:
            if decision.approval_id is not None:
                self._approvals[key] = decision.approval_id
                self._waiting[decision.approval_id] = tool
            raise ToolError(
                NOT_APPROVED.format(approval=decision.approval_id or "pending")
            )
        if approval is not None:
            # The approval ended (rejected, revoked, expired): nothing waits.
            self._waiting.pop(approval, None)
            self._approvals.pop(key, None)
        if outcome.status is ExecutionStatus.FAILED:
            if outcome.error_type in ("SandboxUnavailableError", "PermissionError"):
                raise RuntimeFailure(Failure.SANDBOX_UNAVAILABLE)
            raise ToolError(f"{outcome.error_type or 'Error'}: the tool failed")
        raise ToolError(
            f"PermissionError: the call was refused ({decision.reason.value})"
        )

    def _bash_place(self, arguments: dict[str, Any]):
        workspace = self.workspace
        if workspace.single:
            return workspace.first()
        wanted = arguments.get("repository")
        for repository in workspace.repositories.values():
            if str(repository.repo_id) == wanted:
                return repository
        raise ToolError("ValueError: name the repository (its id) to run in")

    async def _bash(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command")
        if not isinstance(command, str):
            raise ToolError("KeyError: 'command'")
        repository = self._bash_place(arguments)
        writes = repository.writable and self.assignment.role is NodeRole.WORKER
        result = await self._broker(
            REPO_SHELL_RUN if writes else REPO_SHELL_READ,
            {"workdir": repository.root, "command": command},
        )
        if not isinstance(result, Mapping) or not isinstance(
            result.get("exit_code"), int
        ):
            raise ToolError("RuntimeError: the command gave no answer")
        code, output = result["exit_code"], str(result.get("output", ""))
        if code == 124:
            output += f"\n(command timed out after {self._limits.command_seconds} s)"
        return f"exit code: {code}\n{truncate(output, self._limits.output_chars)}"

    def _writable(self, path: object) -> tuple[str, str]:
        repository, host, _ = self.workspace.resolve(path)
        if not repository.writable:
            raise ToolError("PermissionError: this repository is read-only")
        return host, str(path)

    async def _str_replace(self, arguments: dict[str, Any]) -> str:
        old, new = arguments.get("old_str"), arguments.get("new_str")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("KeyError: 'old_str'")
        host, shown = self._writable(arguments.get("path"))
        result = await self._broker(
            REPO_FILE_STR_REPLACE,
            {"path": host, "old_str": old, "new_str": new},
        )
        if isinstance(result, Mapping) and result.get("ok") is True:
            return f"Edited {shown}."
        error = result.get("error") if isinstance(result, Mapping) else None
        if error == "occurrences":
            count = result.get("count")
            raise ToolError(
                f"ValueError: old_str occurs {count} times in {shown} "
                "(must be exactly 1)"
            )
        if error == "not_found":
            raise ToolError(
                f"FileNotFoundError: [Errno 2] No such file or directory: {shown!r}"
            )
        raise ToolError(f"OSError: the file could not be edited ({error})")

    async def _write_file(self, arguments: dict[str, Any]) -> str:
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ToolError("KeyError: 'content'")
        host, shown = self._writable(arguments.get("path"))
        result = await self._broker(REPO_FILE_WRITE, {"path": host, "content": content})
        if isinstance(result, Mapping) and result.get("ok") is True:
            return f"Wrote {shown} ({len(content)} characters)."
        error = result.get("error") if isinstance(result, Mapping) else None
        raise ToolError(f"OSError: the file could not be written ({error})")

    # -- submit ----------------------------------------------------------------------

    def _unresolved(self, extra: Sequence[str]) -> tuple[str, ...]:
        waiting = [
            f"approval needed: {tool} (approval {approval_id})"
            for approval_id, tool in self._waiting.items()
        ]
        # The backend's own entries (approvals still waiting) come first: the
        # model's questions fill what is left of the list.
        return tuple([*waiting, *extra][:MAX_LIST_ITEMS])

    def _findings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        summary = arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ToolError("ValueError: summary is required")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise ToolError(
                f"ValueError: summary is longer than {MAX_SUMMARY_CHARS} characters"
            )
        confidence = arguments.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ToolError("ValueError: confidence must be between 0 and 1")
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

    def _correct(self, text: str) -> None:
        self._corrections += 1
        if self._corrections > self._limits.submit_corrections:
            raise InvalidNodeResultError(ResultReason.BAD_TYPE)
        raise ToolError(text)

    def _submit_plan(self, arguments: dict[str, Any]) -> tuple[str, NodeOutcome]:
        findings = self._findings(arguments)
        plan = arguments.get("plan")
        if not isinstance(plan, dict):
            self._correct("ValueError: plan must be a JSON object")
        result = NodeResult(
            summary=findings["summary"],
            unresolved_questions=self._unresolved(()),
        )
        return "Submitted.", NodeOutcome.succeeded(result, plan=copy.deepcopy(plan))

    async def _submit(
        self, arguments: dict[str, Any]
    ) -> tuple[str, NodeOutcome | None]:
        findings = self._findings(arguments)
        writing = [r for r in self.workspace.repositories.values() if r.writable]
        if self.assignment.role is not NodeRole.WORKER or not writing:
            result = NodeResult(
                summary=findings["summary"],
                discovered_facts=findings["discovered_facts"],
                unresolved_questions=self._unresolved(findings["unresolved_questions"]),
                confidence=findings["confidence"],
            )
            return "Submitted.", NodeOutcome.succeeded(result)
        declared = arguments.get("changed_files")
        if not isinstance(declared, list) or not all(
            isinstance(p, str) for p in declared
        ):
            self._correct("ValueError: changed_files must be a list of paths")
        mine: dict[uuid.UUID, set[str]] = {r.repo_id: set() for r in writing}
        for path in declared:
            try:
                repository, _, relative = self.workspace.resolve(path)
            except ToolError as error:
                self._correct(f"{error} ({path})")
            if not repository.writable:
                self._correct(f"ValueError: {path} is in a read-only place")
            mine[repository.repo_id].add(relative)
        changes = {}
        for repository in writing:
            found = await self._broker(REPO_GIT_CHANGES, {"workdir": repository.root})
            if not isinstance(found, Mapping) or "changed" not in found:
                raise RuntimeFailure(Failure.OTHER)
            changes[repository.repo_id] = found
        problems = []
        for repository in writing:
            changed = set(changes[repository.repo_id]["changed"])
            claimed = mine[repository.repo_id]
            prefix = "" if self.workspace.single else f"{repository.repo_id}/"
            problems += [
                f"not declared: {prefix}{p}" for p in sorted(changed - claimed)
            ]
            problems += [
                f"declared but unchanged: {prefix}{p}"
                for p in sorted(claimed - changed)
            ]
        if problems:
            self._correct(
                "ValueError: changed_files does not match the changes since the "
                "start: " + "; ".join(problems[:50]),
            )
        total = sum(len(changes[r.repo_id]["changed"]) for r in writing)
        if total > MAX_CHANGED_FILES:
            # Checked before anything is committed: the model can narrow it.
            self._correct(
                f"ValueError: {total} files changed; a node may change at most "
                f"{MAX_CHANGED_FILES} (undo the changes that are not needed)",
            )
        changed_files = sorted(
            (p if self.workspace.single else f"{r.repo_id}/{p}")
            for r in writing
            for p in changes[r.repo_id]["changed"]
        )
        single = len(writing) == 1 and self.workspace.single

        def result_of(commits: Mapping[uuid.UUID, str]) -> NodeResult:
            return NodeResult(
                summary=findings["summary"],
                changed_files=tuple(changed_files),
                commit=(commits.get(writing[0].repo_id) if single else None),
                discovered_facts=findings["discovered_facts"],
                unresolved_questions=self._unresolved(findings["unresolved_questions"]),
                confidence=findings["confidence"],
                artifacts=(
                    ()
                    if single
                    else tuple(f"commit:{rid}:{sha}" for rid, sha in commits.items())
                ),
            )

        # The whole result is checked before anything is committed (a path too
        # long, a summary with control characters, a result too large): the
        # model can still correct it, and the branch is not changed for nothing.
        # A repository that will have a commit (uncommitted paths, or a head past
        # its base) counts with the longest id there is; the others with none.
        try:
            result_of(
                {
                    r.repo_id: "f" * 64
                    for r in writing
                    if changes[r.repo_id]["uncommitted"]
                    or str(changes[r.repo_id]["head"])
                    != str(changes[r.repo_id]["base"])
                }
            )
        except InvalidNodeResultError as error:
            self._correct(
                f"ValueError: the result is not acceptable ({error.reason.value}); "
                "shorten the summary or the lists, or the paths you changed",
            )
        commits: dict[uuid.UUID, str] = {}
        for repository in writing:
            found = changes[repository.repo_id]
            uncommitted = sorted(set(found["uncommitted"]))
            head = str(found["head"])
            if uncommitted:
                committed = await self._broker(
                    REPO_GIT_COMMIT,
                    {"workdir": repository.root, "paths": json.dumps(uncommitted)},
                )
                if not isinstance(committed, Mapping) or "commit" not in committed:
                    raise RuntimeFailure(Failure.OTHER)
                head = str(committed["commit"])
            if head != str(found["base"]):
                commits[repository.repo_id] = head
        result = result_of(commits)
        return "Submitted.", NodeOutcome.succeeded(result)


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
        model gave, or ``ToolError`` (the benchmark's refusals)."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ToolError("ValueError: path is outside /workspace")
        if path.startswith(WORKSPACE):
            path = path[len(WORKSPACE) :].lstrip("/")
        elif path.startswith("/"):
            raise ToolError("ValueError: path is outside /workspace")
        parts = path.split("/")
        if any(part == ".." for part in parts):
            raise ToolError("ValueError: path is outside /workspace")
        relative = posixpath.normpath(path) if path else ""
        if relative in ("", "."):
            raise ToolError("ValueError: path is outside /workspace")
        if self.single:
            repository = self.first()
        else:
            head, _, relative = relative.partition("/")
            repository = next(
                (r for r in self.repositories.values() if str(r.repo_id) == head),
                None,
            )
            if repository is None or not relative:
                raise ToolError("ValueError: path is outside /workspace")
        assert repository is not None
        if ".git" in relative.split("/"):
            raise ToolError("ValueError: editing .git is not allowed")
        return repository, f"{repository.root}/{relative}", relative


def _texts(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"ValueError: {label} must be a list of strings")
    if len(value) > MAX_LIST_ITEMS or any(len(v) > MAX_ITEM_CHARS for v in value):
        raise ToolError(f"ValueError: {label} is too long")
    return tuple(value)


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


__all__ = [
    "BASH_TOOL",
    "PLAN_TOOL",
    "READER_SUBMIT_TOOL",
    "STR_REPLACE_TOOL",
    "WORKER_SUBMIT_TOOL",
    "WORKSPACE",
    "WRITE_FILE_TOOL",
    "NodeToolkit",
    "ToolError",
    "ToolkitLimits",
    "truncate",
]
