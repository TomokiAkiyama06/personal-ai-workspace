"""Precedence of the System Security Policy over Shared Memory: pure rules.

Shared Memory is knowledge a model may consult; the System Security Policy is
the mandatory rule set the backend enforces (REQUIREMENTS.md "Shared Memory vs
System Policy"). Shared Memory can never override it, so when the two conflict
the policy wins in the effective view.

**The conflict rule.** The backend cannot judge whether two texts contradict
each other, and this module invents no policy content. A conflict is therefore
*declared*: a shared memory lists the ``policy_subjects`` it is about (an Owner
or Admin sets them when creating, editing or approving it), and a policy item
governs a ``subject``. A policy item **covers** a memory subject when the two are
equal or the memory subject lies below the policy subject in the dotted
hierarchy. A memory is **overridden** when any of its subjects is covered by any
policy item. A memory that declares no subject is never overridden by this rule
(see the README for what that means in practice).

These are pure functions: no database, no clock, no globals, arguments are not
modified. ``subject_covers`` and ``overriding_policy_ids`` validate subjects with
``validation.validate_subject``; everything else is already validated.
"""

from collections.abc import Sequence

from paw_backend.memory.shared.records import (  # noqa: F401
    EffectiveSharedMemory,
    OverriddenMemory,
    SharedMemory,
    SharedMemoryStatus,
    SystemPolicyItem,
)
from paw_backend.memory.shared.validation import validate_subject  # noqa: F401


def subject_covers(policy_subject: str, memory_subject: str) -> bool:
    """Whether the policy subject covers the memory subject.

    True when the two are equal, or when ``memory_subject`` starts with
    ``policy_subject`` followed by ``"."`` (a descendant). Nothing else: a longer
    name that merely starts with the same letters does not count, and a policy
    below the memory subject does not cover it.

    ``subject_covers("merge", "merge")`` is True.
    ``subject_covers("merge", "merge.permission")`` is True.
    ``subject_covers("merge", "merge.permission.admin")`` is True.
    ``subject_covers("merge.permission", "merge")`` is False.
    ``subject_covers("merge", "mergeable")`` is False.
    ``subject_covers("merge", "merge_x")`` is False.
    ``subject_covers("a.b", "a.bc")`` is False.

    Both arguments are checked with ``validate_subject`` first (the policy
    subject as field ``"policy_subject"``, the memory subject as
    ``"memory_subject"``); an invalid one raises ``InvalidSharedMemoryInputError``
    (the policy subject is checked before the memory subject).
    """
    raise NotImplementedError("PAW-046 stub")


def overriding_policy_ids(
    memory_subjects: Sequence[str], policies: Sequence[SystemPolicyItem]
) -> tuple[str, ...]:
    """The ``policy_id`` of every policy that covers any of ``memory_subjects``.

    A policy is included when ``subject_covers(policy.subject, s)`` holds for at
    least one ``s`` in ``memory_subjects``. The result is sorted alphabetically
    and holds each id once, however many subjects a policy covers. It is ``()``
    when ``memory_subjects`` is empty or nothing covers.

    ``memory_subjects=("merge.permission", "docs")`` with policies
    ``p2: merge``, ``p1: merge.permission``, ``p3: deploy`` gives
    ``("p1", "p2")``.
    """
    raise NotImplementedError("PAW-046 stub")


def resolve_effective_view(
    memories: Sequence[SharedMemory], policies: Sequence[SystemPolicyItem]
) -> EffectiveSharedMemory:
    """Apply the precedence rule to ``memories``: the policy wins.

    * A memory whose ``status`` is ``DELETED`` is ignored: it appears nowhere in
      the result.
    * For each remaining memory, ``ids = overriding_policy_ids(memory.policy_subjects,
      policies)``. If ``ids`` is empty the memory goes into ``memories``;
      otherwise it goes into ``overridden`` as
      ``OverriddenMemory(memory.memory_id, memory.version_id, ids)`` and its
      content is not part of the result.
    * ``memories`` and ``overridden`` keep the order of the input.
    * ``applied_policies`` holds the policy items (the objects of ``policies``)
      whose id occurs in any ``OverriddenMemory.policy_ids``, once each, sorted
      by ``policy_id``. A policy that overrides nothing is not listed.

    The result holds tuples, never lists. ``memories`` and ``policies`` are not
    modified. Example: memories ``m1`` (subjects ``("merge.permission",)``),
    ``m2`` (subjects ``()``), ``m3`` (``("docs",)``) and policies ``p1: merge``,
    ``p2: deploy`` give ``memories=(m2, m3)``,
    ``overridden=(OverriddenMemory(m1.memory_id, m1.version_id, ("p1",)),)`` and
    ``applied_policies=(p1,)``.
    """
    raise NotImplementedError("PAW-046 stub")
