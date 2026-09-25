"""Turn a validated worker output into candidate memory versions (PAW-041).

This module is the write side of the consolidator. It runs INSIDE the caller's
transaction (the one that holds the job's lease lock, see ``queue.lock_held``), so
a candidate, its relations, its source, the order guard and the completion of the
job commit together or not at all.

Only ``rules.plan_item`` decides what happens to a memory; this module reads the
current state, asks it, and performs exactly what the answer says, through the
tables of the Memory schema (PAW-040) and nothing else:

* The owner is ``memory_journal_entries.owner_user_id`` (copied from the
  conversation under a row lock). Nothing the worker returned names an owner, and
  every candidate is written to the ``user`` scope of that owner, private to them.
  ``project`` / ``repo`` are kept only as ``attributes.recommended_scope``.
* A memory is found through ``memory_consolidation_keys`` (owner, key). The keys a
  transaction touches are locked with transaction-level advisory locks, taken in
  the order of their hash, so two consolidations of one key are serialised and two
  that need the same keys cannot deadlock. The memory rows themselves are guarded
  by the schema: ``(memory_id, version_number)`` is unique, one version per memory
  is ``active``, and superseding is ``UPDATE ... WHERE status = 'active'`` whose
  row count must be 1. A writer outside this module (a manual edit that does not
  take these locks) therefore makes the write fail (``ApplyConflict``, the
  transaction rolls back, the job is retried against the new state) instead of
  being overwritten.
* A new version supersedes the previous ``active`` one of the same key (status
  ``superseded``, a ``supersedes`` relation from the new to the old); the memory
  the item's ``supersedes`` key names is superseded the same way; ``conflicts_with``
  keys get a ``conflicts_with`` relation and are left alone. The source of a
  version is its conversation and message (``memory_sources``), so deleting the
  conversation finds and reports it.
* ``confirmation_state`` is ``rules.stored_state``: never ``confirmed``.

The outcome (:func:`build_outcome`) is what the entry stores about the run: a code
per item, the ids of what was written, and for HELD items the candidate itself, so
that the confirmation flow (PAW-044) can show it to the user. It is derived from
the raw conversation and stays in the owner's row (deleted with the conversation);
it is never logged.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import sqltypes

from paw_backend.memory.journal.domain import EntryState, ItemResult
from paw_backend.memory.journal.models import ConsolidationKey, JournalEntry
from paw_backend.memory.journal.rules import (
    CurrentMemory,
    OrderKey,
    plan_item,
    stored_state,
)
from paw_backend.memory.journal.worker import CONTRACT, WorkerMemory
from paw_backend.memory.models import (
    ActorType,
    ConfirmationState,
    FreshnessPolicy,
    Memory,
    MemoryRelation,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    MemoryVersion,
    RelationType,
    SourceType,
)

_ENTRY = JournalEntry.__table__
_KEYS = ConsolidationKey.__table__
_MEMORY = Memory.__table__
_VERSION = MemoryVersion.__table__
_RELATION = MemoryRelation.__table__
_SOURCE = MemorySource.__table__

MEMORY_TYPE = "worker_candidate"
CHANGE_REASON = "background consolidation"
_LOCK_PREFIX = "paw.memory_journal.key:"

_LOCK_ORDER = text(
    "SELECT DISTINCT hashtextextended(:prefix || CAST(:owner AS text) || ':' || k, 0)"
    " AS lock_id FROM unnest(:keys) AS k ORDER BY lock_id"
).bindparams(bindparam("keys", type_=ARRAY(sqltypes.Text())))
_LOCK = text("SELECT pg_advisory_xact_lock(:lock_id)")

HELD_RESULTS = frozenset({ItemResult.HELD_CONFIRMED, ItemResult.HELD_HIGH_RISK})


class ApplyConflict(Exception):  # noqa: N818 - an internal signal, not a public error
    """A write found the memory changed under it (a lost update was prevented).

    Never shown to anyone: the transaction rolls back and the job is retried.
    """


@dataclass(frozen=True, slots=True)
class EntryContext:
    """The locked journal entry a worker output is applied for."""

    entry_id: UUID
    conversation_id: UUID
    message_id: UUID
    event_sequence: int
    recorded_at: datetime
    owner_user_id: UUID
    state: EntryState

    @property
    def order_key(self) -> OrderKey:
        return OrderKey(self.conversation_id, self.event_sequence, self.recorded_at)


@dataclass(frozen=True, slots=True)
class _Version:
    id: UUID
    number: int
    summary: CurrentMemory


@dataclass(frozen=True, slots=True)
class _Registered:
    memory_id: UUID
    applied: OrderKey


@dataclass(slots=True)
class ItemOutcome:
    index: int
    item: WorkerMemory
    result: ItemResult
    memory_id: UUID | None = None
    version_number: int | None = None


async def lock_entry(session: AsyncSession, entry_id: UUID) -> EntryContext | None:
    """The journal entry, locked ``FOR UPDATE``; ``None`` if it no longer exists."""
    row = (
        await session.execute(
            select(
                _ENTRY.c.conversation_id,
                _ENTRY.c.message_id,
                _ENTRY.c.event_sequence,
                _ENTRY.c.recorded_at,
                _ENTRY.c.owner_user_id,
                _ENTRY.c.state,
            )
            .where(_ENTRY.c.id == entry_id)
            .with_for_update()
        )
    ).first()
    if row is None:
        return None
    return EntryContext(
        entry_id=entry_id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        event_sequence=row.event_sequence,
        recorded_at=row.recorded_at,
        owner_user_id=row.owner_user_id,
        state=EntryState(row.state),
    )


async def mark_consolidated(
    session: AsyncSession, entry: EntryContext, outcome: dict[str, Any]
) -> None:
    """Pending -> consolidated with the outcome.

    The caller holds the entry's row lock and has read its state as ``pending``, so
    the update always finds it (and a state that is not ``pending`` is refused by
    the caller before it writes anything).
    """
    await session.execute(
        update(_ENTRY)
        .where(_ENTRY.c.id == entry.entry_id)
        .values(
            state=EntryState.CONSOLIDATED.value,
            consolidated_at=func.clock_timestamp(),
            outcome=outcome,
        )
    )


async def _lock_keys(session: AsyncSession, owner: UUID, keys: list[str]) -> None:
    """Advisory-lock (owner, key) for every key, in the order of the lock ids."""
    if not keys:
        return
    lock_ids = (
        await session.execute(
            _LOCK_ORDER, {"prefix": _LOCK_PREFIX, "owner": owner, "keys": keys}
        )
    ).scalars()
    for lock_id in list(lock_ids):
        await session.execute(_LOCK, {"lock_id": lock_id})


async def _load_registry(
    session: AsyncSession, owner: UUID, keys: list[str]
) -> dict[str, _Registered]:
    if not keys:
        return {}
    rows = await session.execute(
        select(
            _KEYS.c.key,
            _KEYS.c.memory_id,
            _KEYS.c.applied_conversation_id,
            _KEYS.c.applied_event_sequence,
            _KEYS.c.applied_recorded_at,
        ).where(_KEYS.c.owner_user_id == owner, _KEYS.c.key.in_(keys))
    )
    return {
        row.key: _Registered(
            row.memory_id,
            OrderKey(
                row.applied_conversation_id,
                row.applied_event_sequence,
                row.applied_recorded_at,
            ),
        )
        for row in rows
    }


async def _load_latest(
    session: AsyncSession, owner: UUID, memory_ids: set[UUID]
) -> dict[UUID, _Version]:
    """The latest version of each memory, if it is one of this owner's private ones."""
    if not memory_ids:
        return {}
    rows = await session.execute(
        select(
            _VERSION.c.id,
            _VERSION.c.memory_id,
            _VERSION.c.version_number,
            _VERSION.c.status,
            _VERSION.c.confirmation_state,
            _VERSION.c.content,
        )
        .where(
            _VERSION.c.memory_id.in_(memory_ids),
            # The scope and owner rule of the Memory schema (memory.acl): only the
            # owner's own private versions are read or replaced here.
            _VERSION.c.scope == MemoryScope.USER.value,
            _VERSION.c.owner_user_id == owner,
        )
        .distinct(_VERSION.c.memory_id)
        .order_by(_VERSION.c.memory_id, _VERSION.c.version_number.desc())
    )
    return {
        row.memory_id: _Version(
            row.id,
            row.version_number,
            CurrentMemory(
                MemoryStatus(row.status),
                ConfirmationState(row.confirmation_state),
                row.content,
            ),
        )
        for row in rows
    }


