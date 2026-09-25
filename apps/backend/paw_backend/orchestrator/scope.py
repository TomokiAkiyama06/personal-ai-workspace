"""The scope and the grant of a sub-agent are derived from its parent's (PAW-034).

A node never gets more than the task it belongs to (``REQUIREMENTS.md``: "親Task
のBudget / Permission / ACLをSub-Agentが超えることはできない"):

* **Grant**: ``authz.delegation.derive_child_grant`` (a subset of the parent's, and
  only of what an agent can exercise). :func:`node_grant` picks what to ask for:
  the plan's request when it made one (asking for more than the parent holds is an
  error, never clipped), else the role's ceiling narrowed to what the parent holds.
* **Scope**: :func:`derive_child_scope` copies the parent's scope and narrows it:
  the repositories to the ones the node asked for (an id outside the parent's
  working set is an error), the credential handles to none for a read-only role.
  The repositories keep the resolved ACL and the registered remotes the caller
  gave the parent: a node cannot change them.
  :func:`scope_within` is the check the orchestrator runs on every derived scope
  (defence in depth) and that the tests run on hand-made ones.

The parent's grant and scope are supplied by the caller for every call
(``TaskAuthority``): the working set is where the repositories come from until
issue #85 persists it (Decision 0014), and where the caller registers the
**remotes** of each repository (Decision 0006, section 8 (d): a repository without
a registered remote lets no call with a URL through).
"""

import uuid
from collections.abc import Sequence

from paw_backend.authz import AgentGrant, derive_child_grant
from paw_backend.orchestrator.domain import (
    ROLE_CEILING,
    ROLES_WITH_CREDENTIALS,
    NodeRole,
)
from paw_backend.orchestrator.errors import ScopeEscalationError
from paw_backend.orchestrator.records import NodeRecord
from paw_backend.tools import TaskScope
from paw_backend.tools.scope import path_within

# Sub-agent identities are derived, never random: the same node attempt of the
# same task is always the same agent in an audit row.
_AGENT_NAMESPACE = uuid.UUID("6f0a7c3e-3a58-4d0b-9c7e-0d34f5b1a034")


def agent_id_of(task_id: uuid.UUID, node_key: str, attempt: int) -> uuid.UUID:
    """The id of the agent that plays one attempt of one node."""
    return uuid.uuid5(_AGENT_NAMESPACE, f"{task_id}/{node_key}/{attempt}")


def node_grant(
    parent: AgentGrant, node: NodeRecord | None, role: NodeRole, agent_id: uuid.UUID
) -> AgentGrant:
    """The grant of the agent that plays ``node`` (or, for ``node=None``, a role
    without a plan node, such as the planner). Raises ``GrantEscalationError`` when
    the plan asked for more than the parent holds."""
    if node is not None and node.capabilities is not None:
        wanted = frozenset(node.capabilities)
    else:
        wanted = ROLE_CEILING[role] & parent.capabilities
    return derive_child_grant(parent, child_agent_id=agent_id, capabilities=wanted)


def derive_child_scope(
    parent: TaskScope,
    *,
    role: NodeRole,
    repositories: Sequence[uuid.UUID] | None,
) -> TaskScope:
    """The scope of a node of ``role`` that asked for ``repositories`` (``None``:
    the whole working set). Raises ``ScopeEscalationError`` for a repository that
    is not in the parent's working set."""
    if not isinstance(parent, TaskScope):
        raise TypeError("parent must be a TaskScope")
    if repositories is None:
        chosen = parent.repositories
    else:
        found = []
        for repo_id in repositories:
            repository = parent.repository(repo_id)
            if repository is None:
                raise ScopeEscalationError()
            found.append(repository)
        chosen = tuple(found)
    child = TaskScope(
        path_roots=parent.path_roots,
        hosts=parent.hosts,
        projects=dict(parent.projects),
        credential_handles=(
            dict(parent.credential_handles) if role in ROLES_WITH_CREDENTIALS else {}
        ),
        repositories=chosen,
    )
    if not scope_within(child, parent):
        raise ScopeEscalationError()
    return child


def scope_within(child: TaskScope, parent: TaskScope) -> bool:
    """Whether ``child`` reaches nothing that ``parent`` does not: every path root
    is a parent root or lies below one, every host and project (with the same
    state) is the parent's, every credential handle is the parent's with no more
    hosts, and every repository is the parent's, unchanged (its worktree, its ACL
    and its remotes)."""
    if not all(
        any(path_within(root, allowed) for allowed in parent.path_roots)
        for root in child.path_roots
    ):
        return False
    if not child.hosts <= parent.hosts:
        return False
    if any(parent.projects.get(pid) != state for pid, state in child.projects.items()):
        return False
    for handle, hosts in child.credential_handles.items():
        allowed = parent.credential_handles.get(handle)
        if allowed is None or not hosts <= allowed:
            return False
    return all(
        parent.repository(repository.repo_id) == repository
        for repository in child.repositories
    )
