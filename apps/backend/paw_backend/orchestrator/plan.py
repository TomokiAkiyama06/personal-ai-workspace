"""The plan: a Task decomposed into a dependency DAG (PAW-034).

A planner (an agent runtime in the ``planner`` role) *proposes* the plan; the
backend orchestrator decides what is accepted. ``Plan`` is validated when it is
built, so a ``Plan`` object is always acceptable: a cycle, an unknown or repeated
dependency, too many nodes, edges or levels, a text or an input that is too large,
a node that asks for more than its role may hold: all raise ``InvalidPlanError``
with a closed reason, before anything is stored.

The schema (Decision 0021, section 1)::

    {"nodes": [
        {"key": "impl",              # [a-z][a-z0-9_-]{0,31}, unique in the plan
         "role": "worker",           # planner | worker | researcher | reviewer
         "title": "Implement the parser",
         "goal": "...",              # what the node must do (text)
         "depends_on": ["research"], # keys of other nodes (optional)
         "required": true,           # must succeed for the task to succeed (optional)
         "input": {...},             # bounded JSON passed to the node (optional)
         "capabilities": ["project.read"],  # narrower than the role (optional)
         "repositories": ["<uuid>"]}        # a subset of the working set (optional)
    ]}

Unknown fields are refused (a misspelled ``depend_on`` must not silently mean "no
dependency"). The planner never chooses the agent, the permissions, the
worktrees or the parallelism: those are the orchestrator's
(``REQUIREMENTS.md``, "Logical roles").

The nodes of an accepted ``Plan`` are in a **deterministic topological order**
(Kahn's algorithm; among the nodes that can go next, the one that came first in
the proposal goes first), and ``ordinal`` is the position in that order: a
dependency always has a smaller ordinal than its dependent, the scheduler orders
by it, and the database stores it.
"""

import heapq
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from paw_backend.authz import Capability
from paw_backend.authz.subjects import to_uuid
from paw_backend.orchestrator.domain import ROLE_CEILING, NodeRole
from paw_backend.orchestrator.errors import InvalidPlanError, PlanReason
from paw_backend.orchestrator.jsonvalue import (
    JsonProblem,
    check_json_object,
    encoded_size,
)
from paw_backend.orchestrator.limits import (
    KEY_PATTERN,
    MAX_DEPENDENCIES,
    MAX_DEPTH,
    MAX_EDGES,
    MAX_GOAL_CHARS,
    MAX_NODE_CAPABILITIES,
    MAX_NODE_INPUT_BYTES,
    MAX_NODE_INPUT_DEPTH,
    MAX_NODE_REPOSITORIES,
    MAX_NODES,
    MAX_PLAN_BYTES,
    MAX_TITLE_CHARS,
)

_KEY = re.compile(KEY_PATTERN)
_SURROGATE = re.compile("[\ud800-\udfff]")
# A title is one line; a goal may have several (newline and tab are allowed).
_TITLE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")
_GOAL_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

NODE_FIELDS = frozenset(
    {
        "key",
        "role",
        "title",
        "goal",
        "depends_on",
        "required",
        "input",
        "capabilities",
        "repositories",
    }
)
_REQUIRED_NODE_FIELDS = ("key", "role", "title", "goal")


def _refuse(reason: PlanReason):
    raise InvalidPlanError(reason)


def _text(value: object, *, limit: int, multiline: bool) -> str:
    if type(value) is not str:
        _refuse(PlanReason.BAD_TYPE)
    if len(value) > limit * 4:  # bounded before it is scanned
        _refuse(PlanReason.BAD_TEXT)
    forbidden = _GOAL_FORBIDDEN if multiline else _TITLE_FORBIDDEN
    if forbidden.search(value) or _SURROGATE.search(value):
        _refuse(PlanReason.BAD_TEXT)
    stripped = value.strip()
    if not stripped or len(stripped) > limit:
        _refuse(PlanReason.BAD_TEXT)
    return stripped


def _key(value: object) -> str:
    if type(value) is not str:
        _refuse(PlanReason.BAD_TYPE)
    if _KEY.fullmatch(value) is None:
        _refuse(PlanReason.BAD_KEY)
    return value


def _collection(value: object) -> list:
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
        _refuse(PlanReason.BAD_TYPE)
    return list(value)