async def _supersede(session: AsyncSession, version_id: UUID) -> None:
    """``active`` -> ``superseded`` of ONE version; anything else is a lost update."""
    result = await session.execute(
        update(_VERSION)
        .where(
            _VERSION.c.id == version_id, _VERSION.c.status == MemoryStatus.ACTIVE.value
        )
        .values(status=MemoryStatus.SUPERSEDED.value)
    )
    if result.rowcount != 1:
        raise ApplyConflict


async def _relate(
    session: AsyncSession,
    newer: UUID,
    older: UUID,
    kind: RelationType,
    reason: str,
) -> None:
    await session.execute(
        pg_insert(_RELATION)
        .values(
            from_version_id=newer,
            to_version_id=older,
            relation_type=kind.value,
            reason=reason,
        )
        .on_conflict_do_nothing()
    )


def _attributes(
    item: WorkerMemory, entry: EntryContext, base_version: int
) -> dict[str, Any]:
    return {
        "key": item.key,
        # What the worker recommended and claimed. Kept for the confirmation flow;
        # the backend's own decisions (private scope, never confirmed) are the columns.
        "recommended_scope": item.scope.value,
        "worker_state": item.state.value,
        "worker_supersedes": item.supersedes,
        "contract": CONTRACT,
        "journal": {
            "entry_id": str(entry.entry_id),
            "conversation_id": str(entry.conversation_id),
            "event_sequence": entry.event_sequence,
            # The version this candidate was based on (0: the key was new).
            "base_memory_version": base_version,
        },
    }


