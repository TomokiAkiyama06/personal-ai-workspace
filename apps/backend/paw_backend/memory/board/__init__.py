"""The Memory Board read model (issue #186): the Memory screen's reads.

``service.MemoryBoard`` answers the Scope Tree with counts, the current version of
each memory of a scope (with a search), the History Graph of one memory and the
sources of one version, for an authenticated person, through the ACL in SQL
(``memory/acl.py``). The writes (edit, restore) stay in
``versioning.MemoryVersioningService``. The choices are Decision 0068 (Proposed).
"""

from paw_backend.memory.board.records import (
    BoardHistory,
    BoardRelation,
    BoardScope,
    BoardScopeKind,
    BoardSource,
    BoardVersion,
    MemoryList,
    ProjectScopeCount,
    RepoScopeCount,
    ScopeTree,
)
from paw_backend.memory.board.service import MemoryBoard

__all__ = [
    "BoardHistory",
    "BoardRelation",
    "BoardScope",
    "BoardScopeKind",
    "BoardSource",
    "BoardVersion",
    "MemoryBoard",
    "MemoryList",
    "ProjectScopeCount",
    "RepoScopeCount",
    "ScopeTree",
]
