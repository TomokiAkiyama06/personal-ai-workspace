"""Memory Conflict / Versioning / Freshness (PAW-042).

* ``service.MemoryVersioningService``: a person's changes of User and Project
  Memory. Every change is a new version (the old one stays as history), guarded by
  an Optimistic Lock; relations ``supersedes`` / ``extends`` / ``conflicts_with``
  between memories; revalidation of a stale candidate.
* ``freshness.FreshnessMaintenance``: the backend-internal jobs of the freshness
  policies (``revalidate`` and ``repo_commit`` stale candidates, ``expiring``
  expiry, ``session_only`` at session or task end).
* ``rules``: the pure decisions, including what a classification of a new memory
  against an old one (``same`` / ``extends`` / ``supersedes`` / ``conflicts`` /
  ``unrelated``) leads to.

Retrieval (PAW-043) offers only ``active`` versions; nothing here changes that. The
open choices are decided in Decision 0034 (Approved 2026-09-28). See
``apps/backend/README.md`` ("Memory Versioning / Freshness").
"""

from paw_backend.memory.versioning.errors import (
    InputProblem,
    InvalidMemoryInputError,
    MemoryBusyError,
    MemoryDatabaseError,
    MemoryNotFoundError,
    MemoryPermissionError,
    MemoryScopeNotSupportedError,
    MemoryStateError,
    MemoryVersionConflictError,
    MemoryVersioningError,
    StateProblem,
)
from paw_backend.memory.versioning.freshness import FreshnessMaintenance
from paw_backend.memory.versioning.records import (
    EDITABLE_SCOPES,
    FreshnessSpec,
    ManualRelation,
    MemoryChanges,
    MemoryDraft,
    MemoryRelationView,
    MemoryVersionView,
    RelationClassification,
    RelationPlan,
    RevalidateTrigger,
    TargetKind,
    TriggerTarget,
)
from paw_backend.memory.versioning.rules import plan_relation
from paw_backend.memory.versioning.service import (
    RELATION_GRAPH_LOCK_KEY,
    MemoryVersioningService,
    memory_lock_key,
)

__all__ = [
    "EDITABLE_SCOPES",
    "FreshnessMaintenance",
    "FreshnessSpec",
    "InputProblem",
    "InvalidMemoryInputError",
    "ManualRelation",
    "MemoryBusyError",
    "MemoryChanges",
    "MemoryDatabaseError",
    "MemoryDraft",
    "MemoryNotFoundError",
    "MemoryPermissionError",
    "MemoryRelationView",
    "MemoryScopeNotSupportedError",
    "MemoryStateError",
    "MemoryVersionConflictError",
    "MemoryVersionView",
    "MemoryVersioningError",
    "MemoryVersioningService",
    "RELATION_GRAPH_LOCK_KEY",
    "RelationClassification",
    "RelationPlan",
    "RevalidateTrigger",
    "StateProblem",
    "TargetKind",
    "TriggerTarget",
    "memory_lock_key",
    "plan_relation",
]