async def apply_items(
    session: AsyncSession, entry: EntryContext, items: tuple[WorkerMemory, ...]
) -> list[ItemOutcome]:
    """Apply a worker's memories for ``entry``; one :class:`ItemOutcome` per item.

    Runs in the caller's transaction; an :class:`ApplyConflict` or a database
    error leaves nothing behind once the caller rolls back.
    """
    owner = entry.owner_user_id
    order = entry.order_key
    involved = sorted(
        {
            key
            for item in items
            for key in (item.key, item.supersedes, *item.conflicts_with)
            if key is not None
        }
    )
    await _lock_keys(session, owner, involved)
    registry = await _load_registry(session, owner, involved)
    latest = await _load_latest(
        session, owner, {registered.memory_id for registered in registry.values()}
    )

    outcomes: list[ItemOutcome] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if item.key in seen:
            outcomes.append(ItemOutcome(index, item, ItemResult.DUPLICATE_KEY))
            continue
        seen.add(item.key)
        registered = registry.get(item.key)
        current = latest.get(registered.memory_id) if registered else None
        target_registered = (
            registry.get(item.supersedes)
            if item.supersedes is not None and item.supersedes != item.key
            else None
        )
        target = latest.get(target_registered.memory_id) if target_registered else None

        result = plan_item(
            item,
            entry=order,
            current=current.summary if current else None,
            applied=registered.applied if registered else None,
            supersedes_target=target.summary if target else None,
        )
        outcome = ItemOutcome(index, item, result)
        outcomes.append(outcome)

        if result is ItemResult.DUPLICATE:
            assert registered is not None  # a duplicate needs a current version
            await _advance(session, owner, item.key, order)
            registry[item.key] = _Registered(registered.memory_id, order)
        elif result.wrote_memory:
            await _write_version(
                session,
                entry,
                item,
                outcome,
                registered,
                current,
                (target_registered.memory_id, target)
                if target_registered and target
                else None,
                registry,
                latest,
            )
    return outcomes


async def _advance(
    session: AsyncSession, owner: UUID, key: str, order: OrderKey
) -> None:
    """Move the order guard of a key forward to ``order`` (a newer entry)."""
    await session.execute(
        update(_KEYS)
        .where(_KEYS.c.owner_user_id == owner, _KEYS.c.key == key)
        .values(
            applied_conversation_id=order.conversation_id,
            applied_event_sequence=order.event_sequence,
            applied_recorded_at=order.recorded_at,
        )
    )


