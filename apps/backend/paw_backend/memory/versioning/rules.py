"""The decisions of Memory versioning, as pure functions (PAW-042). No database.

Keeping them apart from the SQL lets ``tests/test_memory_versioning_rules.py`` try
every branch with plain values, and lets a reviewer read the policy in one place.
The sources are REQUIREMENTS.md "Memory Conflict / Versioning / Retrieval",
"Memory Freshness / Revalidate Policy", "Manual Memory Editing / Concurrency" and
docs/MEMORY_ARCHITECTURE.md sections 10, 11, 15 and 17; the choices they leave
open are decided in Decision 0034 (Approved 2026-09-28):

* **Versions are never overwritten.** An edit, a restore and a revalidation each
  write version ``n + 1``; the version they replace becomes ``superseded`` and a
  ``supersedes`` relation points from the new to the old one.
* **A person's edit is confirmed.** The new version is ``confirmed`` with the
  person as its actor; when the version it replaces was not confirmed (``observed``
  or ``inferred``, a worker's candidate) a ``confirmed_from`` relation records the
  promotion as well.
* **Relations.** ``supersedes`` retires the older memory and is allowed only
  between memories with the same audience (the same scope and scope ids): a
  replacement must not hide a memory from people who cannot see its successor.
  ``extends`` and ``conflicts_with`` leave both memories ``active``; a conflict is
  what retrieval shows as a conflict until a person resolves it (by a
  ``supersedes`` or by deprecating one of them).
* **Freshness** is chosen per memory. A person may write ``permanent``,
  ``revalidate`` and ``expiring`` memories. ``repo_commit`` belongs to Repo Memory
  (whose manual editing is not part of this issue) and ``session_only`` is never
  Long-term Memory, so neither is written by a manual edit.
"""

from datetime import datetime

from paw_backend.memory.models import (
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
    MemoryStatus,
    RelationType,
)
from paw_backend.memory.versioning import limits
from paw_backend.memory.versioning.errors import (
    InputProblem,
    MemoryStateError,
    StateProblem,
)
from paw_backend.memory.versioning.records import (
    FreshnessSpec,
    ManualRelation,
    MemoryChanges,
    MemoryVersionView,
    RelationClassification,
    RelationPlan,
)
from paw_backend.memory.versioning.validation import reject

# What each classification of REQUIREMENTS.md means (Decision 0034, 1).
_PLANS = {
    # The existing memory already says it: nothing new is stored.
    RelationClassification.SAME: RelationPlan(
        writes_new=False, relation=None, retires_older=False, needs_confirmation=False
    ),
    # Adds to the existing memory, which stays valid.
    RelationClassification.EXTENDS: RelationPlan(
        writes_new=True,
        relation=RelationType.EXTENDS,
        retires_older=False,
        needs_confirmation=False,
    ),
    # A clear replacement: the new one is active, the old one superseded.
    RelationClassification.SUPERSEDES: RelationPlan(
        writes_new=True,
        relation=RelationType.SUPERSEDES,
        retires_older=True,
        needs_confirmation=False,
    ),
    # An ambiguous contradiction: both stay, and the person decides.
    RelationClassification.CONFLICTS: RelationPlan(
        writes_new=True,
        relation=RelationType.CONFLICTS_WITH,
        retires_older=False,
        needs_confirmation=True,
    ),
    RelationClassification.UNRELATED: RelationPlan(
        writes_new=True, relation=None, retires_older=False, needs_confirmation=False
    ),
}

# Relations whose edges must never form a cycle: each says "newer comes from older".
ACYCLIC_RELATIONS = frozenset(
    {
        RelationType.SUPERSEDES,
        RelationType.EXTENDS,
        RelationType.CONFIRMED_FROM,
        RelationType.REVALIDATED_FROM,
        RelationType.MERGED_FROM,
    }
)

EDITABLE_FIELDS = ("title", "content", "memory_type", "importance", "freshness")


def plan_relation(classification: RelationClassification) -> RelationPlan:
    """What a classification of a new memory against an old one leads to."""
    return _PLANS[RelationClassification(classification)]


