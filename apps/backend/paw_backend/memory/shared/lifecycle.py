"""Lifecycle rules of Shared Memory: pure functions, no database, no clock.

These are the *rules* of the candidate state machine and of version handling.
The service (``service.py``) does the transactions, locks and SQL; at each
decision point it calls one of these functions and follows the answer. A rule
function

* never touches a database, the clock, a file or a global;
* never changes its arguments (every value object is frozen);
* raises only the error named in its docstring, with the ``StateProblem`` named
  there; the errors carry no caller content;
* takes values that are already validated (a ``SharedMemory`` or a
  ``SharedMemoryChanges`` cannot be built invalid), so it does not re-validate
  strings, lengths or ids. It does check *types* where its docstring says so.

The functions below are implemented in stages (see the implementation brief):
``next_candidate_state``, then ``check_deletable`` / ``check_restorable``, then
``apply_changes`` / ``changed_fields``, then ``plan_edit``, then
``draft_from_candidate``.
"""

from paw_backend.memory.shared.errors import (  # noqa: F401
    SharedMemoryStateError,
    SharedMemoryVersionConflictError,
    StateProblem,
)
from paw_backend.memory.shared.records import (  # noqa: F401
    EDITABLE_FIELDS,
    CandidateAction,
    CandidateState,
    EditPlan,
    SharedMemory,
    SharedMemoryCandidate,
    SharedMemoryChanges,
    SharedMemoryDraft,
    SharedMemoryStatus,
)


def next_candidate_state(
    current: CandidateState, action: CandidateAction
) -> CandidateState:
    """The state a candidate is in after ``action``; the candidate state machine.

    Only a ``PENDING`` candidate can be decided::

        PENDING + APPROVE -> APPROVED
        PENDING + REJECT  -> REJECTED

    A candidate that is already ``APPROVED`` or ``REJECTED`` is final: every
    action on it raises ``SharedMemoryStateError(StateProblem.CANDIDATE_NOT_PENDING)``
    (approving an approved candidate, rejecting a rejected one, and the two
    crossed ones alike).

    Both arguments must be members of their enum. Anything else (``None``, the
    plain strings ``"pending"`` / ``"approve"``, an ``int``) raises ``TypeError``
    before anything else is looked at; a ``str`` that equals a member's value is
    still not a member.
    """
    # Check types first
    if not isinstance(current, CandidateState):
        raise TypeError()
    if not isinstance(action, CandidateAction):
        raise TypeError()

    # Only PENDING candidates can be decided
    if current == CandidateState.PENDING:
        if action == CandidateAction.APPROVE:
            return CandidateState.APPROVED
        elif action == CandidateAction.REJECT:
            return CandidateState.REJECTED
    else:
        # Any action on APPROVED/REJECTED raises CANDIDATE_NOT_PENDING
        raise SharedMemoryStateError(StateProblem.CANDIDATE_NOT_PENDING)


def check_deletable(current: SharedMemory) -> None:
    """Return ``None`` if ``current`` may be deleted, else raise.

    Only an ``ACTIVE`` shared memory can be deleted. A ``DELETED`` one raises
    ``SharedMemoryStateError(StateProblem.ALREADY_DELETED)``. Returns ``None``
    (not a boolean) when the delete is allowed.
    """
    if current.status == SharedMemoryStatus.DELETED:
        raise SharedMemoryStateError(StateProblem.ALREADY_DELETED)
    # If we get here, the status is ACTIVE, so deletion is allowed
    return None


def check_restorable(current: SharedMemory) -> None:
    """Return ``None`` if ``current`` may be restored, else raise.

    Only a ``DELETED`` shared memory can be restored. An ``ACTIVE`` one raises
    ``SharedMemoryStateError(StateProblem.NOT_DELETED)``. Returns ``None`` when
    the restore is allowed.
    """
    if current.status == SharedMemoryStatus.ACTIVE:
        raise SharedMemoryStateError(StateProblem.NOT_DELETED)
    # If we get here, the status is DELETED, so restoration is allowed
    return None


