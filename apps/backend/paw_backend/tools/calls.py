"""A tool call as the broker sees it, and how its arguments are normalised.

What a *model* supplies is the tool name and the arguments (:class:`ToolCall`).
Everything else in a call (who the task belongs to, which agent grant applies,
which paths / hosts / projects the task may touch) is resolved by the backend
into a :class:`TaskContext`. Nothing in the name or the arguments can change
that context, the tool's classes or the policy.

Arguments are checked against the tool's declared schema: an argument that is
not declared, a missing required one, a wrong type (``"true"`` is not a bool,
``True`` is not an int) or an oversized value refuses the whole call. Targets
are normalised (``scope.py``) so that the approval hash and the scope check see
one canonical spelling.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from paw_backend.authz import AgentGrant
from paw_backend.authz.subjects import to_uuid
from paw_backend.tools.approval_types import SummaryItem, summary_value
from paw_backend.tools.capabilities import ApprovalLevel, ToolCapability
from paw_backend.tools.credentials import (
    contains_credential_plaintext,
    is_credential_handle,
)
from paw_backend.tools.decisions import BrokerReason
from paw_backend.tools.registry import MAX_TEXT_LENGTH, ArgumentKind, ToolSpec
from paw_backend.tools.scope import (
    MAX_HOST_LENGTH,
    MAX_PATH_LENGTH,
    MAX_URL_LENGTH,
    Target,
    TargetError,
    TargetKind,
    TaskScope,
    normalise_host,
    normalise_path,
    normalise_project,
    normalise_repository,
    normalise_url,
)
from paw_backend.tools.task_state import TaskRun

HASH_VERSION = 1
# All text arguments of one call together (each is also bounded by its kind).
MAX_ARGUMENT_CHARS = 262_144
_KIND_LIMITS = {
    ArgumentKind.PATH: (MAX_PATH_LENGTH, BrokerReason.INVALID_TARGET),
    ArgumentKind.URL: (MAX_URL_LENGTH, BrokerReason.INVALID_TARGET),
    ArgumentKind.HOST: (MAX_HOST_LENGTH + 2, BrokerReason.INVALID_TARGET),
    ArgumentKind.PROJECT: (64, BrokerReason.INVALID_TARGET),
    ArgumentKind.REPOSITORY: (64, BrokerReason.INVALID_TARGET),
    ArgumentKind.CREDENTIAL_HANDLE: (64, BrokerReason.CREDENTIAL_HANDLE_INVALID),
}


@dataclass(frozen=True, slots=True)
class TaskContext:
    """What the backend knows about the task a call belongs to.

    ``delegator_id`` is the human user the agent works for and ``grant`` what
    that user delegated (``grant.agent_id`` is the requesting agent).
    ``primary_project_id`` is the task's own project; it must be one of the
    projects of the scope. ``run`` is the run of the task the worker was started
    for (``tasks.attempt`` and ``tasks.retry_count`` when the orchestrator
    started it): an approval is requested for, and can only be used by, that
    run, and a worker of a run that a Retry / Restart replaced is refused
    (``task_superseded``).
    """

    task_id: uuid.UUID
    delegator_id: uuid.UUID
    grant: AgentGrant
    scope: TaskScope
    primary_project_id: uuid.UUID
    run: TaskRun

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", to_uuid(self.task_id, "task_id"))
        object.__setattr__(
            self, "delegator_id", to_uuid(self.delegator_id, "delegator_id")
        )
        object.__setattr__(
            self,
            "primary_project_id",
            to_uuid(self.primary_project_id, "primary_project_id"),
        )
        if not isinstance(self.grant, AgentGrant):
            raise TypeError("grant must be an AgentGrant")
        if not isinstance(self.scope, TaskScope):
            raise TypeError("scope must be a TaskScope")
        if not isinstance(self.run, TaskRun):
            raise TypeError("run must be a TaskRun")
        if self.primary_project_id not in self.scope.projects:
            raise ValueError("the task's project must be part of its scope")
        if self.grant.agent_id == self.delegator_id:
            raise ValueError("an agent cannot be the user it acts for")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A request to run a tool. ``tool`` and ``arguments`` come from a model, so
    they are validated by the broker, not here: building a call never raises
    on their account."""

    tool: object
    arguments: object = field(repr=False)
    context: TaskContext
    correlation_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """What an executor is told to run: only ever built by the broker, from an
    ALLOW decision. ``arguments`` are the normalised values."""

    tool: str
    arguments: Mapping[str, object] = field(repr=False)
    context: TaskContext
    call_hash: str
    level: ApprovalLevel
    capabilities: frozenset[ToolCapability]
    correlation_id: uuid.UUID


