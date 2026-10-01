"""Value objects of the Memory Board read model (issue #186). No database, no I/O."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from paw_backend.memory.models import RelationType, SourceType
from paw_backend.memory.versioning.records import MemoryVersionView


class BoardScopeKind(StrEnum):
    """The scopes of the Scope pane (UI_DESIGN.md section 6.1)."""

    USER = "user"
    PROJECT = "project"
    REPO = "repo"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class BoardScope:
    """One node of the Scope pane: what ``MemoryBoard.list_memories`` lists.

    ``project`` is the project-wide memories (scope ``project``) of
    ``project_id``; ``repo`` the Repo Memory of ``repo_id``, a repository of
    ``project_id``.
    """

    kind: BoardScopeKind
    project_id: UUID | None = None
    repo_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RepoScopeCount:
    repo_id: UUID
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class ProjectScopeCount:
    project_id: UUID
    name: str
    # Every listed memory of the project, its repositories' included.
    count: int
    # The project-wide memories (scope ``project``).
    project_count: int
    repos: tuple[RepoScopeCount, ...]


@dataclass(frozen=True, slots=True)
class ScopeTree:
    """What the reader may see, with the number of memories of each scope."""

    user: int
    projects: tuple[ProjectScopeCount, ...]
    shared: int


@dataclass(frozen=True, slots=True)
class BoardVersion:
    """A version with the display name of the person who wrote it (``None``:
    not a person, or a person whose account was deleted)."""

    version: MemoryVersionView
    actor_name: str | None


@dataclass(frozen=True, slots=True)
class MemoryList:
    """The current version of each memory of one scope.

    ``truncated``: more memories matched than ``limits.MAX_LIST_ITEMS``; the
    newest are returned.
    """

    memories: tuple[BoardVersion, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class BoardRelation:
    """An edge of the History Graph, from the newer version to the older one."""

    from_version_id: UUID
    to_version_id: UUID
    relation: RelationType
    reason: str | None


@dataclass(frozen=True, slots=True)
class BoardHistory:
    """The History Graph of one memory (see ``MemoryBoard.history``)."""

    versions: tuple[BoardVersion, ...]
    relations: tuple[BoardRelation, ...]
    related: tuple[BoardVersion, ...]
    can_write: bool


@dataclass(frozen=True, slots=True)
class BoardSource:
    """A ``memory_sources`` row of a version (UI_DESIGN.md section 8)."""

    source_type: SourceType
    conversation_id: UUID | None
    message_id: UUID | None
    source_ref: str | None
    source_deleted_at: datetime | None
    created_at: datetime