def apply_changes(
    current: SharedMemory, changes: SharedMemoryChanges
) -> SharedMemoryDraft:
    """The field set the next version would have if ``changes`` were applied.

    Each of ``title``, ``content``, ``memory_type``, ``importance`` and
    ``policy_subjects`` that ``changes`` sets (is not ``None``) replaces the
    value of ``current``; every other field keeps the value of ``current``.
    ``policy_subjects=()`` is a value: it clears the subjects. The draft's
    ``reason`` is ``changes.reason`` (``current`` has none). ``current`` and
    ``changes`` are not modified.

    Example: ``current`` has ``title="A"``, ``content="c"``,
    ``memory_type="rule"``, ``importance=50``, ``policy_subjects=("x",)``.
    ``SharedMemoryChanges(title="B", importance=70)`` gives a draft with
    ``title="B"``, ``content="c"``, ``memory_type="rule"``, ``importance=70``,
    ``policy_subjects=("x",)``, ``reason=None``.
    ``SharedMemoryChanges(policy_subjects=())`` gives ``policy_subjects=()``
    and every other field as in ``current``.
    """
    # Build the result using the changes where provided, otherwise keep current values
    title = changes.title if changes.title is not None else current.title
    content = changes.content if changes.content is not None else current.content
    memory_type = (
        changes.memory_type if changes.memory_type is not None else current.memory_type
    )
    importance = (
        changes.importance if changes.importance is not None else current.importance
    )
    policy_subjects = (
        changes.policy_subjects
        if changes.policy_subjects is not None
        else current.policy_subjects
    )
    reason = changes.reason

    return SharedMemoryDraft(
        memory_type=memory_type,
        title=title,
        content=content,
        importance=importance,
        policy_subjects=policy_subjects,
        reason=reason,
    )


def changed_fields(current: SharedMemory, draft: SharedMemoryDraft) -> tuple[str, ...]:
    """The names of the fields in which ``draft`` differs from ``current``.

    Compared: ``content``, ``importance``, ``memory_type``, ``policy_subjects``
    and ``title`` (``EDITABLE_FIELDS``). The result is sorted alphabetically,
    holds each name once and is ``()`` when nothing differs. ``draft.reason`` is
    not a field of the memory and is never reported.

    Example: ``current.title="A"`` and ``draft.title="B"``, all else equal, gives
    ``("title",)``; a different content, importance and title give
    ``("content", "importance", "title")``.
    """
    changed = []

    # Check each editable field
    if current.content != draft.content:
        changed.append("content")
    if current.importance != draft.importance:
        changed.append("importance")
    if current.memory_type != draft.memory_type:
        changed.append("memory_type")
    if current.policy_subjects != draft.policy_subjects:
        changed.append("policy_subjects")
    if current.title != draft.title:
        changed.append("title")

    # Return sorted tuple
    return tuple(sorted(changed))


def plan_edit(
    current: SharedMemory, expected_version: int, changes: SharedMemoryChanges
) -> EditPlan | None:
    """Decide what an edit of ``current`` does. Checks run in this order:

    1. ``expected_version != current.version_number``: raise
       ``SharedMemoryVersionConflictError(expected_version, current.version_number)``
       (the caller edited an old version; nothing is overwritten).
    2. ``current.status`` is ``DELETED``: raise
       ``SharedMemoryStateError(StateProblem.DELETED)`` (restore it first).
    3. Build ``draft = apply_changes(current, changes)`` and
       ``names = changed_fields(current, draft)``. If ``names`` is empty the edit
       changes nothing: return ``None`` (no new version is written).
    4. Otherwise return ``EditPlan(draft=draft, changed_fields=names)``.

    The version conflict is reported before the state problem, and both before
    "nothing changed": an edit of an old version is refused even if it changes
    nothing.
    """
    # Check version mismatch first
    if expected_version != current.version_number:
        raise SharedMemoryVersionConflictError(expected_version, current.version_number)

    # Check if deleted
    if current.status == SharedMemoryStatus.DELETED:
        raise SharedMemoryStateError(StateProblem.DELETED)

    # Apply changes and check what fields changed
    draft = apply_changes(current, changes)
    changed = changed_fields(current, draft)

    # Return None if nothing changed
    if len(changed) == 0:
        return None

    # Return the edit plan
    return EditPlan(draft=draft, changed_fields=changed)


def draft_from_candidate(candidate: SharedMemoryCandidate) -> SharedMemoryDraft:
    """The shared memory an approved candidate becomes.

    A ``SharedMemoryDraft`` with the candidate's ``memory_type``, ``title``,
    ``content``, ``importance`` and ``policy_subjects``, and ``reason`` equal to
    the candidate's ``reason`` (``None`` stays ``None``). Nothing else is copied
    (the proposer and the origin are recorded elsewhere). The candidate's state
    is not looked at: whether it may be approved is
    ``next_candidate_state``'s decision.
    """
    return SharedMemoryDraft(
        memory_type=candidate.memory_type,
        title=candidate.title,
        content=candidate.content,
        importance=candidate.importance,
        policy_subjects=candidate.policy_subjects,
        reason=candidate.reason,
    )
