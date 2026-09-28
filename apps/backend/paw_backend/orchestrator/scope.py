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
  The repositories keep the resolved ACL, the registered remotes and the
  Working Set role (issue #85) the caller gave the parent: a node cannot change
  them.
  :func:`scope_within` is the check the orchestrator runs on every derived scope
  (defence in depth) and that the tests run on hand-made ones.

The parent's grant and scope are supplied by the caller for every call
(``TaskAuthority``): it builds the repositories from the stored Working Set
(issue #85, Decision 0030: each with its role, ``tools.scope.with_working_set_roles``)
and registers the **remotes** of each repository (Decision 0006, section 8 (d): a
repository without a registered remote lets no call with a URL through).
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
from paw_backend.tasks import TaskRun
from paw_backend.tools import TaskScope
from paw_backend.tools.scope import path_within

# Sub-agent identities are derived, never random: the same node attempt of the
# same run of the task is always the same agent in an audit row, and no two
# attempts share one.
_AGENT_NAMESPACE = uuid.UUID("6f0a7c3e-3a58-4d0b-9c7e-0d34f5b1a034")


def agent_id_of(
    task_id: uuid.UUID,
    run: TaskRun,
    node_key: str,
    attempt: int,
    *,
    claim: tuple[int, int] | None = None,
) -> uuid.UUID:
    """The id of the agent that plays one attempt of one node (Decision 0021,
    section 3: derived from the task, the node and the attempt).

    The attempt is named in full, so that no two attempts share an agent:

    * ``run`` (the task attempt and retry count, ``TaskRun``): a Restart gives the
      task a new DAG whose node attempts count from 1 again, and a Retry runs the
      planner again from its first attempt;
    * ``claim`` (the queue entry id and its ``claim_count``), for the planner only:
      its attempts are counted by the worker that plans, so a worker that takes the
      run over (a new claim), or a new entry of the same run (after a Resume),
      counts from 1 again. The attempts of a node are counted in the DAG
      (``attempt_count``, never reset within a task attempt) and need no claim.
    """
    parts = [str(task_id), f"{run.attempt}.{run.retry_count}", node_key]
    if claim is not None:
        entry_id, claim_count = claim
        parts.append(f"claim-{entry_id}.{claim_count}")
    parts.append(str(attempt))
    return uuid.uuid5(_AGENT_NAMESPACE, "/".join(parts))


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
    excluded = parent.excluded_repositories
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
        # The repositories left out stay KNOWN to the child as excluded: the
        # child keeps the parent's path roots and hosts (a repository's worktree
        # usually lies below a root, its remote on an allowed host), and without
        # this a path or a URL of a left-out repository would be in scope and
        # attributed to no repository, so no ACL would be asked. Fail closed.
        chosen_ids = {repository.repo_id for repository in chosen}
        excluded = excluded + tuple(
            repository
            for repository in parent.repositories
            if repository.repo_id not in chosen_ids
        )
    child = TaskScope(
        path_roots=parent.path_roots,
        hosts=parent.hosts,
        projects=dict(parent.projects),
        credential_handles=(
            dict(parent.credential_handles) if role in ROLES_WITH_CREDENTIALS else {}
        ),
        repositories=chosen,
        excluded_repositories=excluded,
    )
    if not scope_within(child, parent):
        raise ScopeEscalationError()
    return child


def scope_within(child: TaskScope, parent: TaskScope) -> bool:
    """Whether ``child`` reaches nothing that ``parent`` does not: every path root
    is a parent root or lies below one, every host and project (with the same
    state) is the parent's, every credential handle is the parent's with no more
    hosts, and every repository is the parent's, unchanged (its worktree, its ACL,
    its remotes and its Working Set role)."""
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
    if not all(
        parent.repository(repository.repo_id) == repository
        for repository in child.repositories
    ):
        return False
    # What the parent may not touch, the child may not either.
    return all(
        repository in child.excluded_repositories
        for repository in parent.excluded_repositories
    )
