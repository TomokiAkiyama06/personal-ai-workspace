"""SQL conditions that decide which rows a principal may read.

The Memory schema is filtered by the database, not by the application after
the fact. Every read of ``memory_versions`` (and of the tables that join it:
``memory_embeddings``, ``memory_sources``, ``memory_relations``) must contain
:func:`readable_memory_versions`; every read of ``conversations`` must contain
:func:`readable_conversations`. Apply it before ranking, so that a vector
search can never rank (and leak) a row the principal cannot see.

A :class:`Principal` is the user's *effective grants*, worked out by the
Backend from RBAC and project / repository membership (PAW-025 / 026 / 027),
never taken from a client, an LLM or a prompt:

* ``project_ids``: projects the user may read (member).
* ``project_group_ids``: project groups whose memories the user may read. What
  a group is and who belongs to it is not defined by the requirements yet, so
  the caller resolves it; no id means no group memory. A user's membership of a
  project that is in a group does not, by itself, put the group here.
* ``repo_ids``: repositories whose Repo Memory the user may read. A repository
  inherits its project's permission unless it has an ACL override, so a
  repository the user is barred from must be left out even when the user is a
  member of its project (REQUIREMENTS.md "Project / Repo Permission
  Inheritance"). The same condition serves the ``agent`` permission by
  building the principal from the repositories an agent may use.

The ACL is derived from the scope columns of each version. There is no
per-memory grant table on purpose: a copy of the permissions on every memory
would go stale when a member or an override changes, and a stale grant leaks.
A memory shared with named users would need a new table (a later migration).

Only active users may hold a principal: the ``shared`` scope is readable by
every principal (MEMORY_ARCHITECTURE.md "Shared Memory permissions").
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_

from paw_backend.memory.models import Conversation, MemoryScope, MemoryVersion


def _uuids(values: Iterable[UUID], name: str) -> frozenset[UUID]:
    ids = frozenset(values)
    if not all(isinstance(value, UUID) for value in ids):
        raise TypeError(f"{name} must contain UUIDs only")
    return ids


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    project_ids: frozenset[UUID] = frozenset()
    repo_ids: frozenset[UUID] = frozenset()
    project_group_ids: frozenset[UUID] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, UUID):
            raise TypeError("user_id must be a UUID")
        # Frozen dataclass: normalise through object.__setattr__.
        object.__setattr__(self, "project_ids", _uuids(self.project_ids, "project_ids"))
        object.__setattr__(self, "repo_ids", _uuids(self.repo_ids, "repo_ids"))
        object.__setattr__(
            self,
            "project_group_ids",
            _uuids(self.project_group_ids, "project_group_ids"),
        )


def readable_memory_versions(
    principal: Principal, version: Any = MemoryVersion
) -> ColumnElement[bool]:
    """Condition: ``principal`` may read the row of ``version``.

    Pass an ``aliased(MemoryVersion)`` as ``version`` to filter a joined copy.
    An empty ``project_ids`` / ``project_group_ids`` / ``repo_ids`` matches no
    project / group / repo row.
    """
    return or_(
        and_(
            version.scope == MemoryScope.USER,
            version.owner_user_id == principal.user_id,
        ),
        and_(
            version.scope == MemoryScope.PROJECT,
            version.project_id.in_(sorted(principal.project_ids)),
        ),
        and_(
            version.scope == MemoryScope.PROJECT_GROUP,
            version.project_group_id.in_(sorted(principal.project_group_ids)),
        ),
        and_(
            version.scope == MemoryScope.REPO,
            version.repo_id.in_(sorted(principal.repo_ids)),
        ),
        version.scope == MemoryScope.SHARED,
    )


def readable_conversations(
    principal: Principal, conversation: Any = Conversation
) -> ColumnElement[bool]:
    """Condition: ``principal`` owns the conversation (raw conversations are private).

    Nobody else, including an Admin or Owner, reads another user's raw
    conversation through this condition. ``session_states`` and ``messages``
    follow their conversation: join it and apply this condition.
    """
    return conversation.owner_user_id == principal.user_id


__all__ = [
    "Principal",
    "readable_conversations",
    "readable_memory_versions",
]