def _role(value: object) -> NodeRole:
    if type(value) is NodeRole:
        return value
    if type(value) is str:
        try:
            return NodeRole(value)
        except ValueError:
            _refuse(PlanReason.UNKNOWN_ROLE)
    _refuse(PlanReason.BAD_TYPE)


def _capabilities(value: object, role: NodeRole) -> tuple[Capability, ...] | None:
    if value is None:
        return None
    items = _collection(value)
    if len(items) > MAX_NODE_CAPABILITIES:
        _refuse(PlanReason.UNKNOWN_CAPABILITY)
    found: set[Capability] = set()
    for item in items:
        if type(item) is Capability:
            found.add(item)
        elif type(item) is str:
            try:
                found.add(Capability(item))
            except ValueError:
                _refuse(PlanReason.UNKNOWN_CAPABILITY)
        else:
            _refuse(PlanReason.BAD_TYPE)
    if not found <= ROLE_CEILING[role]:
        _refuse(PlanReason.CAPABILITY_ABOVE_ROLE)
    return tuple(sorted(found, key=lambda capability: capability.value))


def _repositories(value: object) -> tuple[uuid.UUID, ...] | None:
    if value is None:
        return None
    items = _collection(value)
    if len(items) > MAX_NODE_REPOSITORIES:
        _refuse(PlanReason.BAD_REPOSITORY)
    found: set[uuid.UUID] = set()
    for item in items:
        if not isinstance(item, uuid.UUID | str):
            _refuse(PlanReason.BAD_TYPE)
        try:
            found.add(to_uuid(item, "repository"))
        except ValueError:
            _refuse(PlanReason.BAD_REPOSITORY)
    return tuple(sorted(found, key=str))


def _input(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse(PlanReason.BAD_TYPE)
    try:
        return check_json_object(
            value, max_bytes=MAX_NODE_INPUT_BYTES, max_depth=MAX_NODE_INPUT_DEPTH
        )
    except JsonProblem as problem:
        _refuse(
            PlanReason.TOO_LARGE if problem.kind == "size" else PlanReason.BAD_INPUT
        )


@dataclass(frozen=True, slots=True)
class PlanNode:
    """One node of a plan. Every field is checked and normalised here.

    ``role`` is a ``NodeRole`` or its exact value; ``depends_on`` a collection of
    keys (sorted, duplicates refused); ``capabilities`` (``None``: the role's
    ceiling) a subset of the role's ceiling; ``repositories`` (``None``: the whole
    working set) ids; ``input`` is copied.
    """

    key: str
    role: NodeRole
    title: str
    goal: str
    depends_on: tuple[str, ...] = ()
    required: bool = True
    input: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[Capability, ...] | None = None
    repositories: tuple[uuid.UUID, ...] | None = None

    def __post_init__(self) -> None:
        role = _role(self.role)
        object.__setattr__(self, "key", _key(self.key))
        object.__setattr__(self, "role", role)
        object.__setattr__(
            self, "title", _text(self.title, limit=MAX_TITLE_CHARS, multiline=False)
        )
        object.__setattr__(
            self, "goal", _text(self.goal, limit=MAX_GOAL_CHARS, multiline=True)
        )
        dependencies = [_key(item) for item in _collection(self.depends_on)]
        if len(dependencies) > MAX_DEPENDENCIES:
            _refuse(PlanReason.TOO_MANY_DEPENDENCIES)
        if len(set(dependencies)) != len(dependencies):
            _refuse(PlanReason.DUPLICATE_DEPENDENCY)
        object.__setattr__(self, "depends_on", tuple(sorted(dependencies)))
        if type(self.required) is not bool:
            _refuse(PlanReason.BAD_TYPE)
        object.__setattr__(self, "input", _input(self.input))
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities, role))
        object.__setattr__(self, "repositories", _repositories(self.repositories))

    @classmethod
    def from_mapping(cls, data: object) -> "PlanNode":
        if not isinstance(data, Mapping):
            _refuse(PlanReason.NOT_A_MAPPING)
        if any(type(name) is not str or name not in NODE_FIELDS for name in data):
            _refuse(PlanReason.UNKNOWN_FIELD)
        if any(name not in data for name in _REQUIRED_NODE_FIELDS):
            _refuse(PlanReason.MISSING_FIELD)
        return cls(**dict(data))

    def to_mapping(self) -> dict[str, Any]:
        """The node as JSON-friendly data (``from_mapping`` reads it back)."""
        return {
            "key": self.key,
            "role": self.role.value,
            "title": self.title,
            "goal": self.goal,
            "depends_on": list(self.depends_on),
            "required": self.required,
            "input": dict(self.input),
            "capabilities": (
                None
                if self.capabilities is None
                else [capability.value for capability in self.capabilities]
            ),
            "repositories": (
                None
                if self.repositories is None
                else [str(repository) for repository in self.repositories]
            ),
        }


