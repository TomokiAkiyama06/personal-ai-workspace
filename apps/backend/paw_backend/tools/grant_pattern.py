"""What a task-scoped approval grant covers (Decision 0085, sections 1 and 2).

A human who answers an approval with "このタスクの間は許可" lets the broker run,
without asking again, the later calls of the **same tool** whose scope and
arguments are **the same or narrower** than the call they approved. What
"narrower" means is fixed here, per argument, as a :class:`GrantPattern` that the
broker builds when it opens the approval (it is stored with the approval and
copied into the grant) and builds again for every later call:

* a path: the same path or one below it (``/w/a`` covers ``/w/a/b``, never
  ``/w/a-b``; paths are the canonical spelling of ``scope.normalise_path``);
* a URL without a query: the same URL or one below it, also without a query
  (``scope.url_within``: a path that can climb out is not below anything); a URL
  with a query, or one whose own path could climb out, only itself;
* a host, a project, a repository: only itself;
* text, an integer, a boolean (a command, a content...): only the same value.
  "Narrower" cannot be judged for free text, so only its SHA-256 is kept and
  compared: the pattern never holds the text itself;
* an optional argument must be present, or absent, in both;
* the scope status must be the same or narrower (``in_scope`` is narrower than
  ``host_out_of_scope``).

Some calls can never be granted for a task (:func:`grant_pattern_of` returns
``None``): a ``STRONG_APPROVAL``, a tool that deletes (``destructive``), uses a
credential (``credential-use`` or a credential handle), sends something out
(``network`` with ``write``), changes a Working Set, or whose PAW-025 capability
changes an ACL, a role, a permission or other administration. Everything else
about a call (the policy level, the authorization, the budget, the lease, the
Working Set's roles) is checked for every call, granted or not: a grant only
stands in for the human's click.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from paw_backend.authz import Capability
from paw_backend.tools.capabilities import ApprovalLevel, ScopeStatus, ToolCapability
from paw_backend.tools.registry import ArgumentKind, ToolSpec
from paw_backend.tools.scope import url_path_is_safe, url_within

GRANT_PATTERN_VERSION = 1
_ARGUMENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_PATTERN_VALUE = 2048
# How wide a scope status is: a grant covers its own and the narrower ones.
_SCOPE_RANK = {ScopeStatus.IN_SCOPE: 0, ScopeStatus.HOST_OUT_OF_SCOPE: 1}
# What a grant may never stand in for (Decision 0085, section 2).
_NEVER_CLASSES = frozenset({ToolCapability.DESTRUCTIVE, ToolCapability.CREDENTIAL_USE})
_EXTERNAL_SEND = frozenset({ToolCapability.NETWORK, ToolCapability.WRITE})
_NEVER_CAPABILITIES = frozenset(
    {
        Capability.PROJECT_SETTINGS_MANAGE,
        Capability.PROJECT_MEMBERS_MANAGE,
        Capability.PROJECT_AGENT_POLICY_MANAGE,
        Capability.PROJECT_LIFECYCLE_MANAGE,
        Capability.PROJECT_REPO_ADD,
        Capability.PROJECT_TASK_WORKING_SET_MANAGE,
        Capability.ACCOUNT_MANAGE,
        Capability.SHARED_MEMORY_MANAGE,
    }
)
_NEVER_CAPABILITY_PREFIXES = ("admin.", "owner.")
_NEVER_ARGUMENTS = frozenset(
    {ArgumentKind.CREDENTIAL_HANDLE, ArgumentKind.WORKING_SET_REPOSITORY}
)
_EQUAL_KINDS = frozenset(
    {ArgumentKind.HOST, ArgumentKind.PROJECT, ArgumentKind.REPOSITORY}
)


class GrantMatch(StrEnum):
    """How one argument of a later call is compared with the granted one."""

    UNDER = "under"  # the same path / URL, or one below it
    EQUAL = "equal"  # the same canonical value
    DIGEST = "digest"  # the same value, compared by its SHA-256


@dataclass(frozen=True, slots=True)
class GrantArgument:
    name: str
    kind: ArgumentKind
    match: GrantMatch
    value: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or _ARGUMENT_NAME.fullmatch(self.name) is None:
            raise ValueError("a grant argument needs a valid name")
        object.__setattr__(self, "kind", ArgumentKind(self.kind))
        object.__setattr__(self, "match", GrantMatch(self.match))
        if type(self.value) is not str or not 1 <= len(self.value) <= (
            MAX_PATTERN_VALUE
        ):
            raise ValueError("a grant argument needs a bounded value")
        if self.match is GrantMatch.DIGEST and not _DIGEST.fullmatch(self.value):
            raise ValueError("a digest must be a SHA-256 hex digest")
        if self.kind in _NEVER_ARGUMENTS:
            raise ValueError("this argument kind is never granted")

    def covers(self, other: "GrantArgument") -> bool:
        """Whether ``other`` (of a later call) is this argument or narrower."""
        if (other.name, other.kind, other.match) != (self.name, self.kind, self.match):
            return False
        if self.match is not GrantMatch.UNDER:
            return other.value == self.value
        if self.kind is ArgumentKind.PATH:
            return (
                other.value == self.value
                or self.value == "/"
                or other.value.startswith(self.value + "/")
            )
        # A URL without a query (``grant_pattern_of``).
        return "?" not in other.value and url_within(other.value, self.value)


@dataclass(frozen=True, slots=True)
class GrantPattern:
    """The scope status and the arguments of one call, as a grant compares them."""

    scope: ScopeStatus
    arguments: tuple[GrantArgument, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", ScopeStatus(self.scope))
        if self.scope not in _SCOPE_RANK:
            raise ValueError("a call out of scope is never granted")
        if not isinstance(self.arguments, tuple) or not all(
            isinstance(item, GrantArgument) for item in self.arguments
        ):
            raise TypeError("arguments must be a tuple of GrantArgument")
        names = [item.name for item in self.arguments]
        if len(set(names)) != len(names):
            raise ValueError("an argument is named twice")

    def covers(self, call: "GrantPattern") -> bool:
        """Whether the later ``call`` is the same as this one or narrower."""
        if _SCOPE_RANK[call.scope] > _SCOPE_RANK[self.scope]:
            return False
        mine = {item.name: item for item in self.arguments}
        theirs = {item.name: item for item in call.arguments}
        if mine.keys() != theirs.keys():
            return False
        return all(mine[name].covers(theirs[name]) for name in mine)

    def to_json(self) -> dict[str, object]:
        return {
            "v": GRANT_PATTERN_VERSION,
            "scope": self.scope.value,
            "arguments": [
                {
                    "name": item.name,
                    "kind": item.kind.value,
                    "match": item.match.value,
                    "value": item.value,
                }
                for item in self.arguments
            ],
        }

    @classmethod
    def from_json(cls, data: object) -> "GrantPattern":
        """The pattern stored by :meth:`to_json` (``ValueError`` otherwise)."""
        if not isinstance(data, Mapping) or data.get("v") != GRANT_PATTERN_VERSION:
            raise ValueError("not a grant pattern")
        items = data.get("arguments")
        if not isinstance(items, list):
            raise ValueError("not a grant pattern")
        try:
            return cls(
                ScopeStatus(data.get("scope")),
                tuple(
                    GrantArgument(
                        item["name"], item["kind"], item["match"], item["value"]
                    )
                    for item in items
                ),
            )
        except (KeyError, TypeError) as error:
            raise ValueError("not a grant pattern") from error


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def grantable(spec: ToolSpec, level: ApprovalLevel) -> bool:
    """Whether a call of ``spec`` at ``level`` may ever be granted for a task."""
    if level is not ApprovalLevel.APPROVAL or spec.working_set_operation is not None:
        return False
    if spec.capabilities & _NEVER_CLASSES or _EXTERNAL_SEND <= spec.capabilities:
        return False
    capability = spec.authz_capability
    if capability in _NEVER_CAPABILITIES or capability.value.startswith(
        _NEVER_CAPABILITY_PREFIXES
    ):
        return False
    return not any(
        argument.kind in _NEVER_ARGUMENTS for argument in spec.arguments.values()
    )


def grant_pattern_of(
    spec: ToolSpec,
    values: Mapping[str, object],
    scope: ScopeStatus,
    level: ApprovalLevel,
) -> GrantPattern | None:
    """The pattern of a call of ``spec`` with the normalised ``values``
    (``calls.parse_arguments``), or ``None`` when it can never be granted."""
    if not grantable(spec, level) or scope not in _SCOPE_RANK:
        return None
    arguments: list[GrantArgument] = []
    for name, argument in spec.arguments.items():  # declared order
        if name not in values:
            continue
        value = values[name]
        kind = argument.kind
        if kind is ArgumentKind.PATH:
            item = GrantArgument(name, kind, GrantMatch.UNDER, str(value))
        elif kind is ArgumentKind.URL:
            url = str(value)
            if "?" in url or not url_path_is_safe(url):
                item = GrantArgument(name, kind, GrantMatch.DIGEST, _digest(url))
            else:
                item = GrantArgument(name, kind, GrantMatch.UNDER, url.rstrip("/"))
        elif kind in _EQUAL_KINDS:
            item = GrantArgument(name, kind, GrantMatch.EQUAL, str(value))
        else:  # text, integer, boolean
            item = GrantArgument(name, kind, GrantMatch.DIGEST, _digest(value))
        arguments.append(item)
    return GrantPattern(scope, tuple(arguments))