def check_manual_freshness(
    spec: FreshnessSpec, scope: MemoryScope, now: datetime
) -> None:
    """Refuse a freshness a person may not write for ``scope`` at ``now``.

    ``session_only`` is never Long-term Memory; ``repo_commit`` needs a repository
    (a Repo Memory), and ``scope`` is never ``repo`` here. An ``expiring`` memory
    must expire after ``now`` and at most ``MAX_EXPIRES_IN`` ahead.
    """
    if spec.policy is FreshnessPolicy.SESSION_ONLY:
        raise reject("freshness", InputProblem.NOT_ALLOWED)
    if spec.policy is FreshnessPolicy.REPO_COMMIT and scope is not MemoryScope.REPO:
        raise reject("freshness", InputProblem.NOT_ALLOWED)
    if spec.policy is FreshnessPolicy.EXPIRING:
        assert spec.expires_at is not None  # FreshnessSpec requires it
        if not now < spec.expires_at <= now + limits.MAX_EXPIRES_IN:
            raise reject("expires_at", InputProblem.OUT_OF_RANGE)


def same_freshness(version: MemoryVersionView, spec: FreshnessSpec) -> bool:
    """Does ``spec`` describe the freshness ``version`` already has?"""
    return (
        version.freshness_policy is spec.policy
        and version.revalidate_after == spec.revalidate_after
        and tuple(sorted(version.revalidate_triggers))
        == tuple(sorted(str(t) for t in spec.revalidate_triggers))
        and version.expires_at == spec.expires_at
        and version.commit_sha == spec.commit_sha
        and version.branch == spec.branch
    )


def changed_fields(
    current: MemoryVersionView, changes: MemoryChanges
) -> tuple[str, ...]:
    """The names of the fields ``changes`` really changes, in a fixed order."""
    changed: list[str] = []
    for name in ("title", "content", "memory_type", "importance"):
        value = getattr(changes, name)
        if value is not None and value != getattr(current, name):
            changed.append(name)
    if changes.freshness is not None and not same_freshness(current, changes.freshness):
        changed.append("freshness")
    if changes.scope is not None and changes.scope is not current.scope:
        changed.append("scope")
    return tuple(changed)


# The narrowings an edit may make (REQUIREMENTS.md "Scope変更"): from the scope to
# the scope the new version gets. ``project`` to ``user`` is the only one among the
# scopes this service edits; every other change of scope widens (or moves) the
# audience and needs the confirmation flow.
NARROWINGS = frozenset({(MemoryScope.PROJECT, MemoryScope.USER)})


def check_narrowing(current: MemoryScope, new: MemoryScope) -> None:
    """Refuse a change of scope that is not a narrowing (``NOT_ALLOWED``)."""
    if (current, new) not in NARROWINGS:
        raise reject("scope", InputProblem.NOT_ALLOWED)


def check_active(current: MemoryVersionView) -> None:
    """Only the ``active`` version of a memory is edited, retired or revalidated."""
    if current.status is not MemoryStatus.ACTIVE:
        raise MemoryStateError(StateProblem.NOT_ACTIVE)


def check_revalidatable(current: MemoryVersionView) -> None:
    check_active(current)
    if current.freshness_policy is not FreshnessPolicy.REVALIDATE:
        raise MemoryStateError(StateProblem.NOT_REVALIDATABLE)


def check_restorable(current: MemoryVersionView, source: MemoryVersionView) -> None:
    """A restore needs a current version that is active or deprecated.

    Restoring the content the current active version already has changes nothing
    and is refused (``ALREADY_ACTIVE``). A superseded or history current version
    (the memory was replaced by another one) is ``NOT_ACTIVE``.
    """
    if current.status not in (MemoryStatus.ACTIVE, MemoryStatus.DEPRECATED):
        raise MemoryStateError(StateProblem.NOT_ACTIVE)
    if source.version_id == current.version_id and (
        current.status is MemoryStatus.ACTIVE
    ):
        raise MemoryStateError(StateProblem.ALREADY_ACTIVE)


def check_relatable(
    relation: ManualRelation, newer: MemoryVersionView, older: MemoryVersionView
) -> None:
    """Refuse a manual relation between the current versions of two memories."""
    if newer.memory_id == older.memory_id:
        raise MemoryStateError(StateProblem.SAME_MEMORY)
    check_active(newer)
    check_active(older)
    if relation is ManualRelation.SUPERSEDES and newer.audience != older.audience:
        raise MemoryStateError(StateProblem.SCOPE_MISMATCH)


def confirmation_after_edit(current: MemoryVersionView) -> tuple[RelationType, ...]:
    """The relations an edit adds besides ``supersedes`` (a promotion if any)."""
    if current.confirmation_state is ConfirmationState.CONFIRMED:
        return ()
    return (RelationType.CONFIRMED_FROM,)