class ArgumentError(Exception):
    """Internal: a call's arguments are refused. Carries only a reason code."""

    def __init__(self, reason: BrokerReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ParsedArguments:
    # The normalised values are the call's content: never part of a repr / log.
    values: Mapping[str, object] = field(repr=False)
    targets: tuple[Target, ...]
    # The canonical URL of every URL argument (a target keeps only its host).
    urls: tuple[str, ...] = ()
    # Every provided argument as the approver sees it (bounded, redacted).
    summary: tuple[SummaryItem, ...] = field(default=(), repr=False)


def parse_arguments(
    spec: ToolSpec, arguments: object, scope: TaskScope
) -> ParsedArguments:
    """Validate and normalise ``arguments`` against ``spec``, or raise
    :class:`ArgumentError`."""
    if not isinstance(arguments, Mapping):
        raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
    if len(arguments) > len(spec.arguments):
        raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
    for key in arguments:
        if type(key) is not str or key not in spec.arguments:
            raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
    base = scope.path_roots[0] if scope.path_roots else None
    values: dict[str, object] = {}
    targets: list[Target] = []
    urls: list[str] = []
    summary: list[SummaryItem] = []
    total_chars = 0
    for name, argument in spec.arguments.items():  # declared order: deterministic
        if name not in arguments:
            if argument.required:
                raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
            continue
        raw = arguments[name]
        if type(raw) is str:
            # The whole call is bounded before any text is scanned.
            total_chars += len(raw)
            if total_chars > MAX_ARGUMENT_CHARS:
                raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
        value, target = _parse_value(argument, raw, base)
        values[name] = value
        summary.append(
            SummaryItem(name, argument.kind.value, summary_value(_shown(value)))
        )
        if target is not None:
            targets.append(target)
        if argument.kind is ArgumentKind.URL:
            urls.append(value)
    return ParsedArguments(
        MappingProxyType(values), tuple(targets), tuple(urls), tuple(summary)
    )


def _shown(value: object) -> str:
    """The normalised value as text (a bool as ``true`` / ``false``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_value(argument, value: object, base: str | None):
    kind = argument.kind
    if kind in (ArgumentKind.PROJECT, ArgumentKind.REPOSITORY) and isinstance(
        value, uuid.UUID
    ):
        value = str(value)
    if kind is ArgumentKind.INTEGER:
        if type(value) is not int or not argument.minimum <= value <= argument.maximum:
            raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
        return value, None
    if kind is ArgumentKind.BOOLEAN:
        if type(value) is not bool:
            raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
        return value, None
    # Every remaining kind is text. The length limit of the kind (for text, the
    # tool's declared ``max_length``) comes first so that nothing oversized is
    # ever scanned or normalised.
    if type(value) is not str:
        raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
    if kind is ArgumentKind.TEXT:
        limit, too_long = min(argument.max_length, MAX_TEXT_LENGTH), None
    else:
        limit, too_long = _KIND_LIMITS[kind]
    if len(value) > limit:
        raise ArgumentError(too_long or BrokerReason.INVALID_ARGUMENTS)
    if contains_credential_plaintext(value):
        raise ArgumentError(BrokerReason.CREDENTIAL_PLAINTEXT_IN_ARGUMENTS)
    try:
        if kind is ArgumentKind.PATH:
            path = normalise_path(value, base=base)
            return path, Target(TargetKind.PATH, path)
        if kind is ArgumentKind.URL:
            url, host = normalise_url(value)
            return url, Target(TargetKind.HOST, host)
        if kind is ArgumentKind.HOST:
            host = normalise_host(value)
            return host, Target(TargetKind.HOST, host)
        if kind is ArgumentKind.PROJECT:
            project = str(normalise_project(value))
            return project, Target(TargetKind.PROJECT, project)
        if kind is ArgumentKind.REPOSITORY:
            repository = str(normalise_repository(value))
            return repository, Target(TargetKind.REPOSITORY, repository)
    except TargetError:
        raise ArgumentError(BrokerReason.INVALID_TARGET) from None
    if kind is ArgumentKind.CREDENTIAL_HANDLE:
        if not is_credential_handle(value):
            raise ArgumentError(BrokerReason.CREDENTIAL_HANDLE_INVALID)
        return value, Target(TargetKind.CREDENTIAL, value)
    # ArgumentKind.TEXT
    if len(value) > argument.max_length or "\x00" in value:
        raise ArgumentError(BrokerReason.INVALID_ARGUMENTS)
    return value, None


def compute_call_hash(
    tool: str, values: Mapping[str, object], context: TaskContext
) -> str:
    """The digest an approval is bound to: tool, normalised arguments, task and
    requester (the user and the agent). Two spellings of the same call hash
    alike; changing any part changes the hash."""
    document = {
        "v": HASH_VERSION,
        "tool": tool,
        "arguments": dict(values),
        "task": str(context.task_id),
        "user": str(context.delegator_id),
        "agent": str(context.grant.agent_id),
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