async def _write_version(
    session: AsyncSession,
    entry: EntryContext,
    item: WorkerMemory,
    outcome: ItemOutcome,
    registered: _Registered | None,
    current: _Version | None,
    target: tuple[UUID, _Version] | None,
    registry: dict[str, _Registered],
    latest: dict[UUID, _Version],
) -> None:
    """Write the candidate as the next version of its key's memory (new if needed)."""
    owner = entry.owner_user_id
    order = entry.order_key
    assert item.content is not None  # plan_item returns NO_CONTENT otherwise

    previous = (
        current if current and current.summary.status is MemoryStatus.ACTIVE else None
    )
    target_memory_id, target_version = target if target else (None, None)
    replaced = (
        target_version
        if target_version and target_version.summary.status is MemoryStatus.ACTIVE
        else None
    )
    # Retire the versions this one replaces BEFORE it is inserted: a memory has at
    # most one active version (a unique index that is checked at once).
    for old in (previous, replaced):
        if old is not None:
            await _supersede(session, old.id)

    if registered is None:
        memory_id = (
            await session.execute(insert(_MEMORY).returning(_MEMORY.c.id))
        ).scalar_one()
    else:
        memory_id = registered.memory_id
    number = current.number + 1 if current else 1

    version_id = (
        await session.execute(
            insert(_VERSION)
            .values(
                memory_id=memory_id,
                version_number=number,
                scope=MemoryScope.USER.value,
                owner_user_id=owner,
                memory_type=MEMORY_TYPE,
                title=item.key,
                content=item.content,
                status=MemoryStatus.ACTIVE.value,
                confirmation_state=stored_state(item.state).value,
                freshness_policy=FreshnessPolicy.PERMANENT.value,
                actor_type=ActorType.SYSTEM.value,
                change_reason=CHANGE_REASON,
                attributes=_attributes(item, entry, number - 1),
            )
            .returning(_VERSION.c.id)
        )
    ).scalar_one()

    if previous is not None:
        await _relate(
            session, version_id, previous.id, RelationType.SUPERSEDES, "same key"
        )
    if replaced is not None:
        await _relate(
            session, version_id, replaced.id, RelationType.SUPERSEDES, "supersedes key"
        )
    retired = {old.id for old in (previous, replaced) if old is not None}
    for other in item.conflicts_with:
        conflicting = registry.get(other)
        version = latest.get(conflicting.memory_id) if conflicting else None
        if (
            other != item.key
            and version is not None
            and version.summary.status is MemoryStatus.ACTIVE
            and version.id not in retired
        ):
            await _relate(
                session, version_id, version.id, RelationType.CONFLICTS_WITH, "worker"
            )
    await session.execute(
        insert(_SOURCE).values(
            memory_version_id=version_id,
            source_type=SourceType.CONVERSATION.value,
            conversation_id=entry.conversation_id,
            message_id=entry.message_id,
        )
    )

    if registered is None:
        await session.execute(
            insert(_KEYS).values(
                owner_user_id=owner,
                key=item.key,
                memory_id=memory_id,
                applied_conversation_id=order.conversation_id,
                applied_event_sequence=order.event_sequence,
                applied_recorded_at=order.recorded_at,
            )
        )
    else:
        await _advance(session, owner, item.key, order)
    # What later items of this output see.
    registry[item.key] = _Registered(memory_id, order)
    latest[memory_id] = _Version(
        version_id,
        number,
        CurrentMemory(MemoryStatus.ACTIVE, stored_state(item.state), item.content),
    )
    if replaced is not None and target_memory_id is not None:
        latest[target_memory_id] = _Version(
            replaced.id,
            replaced.number,
            CurrentMemory(
                MemoryStatus.SUPERSEDED,
                replaced.summary.confirmation_state,
                replaced.summary.content,
            ),
        )
    outcome.memory_id = memory_id
    outcome.version_number = number


def build_outcome(outcomes: list[ItemOutcome]) -> dict[str, Any]:
    """The JSON the entry stores. Held items carry the candidate for the user."""
    items: list[dict[str, Any]] = []
    for outcome in outcomes:
        item = outcome.item
        record: dict[str, Any] = {
            "index": outcome.index,
            "result": outcome.result.value,
            "key": item.key,
        }
        if outcome.memory_id is not None:
            record["memory_id"] = str(outcome.memory_id)
            record["version_number"] = outcome.version_number
        if outcome.result in HELD_RESULTS:
            record["candidate"] = {
                "scope": item.scope.value,
                "state": item.state.value,
                "content": item.content,
                "supersedes": item.supersedes,
                "conflicts_with": list(item.conflicts_with),
            }
        items.append(record)
    return {"contract": CONTRACT, "items": items}