def _topological_order(nodes: list[PlanNode]) -> list[PlanNode]:
    """Kahn's algorithm; the ready node that came first in the proposal goes first.

    Raises ``CYCLE`` when some node is never released.
    """
    position = {node.key: index for index, node in enumerate(nodes)}
    waiting = {node.key: len(node.depends_on) for node in nodes}
    dependents: dict[str, list[str]] = {node.key: [] for node in nodes}
    for node in nodes:
        for dependency in node.depends_on:
            dependents[dependency].append(node.key)
    ready = [position[key] for key, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[PlanNode] = []
    while ready:
        node = nodes[heapq.heappop(ready)]
        ordered.append(node)
        for key in dependents[node.key]:
            waiting[key] -= 1
            if waiting[key] == 0:
                heapq.heappush(ready, position[key])
    if len(ordered) != len(nodes):
        _refuse(PlanReason.CYCLE)
    return ordered


@dataclass(frozen=True, slots=True)
class Plan:
    """An acceptable plan; ``nodes`` are in topological order (see the module)."""

    nodes: tuple[PlanNode, ...]

    def __post_init__(self) -> None:
        items = _collection(self.nodes)
        if not items:
            _refuse(PlanReason.EMPTY)
        if len(items) > MAX_NODES:
            _refuse(PlanReason.TOO_MANY_NODES)
        nodes = [
            item if isinstance(item, PlanNode) else PlanNode.from_mapping(item)
            for item in items
        ]
        keys = {node.key for node in nodes}
        if len(keys) != len(nodes):
            _refuse(PlanReason.DUPLICATE_KEY)
        edges = 0
        for node in nodes:
            for dependency in node.depends_on:
                if dependency == node.key:
                    _refuse(PlanReason.SELF_DEPENDENCY)
                if dependency not in keys:
                    _refuse(PlanReason.UNKNOWN_DEPENDENCY)
                edges += 1
        if edges > MAX_EDGES:
            _refuse(PlanReason.TOO_MANY_EDGES)
        ordered = _topological_order(nodes)
        depth: dict[str, int] = {}
        for node in ordered:
            depth[node.key] = 1 + max((depth[d] for d in node.depends_on), default=0)
        if max(depth.values()) > MAX_DEPTH:
            _refuse(PlanReason.TOO_DEEP)
        size = sum(
            len(node.title) + len(node.goal) + encoded_size(node.input)
            for node in ordered
        )
        if size > MAX_PLAN_BYTES:
            _refuse(PlanReason.TOO_LARGE)
        if not any(node.required for node in ordered):
            _refuse(PlanReason.NO_REQUIRED_NODE)
        object.__setattr__(self, "nodes", tuple(ordered))

    @classmethod
    def from_mapping(cls, data: object) -> "Plan":
        """A planner's output (``{"nodes": [...]}``) as an acceptable ``Plan``."""
        if not isinstance(data, Mapping):
            _refuse(PlanReason.NOT_A_MAPPING)
        if any(name != "nodes" for name in data):
            _refuse(PlanReason.UNKNOWN_FIELD)
        if "nodes" not in data:
            _refuse(PlanReason.MISSING_FIELD)
        return cls(data["nodes"])

    def to_mapping(self) -> dict[str, Any]:
        return {"nodes": [node.to_mapping() for node in self.nodes]}

    @property
    def edges(self) -> tuple[tuple[str, str], ...]:
        """``(node, dependency)`` pairs, in the order of the nodes."""
        return tuple(
            (node.key, dependency)
            for node in self.nodes
            for dependency in node.depends_on
        )

    @property
    def depth(self) -> int:
        """The number of nodes on the longest dependency chain."""
        levels: dict[str, int] = {}
        for node in self.nodes:
            levels[node.key] = 1 + max((levels[d] for d in node.depends_on), default=0)
        return max(levels.values())

    def ordinal(self, key: str) -> int:
        for index, node in enumerate(self.nodes):
            if node.key == key:
                return index
        raise KeyError(key)
