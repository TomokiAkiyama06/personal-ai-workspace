"""The tool registry: which tools exist and what each one *is*.

A :class:`ToolSpec` is declared by backend code and is immutable: its
capability classes, environment, argument schema and minimum approval level
cannot be changed by a call, a prompt or a model's output. The registry is
built once from a list of specs and has no way to add, replace or remove one
afterwards. A tool that is not in it does not exist for the broker (default
deny).

The specs are validated when they are built, so that a tool cannot be declared
in a way that would let it skip a check:

* a tool that takes a host or URL argument is a ``network`` tool, one that
  takes a credential handle is a ``credential-use`` tool (it cannot hide
  behind a ``read`` declaration);
* a project-local ``write`` / ``destructive`` tool must name what it touches
  with a *required* path, host, URL, project or repository argument (an optional
  one can be left out, and a call without targets looks "in scope");
* a tool whose PAW-025 capability writes to a repository (``project.repo.write``,
  ``project.pr.create``) must require the **path or the repository** it changes:
  only those two tie a call to a repository, and so to that repository's ACL
  (``broker.py``); a host or URL does not name one;
* a tool declares the arguments it accepts. Anything else in a call is
  refused, so a model cannot smuggle a ``"capability": "read"`` or an
  ``"approved": true`` next to the real arguments.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from paw_backend.authz import Capability, RepoPermission
from paw_backend.authz.capabilities import REPO_PERMISSION_OF
from paw_backend.tools.capabilities import ApprovalLevel, Environment, ToolCapability

TOOL_NAME_PATTERN = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*"
_TOOL_NAME = re.compile(TOOL_NAME_PATTERN)
_ARGUMENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_TOOL_NAME_LENGTH = 48
MAX_ARGUMENTS = 16
MAX_TEXT_LENGTH = 65_536
DEFAULT_TEXT_LENGTH = 4096
INT_MIN, INT_MAX = -(2**31), 2**31 - 1
# ``tool.unknown`` and ``tool.approval.*`` are audit actions of the broker.
RESERVED_NAMES = frozenset({"unknown", "approval"})


class ArgumentKind(StrEnum):
    PATH = "path"  # a file system path: must be inside the task's roots
    URL = "url"  # an http(s) URL: its host must be a task host
    HOST = "host"  # a host name: must be a task host
    PROJECT = "project"  # a project id: must be a project of the task
    REPOSITORY = "repository"  # a repository id: must be in the task's working set
    CREDENTIAL_HANDLE = "credential_handle"  # an opaque handle, never plaintext
    TEXT = "text"  # free text, bounded
    INTEGER = "integer"
    BOOLEAN = "boolean"


TARGET_KINDS = frozenset(
    {
        ArgumentKind.PATH,
        ArgumentKind.URL,
        ArgumentKind.HOST,
        ArgumentKind.PROJECT,
        ArgumentKind.REPOSITORY,
    }
)


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    kind: ArgumentKind
    required: bool = True
    max_length: int = DEFAULT_TEXT_LENGTH  # TEXT only
    minimum: int = INT_MIN  # INTEGER only
    maximum: int = INT_MAX  # INTEGER only

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ArgumentKind(self.kind))
        if type(self.required) is not bool:
            raise TypeError("required must be a bool")
        for name in ("max_length", "minimum", "maximum"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        if not 1 <= self.max_length <= MAX_TEXT_LENGTH:
            raise ValueError("max_length is out of range")
        if not INT_MIN <= self.minimum <= self.maximum <= INT_MAX:
            raise ValueError("minimum / maximum are out of range")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool. ``authz_capability`` is the PAW-025 capability the call needs:
    the authorization decision is made on it first, and the tool classes below
    only ever narrow that result."""

    name: str
    capabilities: frozenset[ToolCapability]
    authz_capability: Capability
    arguments: Mapping[str, ArgumentSpec] = field(default_factory=dict)
    environment: Environment = Environment.PROJECT_LOCAL
    # Raises the level the policy computes; can never lower it.
    min_level: ApprovalLevel = ApprovalLevel.AUTO
    requires_budget: bool = True
    # A tool that would hand a credential's plaintext to the agent. Such a
    # tool is always denied (it can be registered so that the attempt is
    # recognised and audited as such).
    returns_credential_plaintext: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or len(self.name) > MAX_TOOL_NAME_LENGTH
            or _TOOL_NAME.fullmatch(self.name) is None
            or self.name.split(".")[0] in RESERVED_NAMES
        ):
            raise ValueError("tool name is not valid")
        if isinstance(self.capabilities, str | bytes) or not isinstance(
            self.capabilities, Iterable
        ):
            raise TypeError("capabilities must be a collection of ToolCapability")
        capabilities = frozenset(self.capabilities)
        if not capabilities or not all(
            isinstance(c, ToolCapability) for c in capabilities
        ):
            raise ValueError("a tool needs at least one ToolCapability")
        if not isinstance(self.authz_capability, Capability):
            raise TypeError("authz_capability must be a Capability")
        object.__setattr__(self, "environment", Environment(self.environment))
        object.__setattr__(self, "min_level", ApprovalLevel(self.min_level))
        if type(self.requires_budget) is not bool:
            raise TypeError("requires_budget must be a bool")
        if type(self.returns_credential_plaintext) is not bool:
            raise TypeError("returns_credential_plaintext must be a bool")

        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments must be a mapping")
        if len(self.arguments) > MAX_ARGUMENTS:
            raise ValueError("a tool takes at most 16 arguments")
        arguments: dict[str, ArgumentSpec] = {}
        for argument_name, argument in self.arguments.items():
            if (
                type(argument_name) is not str
                or _ARGUMENT_NAME.fullmatch(argument_name) is None
                or not isinstance(argument, ArgumentSpec)
            ):
                raise ValueError("an argument declaration is not valid")
            arguments[argument_name] = argument
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "arguments", MappingProxyType(arguments))
        self._check_consistency()

    def _check_consistency(self) -> None:
        kinds = {a.kind for a in self.arguments.values()}
        # An optional argument may be left out, so it cannot be what makes a
        # call "name what it touches": only required arguments count below.
        required = {a.kind for a in self.arguments.values() if a.required}
        caps = self.capabilities
        if kinds & {ArgumentKind.URL, ArgumentKind.HOST} and (
            ToolCapability.NETWORK not in caps
        ):
            raise ValueError("a tool with a host or URL argument is a network tool")
        if ArgumentKind.CREDENTIAL_HANDLE in kinds and (
            ToolCapability.CREDENTIAL_USE not in caps
        ):
            raise ValueError("a tool with a credential handle uses credentials")
        if ToolCapability.NETWORK in caps and not required & {
            ArgumentKind.URL,
            ArgumentKind.HOST,
        }:
            raise ValueError("a network tool must require its host or URL")
        if ToolCapability.CREDENTIAL_USE in caps and (
            ArgumentKind.CREDENTIAL_HANDLE not in required
        ):
            raise ValueError("credentials are used by a required handle only")
        if (
            self.environment is Environment.PROJECT_LOCAL
            and caps & {ToolCapability.WRITE, ToolCapability.DESTRUCTIVE}
            and not required & TARGET_KINDS
        ):
            raise ValueError("a write tool must require what it touches")
        if REPO_PERMISSION_OF.get(
            self.authz_capability
        ) is RepoPermission.WRITE and not required & {
            ArgumentKind.PATH,
            ArgumentKind.REPOSITORY,
        }:
            raise ValueError("a repository write must require its path or repository")
        if self.returns_credential_plaintext and (
            ToolCapability.CREDENTIAL_USE not in caps
        ):
            raise ValueError("only a credential tool can return credential plaintext")


class ToolRegistry:
    """A fixed set of tools, looked up by their exact name."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        if isinstance(specs, str | bytes) or not isinstance(specs, Iterable):
            raise TypeError("specs must be a collection of ToolSpec")
        by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise TypeError("a registry holds ToolSpec objects only")
            if spec.name in by_name:
                raise ValueError("two tools have the same name")
            by_name[spec.name] = spec
        self._specs: Mapping[str, ToolSpec] = MappingProxyType(by_name)

    def get(self, name: object) -> ToolSpec | None:
        """The tool called exactly ``name`` (no case folding, no trimming)."""
        if type(name) is not str:
            return None
        return self._specs.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def __len__(self) -> int:
        return len(self._specs)
