"""The independent reviews the backend adds to a plan (Decision 0099, 5).

When a plan of a task that writes (not ``read_only``) is accepted and it has at
least one ``worker`` node, the backend adds two ``reviewer`` nodes of its own,
whatever the planner wrote (:func:`add_required_reviews`):

* ``paw-review-claude`` on ``claude`` and ``paw-review-codex`` on ``codex``;
* both ``required``, both depend on every Worker node, neither on the other
  (they run in parallel and never see each other's result);
* their title and goal are fixed texts (the goal quotes the task's title and
  names the Worker nodes).

A key that starts with ``paw-`` is the backend's: a planner's plan with one is
refused (:func:`refuse_reserved_keys`, ``reserved_key``), so a planner can
neither write nor replace a required review, nor remove one (a planner's own
reviewer nodes are extra). The node limit (``MAX_NODES``) and the other limits
are checked on the plan with the reviews added: a plan they push over a limit
is refused like any other unacceptable plan.

This is not a node adding nodes (Decision 0021, 1): the backend adds them once,
in a fixed form, when it accepts the plan.

Whether the reviews are added at all is the deployment's: only when the
reviewer role may be assigned both ``claude`` and ``codex``
(:func:`adds_required_reviews`, ``[orchestrator.assignment]``, which the
runtime configuration requires once it is written, Decision 0099, 7). Without
it (the CLI rungs not enabled yet) a write task keeps the plan the planner
made: a review that could never run would fail every write task.

The independence rule (Decision 0099, 5) is the orchestrator's: a required
review never runs on, nor escalates to, the other required review's agent or
an agent that ran an upstream Worker (:func:`forbidden_agents`).
"""

import re
from collections.abc import Collection, Iterable, Mapping
from types import MappingProxyType

from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import InvalidPlanError, PlanReason
from paw_backend.orchestrator.plan import Plan, PlanNode

#: The prefix of the backend's own node keys.
RESERVED_KEY_PREFIX = "paw-"
CLAUDE_REVIEW = "paw-review-claude"
CODEX_REVIEW = "paw-review-codex"
#: The required reviews: the node key and the agent it runs on.
REQUIRED_REVIEWS: Mapping[str, str] = MappingProxyType(
    {CLAUDE_REVIEW: "claude", CODEX_REVIEW: "codex"}
)
REVIEW_AGENTS = frozenset(REQUIRED_REVIEWS.values())
_TITLES = MappingProxyType(
    {
        CLAUDE_REVIEW: "Independent review by Claude",
        CODEX_REVIEW: "Independent review by Codex",
    }
)
# What of the task's title is quoted in a goal: one line, no control characters.
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
_QUOTED_TITLE_CHARS = 200

GOAL_TEMPLATE = (
    'Review, on your own, the changes the worker nodes {workers} made for the task "'
    '{title}". Their work is merged into your read-only checkout of each repository; '
    "read the change with git from the review base to HEAD (the base is named "
    "with the repository, for example `git log -p <review base>..HEAD` or "
    "`git diff <review base> HEAD`). Check that the change does what the task "
    "asks, that it is correct and tested, and that it adds no security problem. "
    "You cannot change anything. Submit a summary of your review, and put every "
    "problem you found in unresolved_questions."
)


def is_reserved_key(key: str) -> bool:
    return key.startswith(RESERVED_KEY_PREFIX)


def is_required_review(key: str) -> bool:
    """Whether ``key`` is one of the backend's required reviews."""
    return key in REQUIRED_REVIEWS


def adds_required_reviews(allowed: Iterable[str]) -> bool:
    """Whether a deployment whose reviewer role may be assigned ``allowed``
    adds the required reviews (both of their agents are allowed)."""
    return REVIEW_AGENTS <= set(allowed)


def refuse_reserved_keys(plan: Plan) -> Plan:
    """``plan`` itself if no key of it is the backend's, else
    ``InvalidPlanError`` (``RESERVED_KEY``)."""
    if any(is_reserved_key(node.key) for node in plan.nodes):
        raise InvalidPlanError(PlanReason.RESERVED_KEY)
    return plan


def _quoted(title: str) -> str:
    text = " ".join(_UNPRINTABLE.sub(" ", title).split())
    if len(text) > _QUOTED_TITLE_CHARS:
        text = text[: _QUOTED_TITLE_CHARS - 3] + "..."
    return text.replace('"', "'")


def review_node(key: str, *, workers: Collection[str], task_title: str) -> PlanNode:
    """The required review ``key`` of a plan whose Worker nodes are ``workers``."""
    return PlanNode(
        key=key,
        role=NodeRole.REVIEWER,
        title=_TITLES[key],
        goal=GOAL_TEMPLATE.format(
            workers=", ".join(workers), title=_quoted(task_title)
        ),
        depends_on=tuple(workers),
        required=True,
        agent=REQUIRED_REVIEWS[key],
    )


def add_required_reviews(plan: Plan, *, task_title: str) -> Plan:
    """``plan`` with the two required reviews added after every Worker node (see
    the module); ``plan`` itself when it has no Worker node. The planner's own
    keys must not be reserved (:func:`refuse_reserved_keys` first).
    ``InvalidPlanError`` when the added nodes push the plan over a limit (the
    nodes, a node's dependencies, the edges, the depth or the size)."""
    workers = [node.key for node in plan.nodes if node.role is NodeRole.WORKER]
    if not workers:
        return plan
    refuse_reserved_keys(plan)
    reviews = tuple(
        review_node(key, workers=workers, task_title=task_title)
        for key in REQUIRED_REVIEWS
    )
    return Plan((*plan.nodes, *reviews))


def forbidden_agents(key: str, worker_agents: Iterable[str]) -> frozenset[str]:
    """The agents the required review ``key`` must not run on (Decision 0099,
    5): the other required review's and every agent that actually ran an
    upstream Worker (``worker_agents``: their attempts' ``placement_agent``)."""
    own = REQUIRED_REVIEWS[key]
    return frozenset(
        {agent for agent in REVIEW_AGENTS if agent != own} | set(worker_agents)
    )


__all__ = [
    "CLAUDE_REVIEW",
    "CODEX_REVIEW",
    "GOAL_TEMPLATE",
    "REQUIRED_REVIEWS",
    "RESERVED_KEY_PREFIX",
    "REVIEW_AGENTS",
    "add_required_reviews",
    "adds_required_reviews",
    "forbidden_agents",
    "is_required_review",
    "is_reserved_key",
    "refuse_reserved_keys",
    "review_node",
]
