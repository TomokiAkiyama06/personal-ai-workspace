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
    raise NotImplementedError("PAW-046 stub")


def check_deletable(current: SharedMemory) -> None:
    """Return ``None`` if ``current`` may be deleted, else raise.

    Only an ``ACTIVE`` shared memory can be deleted. A ``DELETED`` one raises
    ``SharedMemoryStateError(StateProblem.ALREADY_DELETED)``. Returns ``None``
    (not a boolean) when the delete is allowed.
    """
    raise NotImplementedError("PAW-046 stub")


def check_restorable(current: SharedMemory) -> None:
    """Return ``None`` if ``current`` may be restored, else raise.

    Only a ``DELETED`` shared memory can be restored. An ``ACTIVE`` one raises
    ``SharedMemoryStateError(StateProblem.NOT_DELETED)``. Returns ``None`` when
    the restore is allowed.
    """
    raise NotImplementedError("PAW-046 stub")


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
    raise NotImplementedError("PAW-046 stub")


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
    raise NotImplementedError("PAW-046 stub")


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
    raise NotImplementedError("PAW-046 stub")


def draft_from_candidate(candidate: SharedMemoryCandidate) -> SharedMemoryDraft:
    """The shared memory an approved candidate becomes.

    A ``SharedMemoryDraft`` with the candidate's ``memory_type``, ``title``,
    ``content``, ``importance`` and ``policy_subjects``, and ``reason`` equal to
    the candidate's ``reason`` (``None`` stays ``None``). Nothing else is copied
    (the proposer and the origin are recorded elsewhere). The candidate's state
    is not looked at: whether it may be approved is
    ``next_candidate_state``'s decision.
    """
    raise NotImplementedError("PAW-046 stub")
