"""Roles, states and the capability ceiling of each role (PAW-034).

No database and no I/O. ``REQUIREMENTS.md`` ("Agent Orchestration / Parallel-first
execution") defines the four logical roles; what each may do is data here
(``ROLE_CEILING``), a product choice listed in Decision 0021 for the human.
"""

from enum import StrEnum
from types import MappingProxyType

from paw_backend.authz import Capability


class NodeRole(StrEnum):
    """The logical roles of a node (REQUIREMENTS.md, "Logical roles")."""

    PLANNER = "planner"  # decomposition, working-set candidates; read-only
    WORKER = "worker"  # implementation, fixes, tests; may write
    RESEARCHER = "researcher"  # repository / docs / web research; read-only
    REVIEWER = "reviewer"  # patch / test / spec conformance; read-only


class NodeState(StrEnum):
    PENDING = "pending"  # waits for a dependency that has not succeeded yet
    READY = "ready"  # every dependency succeeded; can be started
    RUNNING = "running"  # an attempt is in progress
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # gave up: retries, alternatives and escalations are used up
    BLOCKED = "blocked"  # a dependency failed, was blocked or cancelled
    CANCELLED = "cancelled"


# A node in one of these states never changes by itself. FAILED, BLOCKED and
# CANCELLED nodes come back only when a Retry of the task re-opens the DAG.
SETTLED_NODE_STATES = frozenset(
    {
        NodeState.SUCCEEDED,
        NodeState.FAILED,
        NodeState.BLOCKED,
        NodeState.CANCELLED,
    }
)
# The states from which a dependent cannot start.
UNSATISFIABLE_NODE_STATES = frozenset(
    {NodeState.FAILED, NodeState.BLOCKED, NodeState.CANCELLED}
)


class DagState(StrEnum):
    ACTIVE = "active"  # nodes are still to run (or the task is waiting)
    SUCCEEDED = "succeeded"  # every required node succeeded
    FAILED = "failed"  # nothing can run any more and a required node did not succeed
    CANCELLED = "cancelled"  # the task was cancelled


class AttemptState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Cut short without an outcome: the worker died, the task was paused or
    # cancelled, or the lease was lost. Not a failure of the node.
    INTERRUPTED = "interrupted"


class NextStep(StrEnum):
    """What happens to a node after a failed attempt."""

    RETRY = "retry"  # the same agent, the same approach
    ALTERNATIVE = "alternative"  # the same agent, another approach
    ESCALATE = "escalate"  # the next agent of the ladder
    GIVE_UP = "give_up"  # the node fails; its dependents are blocked
    HOLD = "hold"  # keep the node ready: the task waits for a human


class RunOutcome(StrEnum):
    """What one ``Orchestrator.run_entry`` did with the entry it was given."""

    IDLE = "idle"  # nothing to claim
    SKIPPED = "skipped"  # the task is not in a state that can run
    LEASE_LOST = "lease_lost"  # the worker lost its lease and stopped writing
    DAG_SUCCEEDED = "dag_succeeded"  # every required node succeeded: evaluating
    DAG_FAILED = "dag_failed"  # a required node did not succeed: the task failed
    PLAN_FAILED = "plan_failed"  # no acceptable plan: the task failed
    WAITING_FOR_USER = "waiting_for_user"  # budget or loop: a human decides
    BUDGET_FAILED = "budget_failed"  # retries used up: the task failed
    BUDGET_NOT_CONFIGURED = "budget_not_configured"  # no preset: the task failed
    PAUSED = "paused"  # the task was paused: quiesced, entry completed
    TASK_ENDED = "task_ended"  # cancelled / failed / completed under the run
    SUPERSEDED = "superseded"  # a Retry / Restart replaced the run
    # An unexpected error or state: the task was failed safely (or, when even that
    # could not be written, the entry was left claimed for the next worker).
    ERROR = "error"


# -- what a role may hold ----------------------------------------------------------
_C = Capability
_READ_ONLY = frozenset({_C.PROJECT_READ, _C.PROJECT_MEMORY_USE, _C.SHARED_MEMORY_READ})

# The most a node of the role can be granted (``docs/decisions/0021-*.md``,
# section 2). The Planner, the Researcher and the Reviewer are read-only: the
# backend guarantees it by never granting them a capability that writes. Only the
# Worker may write to a repository. Creating a pull request stays out of every
# role: integration is PAW-035's. A node's grant is the intersection of this
# ceiling, what the plan asks for and what its parent holds.
ROLE_CEILING: MappingProxyType[NodeRole, frozenset[Capability]] = MappingProxyType(
    {
        NodeRole.PLANNER: _READ_ONLY,
        NodeRole.RESEARCHER: _READ_ONLY,
        NodeRole.REVIEWER: _READ_ONLY,
        NodeRole.WORKER: _READ_ONLY | {_C.PROJECT_TASK_RUN, _C.PROJECT_REPO_WRITE},
    }
)
# A credential (a handle to a secret) is for writing to an outside system; the
# read-only roles get none. The Worker gets the handles of the task.
ROLES_WITH_CREDENTIALS = frozenset({NodeRole.WORKER})
del _C

WRITING_CAPABILITIES = frozenset(
    {Capability.PROJECT_REPO_WRITE, Capability.PROJECT_PR_CREATE, Capability.PR_CREATE}
)
