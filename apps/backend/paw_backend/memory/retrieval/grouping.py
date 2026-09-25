"""Deduplicate, handle conflicts and cut to Top-N: pure rules, no I/O.

Runs after ranking, on candidates that are all readable by the caller (the
database filter removed everything else before a candidate existed). Nothing here
looks at a memory the caller cannot read, so nothing here can leak one: the
relations it is given were read with the ACL condition on **both** ends.

* **Dedup** (:func:`deduplicate`): a memory that is the same as one with a better
  claim is merged into it and listed in its ``duplicates``. Same means the
  normalised text is equal, or the words and Japanese character pairs overlap by
  at least ``near_duplicate_similarity`` (Jaccard; a text of fewer than four
  features must be equal, so "OK" and "OK." are the same but two short different
  notes are not; equal means the same words in the same order, whatever the
  punctuation and case). The better
  claim is the general precedence: confirmed before inferred, fresh before stale,
  the more specific scope, then the score. Two memories tied by a
  ``conflicts_with`` relation are never merged: they contradict, they are not
  copies. The comparison is greedy in precedence order, not transitive.
* **Conflicts** (:func:`conflict_groups`): connected components of two or more
  memories over ``conflicts_with`` relations. Every member stays a result; the
  group says "these disagree" and picks nothing.
* **Top-N** (:func:`select`): groups are returned whole. A group counts as many
  memories as it has members and ranks with its best member; one that does not
  fit in what is left is skipped (and counted), not cut in half.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from paw_backend.memory.fulltext import features
from paw_backend.memory.retrieval.candidates import Ranked
from paw_backend.memory.retrieval.ranking import (
    CONFIRMATION_RANK,
    SCOPE_SPECIFICITY,
)
from paw_backend.memory.retrieval.records import ConflictGroup, Freshness

# Texts with fewer distinct features than this must be equal to count as copies.
_MIN_FEATURES_FOR_OVERLAP = 4


def precedence_key(item: Ranked) -> tuple:
    """Sort key (ascending = better claim first) of the general precedence."""
    candidate = item.candidate
    return (
        -CONFIRMATION_RANK[candidate.confirmation_state],
        0 if item.freshness is Freshness.FRESH else 1,
        -SCOPE_SPECIFICITY[candidate.scope],
        -item.score,
        str(candidate.version_id),
    )


def rank_key(item: Ranked) -> tuple:
    """Sort key (ascending = first) of the final order: score, then precedence."""
    return (-item.score, *precedence_key(item))


@dataclass(frozen=True, slots=True)
class _Text:
    exact: str
    parts: frozenset[str]


def _text_of(item: Ranked) -> _Text:
    joined = f"{item.candidate.title} {item.candidate.content}"
    found = features(joined)
    return _Text(exact=" ".join(found), parts=frozenset(found))


def near_duplicate(a: _Text, b: _Text, similarity: float) -> bool:
    """Whether two texts are copies of each other (see the module docstring)."""
    if a.exact == b.exact:
        return True
    if min(len(a.parts), len(b.parts)) < _MIN_FEATURES_FOR_OVERLAP:
        return False
    return len(a.parts & b.parts) / len(a.parts | b.parts) >= similarity


def deduplicate(
    items: Sequence[Ranked],
    conflicts: Iterable[tuple[UUID, UUID]],
    similarity: float,
) -> tuple[list[Ranked], dict[UUID, UUID]]:
    """The survivors, and ``{merged version id: survivor version id}``.

    Two versions of one memory never both appear (only one version of a memory is
    active), but a repeated candidate is merged by memory id as well, keeping the
    better claim.
    """
    conflicting = {frozenset(pair) for pair in conflicts}
    survivors: list[Ranked] = []
    texts: dict[UUID, _Text] = {}
    merged_into: dict[UUID, UUID] = {}
    merged: dict[UUID, list[UUID]] = {}
    for item in sorted(items, key=precedence_key):
        version_id = item.candidate.version_id
        target = None
        for survivor in survivors:
            survivor_id = survivor.candidate.version_id
            if frozenset((version_id, survivor_id)) in conflicting:
                continue
            if survivor.candidate.memory_id == item.candidate.memory_id:
                target = survivor_id
                break
            texts.setdefault(survivor_id, _text_of(survivor))
            texts.setdefault(version_id, _text_of(item))
            if near_duplicate(texts[survivor_id], texts[version_id], similarity):
                target = survivor_id
                break
        if target is None:
            survivors.append(item)
        else:
            merged_into[version_id] = target
            merged.setdefault(target, []).append(version_id)
    result = [
        replace(
            item, duplicates=tuple(sorted(merged.get(item.candidate.version_id, ())))
        )
        for item in survivors
    ]
    return result, merged_into


def conflict_groups(
    survivors: Sequence[Ranked],
    conflicts: Iterable[tuple[UUID, UUID]],
    merged_into: dict[UUID, UUID],
) -> list[tuple[Ranked, ...]]:
    """The groups of two or more survivors tied by conflict relations.

    A relation whose end was merged into a survivor is an edge of that survivor.
    Members are ordered by ``precedence_key``; the groups by their best member
    (``rank_key``), so the numbering is deterministic.
    """
    by_id = {item.candidate.version_id: item for item in survivors}
    parent = {version_id: version_id for version_id in by_id}

    def find(version_id: UUID) -> UUID:
        while parent[version_id] != version_id:
            parent[version_id] = parent[parent[version_id]]
            version_id = parent[version_id]
        return version_id

    for first, second in conflicts:
        a = merged_into.get(first, first)
        b = merged_into.get(second, second)
        if a in by_id and b in by_id and a != b:
            parent[find(a)] = find(b)
    members: dict[UUID, list[Ranked]] = {}
    for version_id, item in by_id.items():
        members.setdefault(find(version_id), []).append(item)
    groups = [
        tuple(sorted(group, key=precedence_key))
        for group in members.values()
        if len(group) >= 2
    ]
    groups.sort(key=lambda group: min(rank_key(item) for item in group))
    return groups


@dataclass(frozen=True, slots=True)
class Selection:
    """The final list, its conflict groups and how many groups did not fit."""

    hits: tuple[tuple[Ranked, int | None], ...]
    groups: tuple[ConflictGroup, ...]
    dropped_groups: int


def select(
    survivors: Sequence[Ranked],
    groups: Sequence[tuple[Ranked, ...]],
    limit: int,
) -> Selection:
    """Top ``limit`` memories, groups whole (see the module docstring)."""
    grouped = {
        item.candidate.version_id: number
        for number, group in enumerate(groups)
        for item in group
    }
    units: list[tuple[tuple, tuple[Ranked, ...], int | None]] = []
    for number, group in enumerate(groups):
        units.append((min(rank_key(item) for item in group), group, number))
    for item in survivors:
        if item.candidate.version_id not in grouped:
            units.append((rank_key(item), (item,), None))
    units.sort(key=lambda unit: unit[0])

    chosen: list[tuple[tuple[Ranked, ...], int | None]] = []
    used = 0
    dropped = 0
    for _, members, number in units:
        if used + len(members) <= limit:
            chosen.append((members, number))
            used += len(members)
        elif number is not None:
            dropped += 1
    hits: list[tuple[Ranked, int | None]] = []
    result_groups: list[ConflictGroup] = []
    for members, number in chosen:
        group_id = None
        if number is not None:
            group_id = len(result_groups)
            result_groups.append(
                ConflictGroup(
                    group_id, tuple(item.candidate.version_id for item in members)
                )
            )
        hits.extend((item, group_id) for item in members)
    return Selection(tuple(hits), tuple(result_groups), dropped)
