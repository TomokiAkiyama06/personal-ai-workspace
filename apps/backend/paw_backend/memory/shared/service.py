"""The Shared Memory service (PAW-046): read, manage, candidates, effective view.

Shared Memory is Workspace-wide knowledge a model may consult. It lives in the
PAW-040 tables (``memories`` and ``memory_versions`` with ``scope = 'shared'``);
only the candidates have a table of their own (``shared_memory_candidates``).
There is no HTTP surface yet: the API layer of a later issue calls this service
with the authenticated user.

Who may do what (REQUIREMENTS.md "Shared Memory permissions")
-------------------------------------------------------------
Every method takes the acting ``actor`` first: a ``paw_backend.authz.Principal``
(a human user, already known to be active) or an :class:`AgentActor` (an agent
acting for a user). The service asks the ``Authorizer`` once per call, so the
decision is audited by the Authorizer (audit mode of the capability: reads
record denials only, the others every decision, fail-closed).

* ``list_memories``, ``get_memory``, ``effective_view``: capability
  ``shared_memory.read``. Every active user; an agent whose grant lists it and
  covers all projects.
* ``internal_effective_view``: the same capability, but **backend-internal
  only** (the context assembly). It is the one path that returns the wording of
  the System Policies that overrode a memory; no API layer may return its result
  to a user or an agent (see "The effective view").
* ``list_memories`` / ``get_memory`` with ``include_deleted=True``,
  ``list_candidates``, ``get_candidate``: capability ``shared_memory.manage``
  (the views only managers may see; they change nothing). Owner, Admin (human).
* Each operation that changes Shared Memory has a capability of its own, and it
  is that capability the Authorizer stores as the ``action`` of its audit row
  (Decision 0009, section 12): ``create_memory`` ``shared_memory.create``,
  ``edit_memory`` ``shared_memory.edit``, ``delete_memory``
  ``shared_memory.delete``, ``restore_memory`` ``shared_memory.restore``,
  ``approve_candidate`` ``shared_memory.candidate.approve``, ``reject_candidate``
  ``shared_memory.candidate.reject``. Owner, Admin (human).
* ``propose_candidate``: capability ``memory.use``. A user for themselves; an
  agent for its delegating user.

**No automatic promotion.** Managing Shared Memory (every method of the
``shared_memory.manage`` and operation capability items above) is a decision of
a human Owner or Admin. An :class:`AgentActor`, and a ``Principal`` whose role
is ``system`` (a background worker), always get
:class:`AutomaticPromotionRefusedError` from those methods, after the
Authorizer has recorded its decision and whatever it decided. A
``Principal`` whose role is neither Owner nor Admin gets
:class:`SharedMemoryPermissionError`. An agent can propose a candidate; the
candidate stays ``pending`` until an Owner or Admin approves it. There is no
other method that creates or changes a shared memory.

Order of every call
-------------------
1. Arguments are validated (``InvalidSharedMemoryInputError``), including the
   type of ``actor``; nothing is authorized or read yet.
2. The actor is authorized. A refusal is a :class:`SharedMemoryPermissionError`
   (or :class:`AutomaticPromotionRefusedError`); the database is not touched.
3. The database is used. A change appends its completion row to ``audit_events``
   as the last write of its transaction (see below).

The current version and deletion
--------------------------------
A shared memory is seen through its *current version*: the version with the
highest ``version_number``. The memory is Shared Memory when that version has
``scope = 'shared'`` and its status is ``active`` (state ``ACTIVE``) or
``deprecated`` (state ``DELETED``). Any other memory, or a memory whose current
version has another status, is "not found" for this service. Older versions are
``superseded`` and stay as history.

* ``create_memory``: version 1, ``active``, ``confirmation_state = 'confirmed'``,
  ``freshness_policy = 'permanent'``, ``actor_type = 'user'`` with the manager's
  id. Nothing else is written.
* ``edit_memory``: never overwrites. The current version becomes ``superseded``,
  a new version ``n + 1`` (``active``) is written and a ``supersedes`` relation
  (new to old, ``reason`` = the changed field names joined with ``", "``) is
  added. ``expected_version`` is the optimistic lock: it must equal the current
  version number, else :class:`SharedMemoryVersionConflictError`. An edit that
  changes nothing writes nothing and returns the current memory. A deleted
  memory cannot be edited (:class:`SharedMemoryStateError`).
* ``delete_memory``: the current version's status becomes ``deprecated``. Nothing
  is erased. ``restore_memory`` sets it back to ``active``. Who deleted or
  restored, and when, is only in the audit trail (the memory row keeps neither):
  the Authorizer's row (the *attempt*: its ``action`` is ``shared_memory.delete``
  or ``shared_memory.restore``, written before the status changes, so it does not
  say that the change happened) and the completion row below.

The audit trail of a change (``audit.py``, Decision 0009 section 13)
--------------------------------------------------------------------
Every method that changes Shared Memory (the six of the operation capabilities)
appends one completion row to ``audit_events`` **in the transaction of the
change**: the same ``action`` as the attempt, ``decision`` ``allow``, ``reason``
``completed``, the acting Owner or Admin (``actor_id``, ``actor_role``), the
resource, the transition time (``occurred_at``: the service clock, the reading
that stamps a new version) and the ``correlation_id`` of the attempt (the service
creates one per call and hands it to the Authorizer). So the completion row
exists if and only if the change committed; a rollback, a failed statement or a
failed commit takes it back, and a completion row that cannot be written takes
the change back (fail-closed). A call that fails after the attempt (missing
memory, wrong state, version conflict, lock timeout, failed update) leaves the
attempt without a completion. A call that changes nothing (an edit with no
differences) writes none. There is no failure record: it would have to be written
after the rollback, so its absence would prove nothing. The attempt keeps its
rules: written first, and an audit failure refuses the call.

Shared Memory candidates
------------------------
A candidate is a proposed shared memory with its proposer and provenance. It is
visible to the Owner and Admin only (through ``list_candidates`` /
``get_candidate``), because it holds content that came from a private memory.
``approve_candidate`` writes a new shared memory from it (version 1, plus a
``memory_sources`` row of type ``user_confirmation`` with ``source_ref =
"shared_memory_candidate:<candidate id>"``) and marks the candidate ``approved``
in the same transaction; ``reject_candidate`` marks it ``rejected``. Both record
the deciding user and the time. The state machine is
``lifecycle.next_candidate_state``. A proposer may have at most
``max_pending_candidates`` (default 50) pending candidates
(:class:`CandidateLimitError`); the count is race-free.

The effective view
------------------
``effective_view`` returns a page of the active shared memories with the System
Security Policy applied (``precedence.resolve_effective_view``): a memory whose
declared ``policy_subjects`` are covered by a policy item is suppressed and only
named in ``overridden``. The policy is loaded on every call. If it cannot be
loaded the call raises :class:`PolicySourceError` and returns no memory.

The result of ``effective_view`` (:class:`EffectiveSharedMemory`) never carries
the wording of a System Policy: users and agents do not see it (Decision 0009,
section 10). ``internal_effective_view`` runs the same steps (same
authorization, same policy load, same fail-closed rule) and returns an
:class:`InternalEffectiveView`, which adds the policy items that won
(``applied_policies``, with their ``statement``). It exists for the backend's
own context assembly. It is a separate method rather than a flag so that no
argument of ``effective_view`` can turn the wording on; whoever exposes this
service over HTTP (a later issue) exposes ``effective_view`` and not
``internal_effective_view``.

Concurrency
-----------
The database runs at READ COMMITTED. Every transaction that writes starts with
``SET LOCAL lock_timeout`` (``lock_timeout_ms``); a lock wait that exceeds it
raises :class:`SharedMemoryBusyError`, with nothing changed. Operations on one
memory (``edit_memory``, ``delete_memory``, ``restore_memory``) take a
transaction-level advisory lock keyed by ``memory_lock_key(memory_id)`` first,
then read the current version with a new statement, so two of them never
interleave. ``propose_candidate`` takes ``proposer_lock_key(user_id)``. A
candidate is locked with ``SELECT ... FOR UPDATE`` before it is decided. The
reads never wait for a lock.

Errors
------
Messages are fixed strings; they never contain caller content, ids, policy text,
driver messages or SQL (see ``errors.py``). A stored shared memory whose
``policy_subjects`` are malformed raises :class:`SharedMemoryDataError` when it
is read. Database errors the service does not handle propagate unchanged.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg.errors
from sqlalchemy import Select, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import (
    Authorizer,
    Capability,
    Decision,
    Principal,
    Resource,
    SystemRole,
)
from paw_backend.db import Database
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
from paw_backend.memory.shared import limits
from paw_backend.memory.shared.audit import completion_event, record_completion
from paw_backend.memory.shared.errors import (
    AutomaticPromotionRefusedError,
    CandidateLimitError,
    CandidateNotFoundError,
    InputProblem,
    InvalidSharedMemoryInputError,
    RulesContractError,
    SharedMemoryBusyError,
    SharedMemoryDataError,
    SharedMemoryNotFoundError,
    SharedMemoryPermissionError,
)
from paw_backend.memory.shared.lifecycle import (
    check_deletable,
    check_restorable,
    draft_from_candidate,
    next_candidate_state,
    plan_edit,
)
from paw_backend.memory.shared.models import SharedMemoryCandidateRow
from paw_backend.memory.shared.policy import SystemPolicySource, load_policies
from paw_backend.memory.shared.precedence import resolve_effective_view
from paw_backend.memory.shared.records import (
    EDITABLE_FIELDS,
    Actor,
    AgentActor,
    CandidateAction,
    CandidateDecision,
    CandidateProposal,
    CandidateState,
    EditPlan,
    EffectiveSharedMemory,
    InternalEffectiveView,
    OriginScope,
    SharedMemory,
    SharedMemoryCandidate,
    SharedMemoryChanges,
    SharedMemoryDraft,
    SharedMemoryStatus,
)
from paw_backend.memory.shared.validation import (
    validate_aware_datetime,
    validate_enum,
    validate_int,
    validate_optional_text,
    validate_page,
    validate_subject,
    validate_uuid,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

RESOURCE_MEMORY = "shared_memory"
RESOURCE_CANDIDATE = "shared_memory_candidate"
SOURCE_REF_PREFIX = "shared_memory_candidate:"
MANAGER_ROLES = frozenset({SystemRole.OWNER, SystemRole.ADMIN})

_MEMORIES = Memory.__table__
_VERSIONS = MemoryVersion.__table__
_RELATIONS = MemoryRelation.__table__
_SOURCES = MemorySource.__table__
_CANDIDATES = SharedMemoryCandidateRow.__table__

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_LIVE = (MemoryStatus.ACTIVE.value,)
_LIVE_OR_DELETED = (MemoryStatus.ACTIVE.value, MemoryStatus.DEPRECATED.value)


def memory_lock_key(memory_id: UUID) -> str:
    """The advisory-lock key that serialises writes to one shared memory."""
    return f"paw.shared_memory.{memory_id}"


def proposer_lock_key(user_id: UUID) -> str:
    """The advisory-lock key that serialises the proposals of one user."""
    return f"paw.shared_memory.proposer.{user_id}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _check_type(field: str, value: object, kind: type) -> None:
    if value is None:
        raise InvalidSharedMemoryInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, kind):
        raise InvalidSharedMemoryInputError(field, InputProblem.WRONG_TYPE)


def _check_bool(field: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise InvalidSharedMemoryInputError(field, InputProblem.WRONG_TYPE)
    return value


def _subjects_of(attributes: object) -> tuple[str, ...]:
    """The ``policy_subjects`` stored in a version's ``attributes``.

    Absent means none. Anything that is not a list of at most
    ``MAX_POLICY_SUBJECTS`` valid subjects is malformed:
    :class:`SharedMemoryDataError`.
    """
    if not isinstance(attributes, dict):
        raise SharedMemoryDataError
    raw = attributes.get("policy_subjects", [])
    if not isinstance(raw, list) or len(raw) > limits.MAX_POLICY_SUBJECTS:
        raise SharedMemoryDataError
    try:
        return tuple(sorted({validate_subject(item) for item in raw}))
    except InvalidSharedMemoryInputError:
        raise SharedMemoryDataError from None


def _attributes_of(draft: SharedMemoryDraft) -> dict[str, Any]:
    return (
        {"policy_subjects": list(draft.policy_subjects)}
        if draft.policy_subjects
        else {}
    )


def _memory_from_row(row: Any) -> SharedMemory:
    return SharedMemory(
        memory_id=row.memory_id,
        version_id=row.id,
        version_number=row.version_number,
        memory_type=row.memory_type,
        title=row.title,
        content=row.content,
        importance=row.importance,
        policy_subjects=_subjects_of(row.attributes),
        status=(
            SharedMemoryStatus.ACTIVE
            if row.status == MemoryStatus.ACTIVE.value
            else SharedMemoryStatus.DELETED
        ),
        created_at=row.memory_created_at,
        updated_at=row.created_at,
    )


def _candidate_from_row(row: Any) -> SharedMemoryCandidate:
    return SharedMemoryCandidate(
        candidate_id=row.id,
        state=CandidateState(row.state),
        proposer_user_id=row.proposer_user_id,
        proposer_agent_id=row.proposer_agent_id,
        origin_scope=OriginScope(row.origin_scope),
        origin_version_id=row.origin_version_id,
        memory_type=row.memory_type,
        title=row.title,
        content=row.content,
        importance=row.importance,
        policy_subjects=tuple(row.policy_subjects),
        reason=row.reason,
        created_at=row.created_at,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        decision_reason=row.decision_reason,
        memory_id=row.memory_id,
    )


def _current_versions() -> Select[Any]:
    """Every memory joined to its current (highest numbered) shared version."""
    newest = (
        select(func.max(_VERSIONS.c.version_number))
        .where(_VERSIONS.c.memory_id == _MEMORIES.c.id)
        .correlate(_MEMORIES)
        .scalar_subquery()
    )
    return (
        select(*_VERSIONS.c, _MEMORIES.c.created_at.label("memory_created_at"))
        .join(_MEMORIES, _MEMORIES.c.id == _VERSIONS.c.memory_id)
        .where(
            _VERSIONS.c.version_number == newest,
            _VERSIONS.c.scope == MemoryScope.SHARED.value,
        )
    )


class SharedMemoryService:
    """Shared Memory administration (see the module docstring for the rules)."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        policies: SystemPolicySource,
        *,
        clock: Clock = _utc_now,
        lock_timeout_ms: int = limits.DEFAULT_LOCK_TIMEOUT_MS,
        policy_timeout_seconds: float = limits.DEFAULT_POLICY_TIMEOUT_SECONDS,
        max_pending_candidates: int = limits.MAX_PENDING_CANDIDATES_PER_PROPOSER,
    ) -> None:
        """Validate the collaborators up front; a wrong one fails here, loudly.

        ``authorizer`` must have callable ``authorize`` and
        ``authorize_agent_action``; ``policies`` a callable ``items``; ``clock``
        must be callable without arguments and return an aware ``datetime``
        (checked at every use). ``lock_timeout_ms`` is an ``int`` from 1 to
        60000, ``policy_timeout_seconds`` an ``int`` or ``float`` (not a
        ``bool``) above 0 and at most 60, ``max_pending_candidates`` an ``int``
        from 1 to 1000. Otherwise ``InvalidSharedMemoryInputError``.
        """
        _check_type("database", database, Database)
        for name in ("authorize", "authorize_agent_action"):
            if not callable(getattr(authorizer, name, None)):
                raise InvalidSharedMemoryInputError(
                    "authorizer", InputProblem.WRONG_TYPE
                )
        if not callable(getattr(policies, "items", None)):
            raise InvalidSharedMemoryInputError("policies", InputProblem.WRONG_TYPE)
        if not callable(clock):
            raise InvalidSharedMemoryInputError("clock", InputProblem.WRONG_TYPE)
        validate_int(
            "lock_timeout_ms", lock_timeout_ms, low=1, high=limits.MAX_LOCK_TIMEOUT_MS
        )
        validate_int("max_pending_candidates", max_pending_candidates, low=1, high=1000)
        if isinstance(policy_timeout_seconds, bool) or not isinstance(
            policy_timeout_seconds, int | float
        ):
            raise InvalidSharedMemoryInputError(
                "policy_timeout_seconds", InputProblem.WRONG_TYPE
            )
        if not 0 < policy_timeout_seconds <= limits.MAX_POLICY_TIMEOUT_SECONDS:
            raise InvalidSharedMemoryInputError(
                "policy_timeout_seconds", InputProblem.OUT_OF_RANGE
            )
        self._database = database
        self._authorizer = authorizer
        self._policies = policies
        self._clock = clock
        self._lock_timeout_ms = lock_timeout_ms
        self._policy_timeout_seconds = float(policy_timeout_seconds)
        self._max_pending = max_pending_candidates

    # -- helpers ---------------------------------------------------------------

    def _now(self) -> datetime:
        return validate_aware_datetime("clock", self._clock())

    @staticmethod
    def _check_actor(actor: object) -> None:
        _check_type("actor", actor, Principal | AgentActor)  # type: ignore[arg-type]

    async def _decide(
        self,
        actor: Actor,
        capability: Capability,
        resource: Resource,
        correlation_id: UUID | None = None,
    ) -> Decision | None:
        """Ask the Authorizer; its answer if it is a ``Decision``, else ``None``.

        ``correlation_id`` is stored on the audit row of the decision, so that the
        completion row of the change (``audit.py``) can be tied to it.
        """
        if isinstance(actor, AgentActor):
            decision = await self._authorizer.authorize_agent_action(
                actor.delegator_id,
                actor.grant,
                capability,
                resource,
                correlation_id=correlation_id,
            )
        else:
            decision = await self._authorizer.authorize(
                actor, capability, resource, correlation_id=correlation_id
            )
        return decision if isinstance(decision, Decision) else None

    async def _authorize(
        self, actor: Actor, capability: Capability, resource: Resource
    ) -> UUID:
        """Authorize a read or a self-service action; return the acting user's id."""
        decision = await self._decide(actor, capability, resource)
        if decision is None or not decision.allowed:
            reason = "invalid_decision" if decision is None else decision.reason.value
            raise SharedMemoryPermissionError(reason)
        return actor.delegator_id if isinstance(actor, AgentActor) else actor.user_id

    async def _authorize_manage(
        self,
        actor: Actor,
        capability: Capability,
        resource: Resource,
        correlation_id: UUID | None = None,
    ) -> Principal:
        """Authorize a managing ``capability``; only a human Owner or Admin passes.

        Returns that Owner or Admin. ``capability`` is the operation's own (there
        is no default): the Authorizer stores it as the ``action`` of its audit
        row, which is how the history tells a deletion from a restoration.
        ``correlation_id`` (a change passes a new one) is stored on that row and
        on the completion row of the change.

        The Authorizer decides first (and audits). An agent, and the ``system``
        role, are refused with :class:`AutomaticPromotionRefusedError` whatever
        the decision was; any other refusal is a permission error. Even an
        allowed decision is refused unless the principal is an Owner or Admin.
        """
        decision = await self._decide(actor, capability, resource, correlation_id)
        reason = (
            "invalid_decision"
            if decision is None
            else decision.reason.value
            if not decision.allowed
            else "not_a_human_owner_or_admin"
        )
        if isinstance(actor, AgentActor) or actor.system_role is SystemRole.SYSTEM:
            raise AutomaticPromotionRefusedError(reason)
        if decision is None or not decision.allowed:
            raise SharedMemoryPermissionError(reason)
        if actor.system_role not in MANAGER_ROLES:
            raise SharedMemoryPermissionError(reason)
        return actor

    @asynccontextmanager
    async def _transaction(
        self, lock_key: str | None = None
    ) -> AsyncIterator[AsyncSession]:
        """One transaction with the lock timeout and, if given, an advisory lock."""
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(
                    select(
                        func.set_config(
                            "lock_timeout", str(self._lock_timeout_ms), True
                        )
                    )
                )
                if lock_key is not None:
                    await session.execute(_LOCK_SQL, {"key": lock_key})
                yield session
        except DBAPIError as error:
            # Only the type of the driver's error is read, never its text.
            if isinstance(error.orig, psycopg.errors.LockNotAvailable):
                raise SharedMemoryBusyError from None
            raise

    @staticmethod
    async def _read_current(
        session: AsyncSession, memory_id: UUID, *, include_deleted: bool
    ) -> SharedMemory:
        """The shared memory ``memory_id``, or :class:`SharedMemoryNotFoundError`."""
        statement = _current_versions().where(_VERSIONS.c.memory_id == memory_id)
        row = (await session.execute(statement)).first()
        allowed = _LIVE_OR_DELETED if include_deleted else _LIVE
        if row is None or row.status not in allowed:
            raise SharedMemoryNotFoundError
        return _memory_from_row(row)

    async def _insert_memory(
        self,
        session: AsyncSession,
        draft: SharedMemoryDraft,
        user_id: UUID,
        now: datetime,
        *,
        source_ref: str | None = None,
    ) -> UUID:
        """Insert a memory with its version 1 (and a source); return the memory id."""
        memory_id = (
            await session.execute(
                insert(_MEMORIES).values(created_at=now).returning(_MEMORIES.c.id)
            )
        ).scalar_one()
        version_id = (
            await session.execute(
                insert(_VERSIONS)
                .values(**self._version_values(memory_id, 1, draft, user_id, now))
                .returning(_VERSIONS.c.id)
            )
        ).scalar_one()
        if source_ref is not None:
            await session.execute(
                insert(_SOURCES).values(
                    memory_version_id=version_id,
                    source_type=SourceType.USER_CONFIRMATION.value,
                    source_ref=source_ref,
                    created_at=now,
                )
            )
        return memory_id

    @staticmethod
    def _version_values(
        memory_id: UUID,
        number: int,
        draft: SharedMemoryDraft,
        user_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "memory_id": memory_id,
            "version_number": number,
            "scope": MemoryScope.SHARED.value,
            "memory_type": draft.memory_type,
            "title": draft.title,
            "content": draft.content,
            "importance": draft.importance,
            "status": MemoryStatus.ACTIVE.value,
            "confirmation_state": ConfirmationState.CONFIRMED.value,
            "freshness_policy": FreshnessPolicy.PERMANENT.value,
            "attributes": _attributes_of(draft),
            "actor_type": ActorType.USER.value,
            "actor_user_id": user_id,
            "change_reason": draft.reason,
            "created_at": now,
        }

    async def _set_status(
        self,
        session: AsyncSession,
        version_id: UUID,
        old: MemoryStatus,
        new: MemoryStatus,
    ) -> None:
        """Change a version's status if (and only if) it still is ``old``."""
        result = await session.execute(
            update(_VERSIONS)
            .where(_VERSIONS.c.id == version_id, _VERSIONS.c.status == old.value)
            .values(status=new.value)
        )
        if result.rowcount != 1:
            raise SharedMemoryBusyError

    @staticmethod
    async def _complete(
        session: AsyncSession,
        manager: Principal,
        capability: Capability,
        resource_kind: str,
        resource_id: UUID,
        correlation_id: UUID,
        now: datetime,
    ) -> None:
        """Append the completion row of the change, in the transaction of the change.

        The last write of every change: if it fails, the change is rolled back
        with it (see ``audit.py``).
        """
        await record_completion(
            session,
            completion_event(
                manager, capability, resource_kind, resource_id, correlation_id, now
            ),
        )

    # -- read ------------------------------------------------------------------

    async def _page(
        self, *, statuses: tuple[str, ...], limit: int, offset: int
    ) -> list[SharedMemory]:
        statement = (
            _current_versions()
            .where(_VERSIONS.c.status.in_(statuses))
            .order_by(_MEMORIES.c.created_at, _MEMORIES.c.id)
            .limit(limit)
            .offset(offset)
        )
        async with self._database.session() as session:
            rows = (await session.execute(statement)).all()
        return [_memory_from_row(row) for row in rows]

    async def list_memories(
        self,
        actor: Actor,
        *,
        include_deleted: bool = False,
        limit: int = limits.DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[SharedMemory]:
        """A page of shared memories, oldest first (``created_at``, then id).

        Active memories only, for every active user. ``include_deleted=True``
        adds the deleted ones and needs ``shared_memory.manage`` (Owner or
        Admin). ``limit`` is 1 to 200, ``offset`` 0 to 100000.
        """
        self._check_actor(actor)
        include_deleted = _check_bool("include_deleted", include_deleted)
        limit, offset = validate_page(limit, offset)
        resource = Resource(kind=RESOURCE_MEMORY)
        if include_deleted:
            await self._authorize_manage(
                actor, Capability.SHARED_MEMORY_MANAGE, resource
            )
        else:
            await self._authorize(actor, Capability.SHARED_MEMORY_READ, resource)
        statuses = _LIVE_OR_DELETED if include_deleted else _LIVE
        return await self._page(statuses=statuses, limit=limit, offset=offset)

    async def get_memory(
        self, actor: Actor, memory_id: UUID, *, include_deleted: bool = False
    ) -> SharedMemory:
        """One shared memory (:class:`SharedMemoryNotFoundError` if there is none)."""
        self._check_actor(actor)
        memory_uuid = validate_uuid("memory_id", memory_id)
        include_deleted = _check_bool("include_deleted", include_deleted)
        resource = Resource(kind=RESOURCE_MEMORY, id=memory_uuid)
        if include_deleted:
            await self._authorize_manage(
                actor, Capability.SHARED_MEMORY_MANAGE, resource
            )
        else:
            await self._authorize(actor, Capability.SHARED_MEMORY_READ, resource)
        async with self._database.session() as session:
            return await self._read_current(
                session, memory_uuid, include_deleted=include_deleted
            )

    async def effective_view(
        self,
        actor: Actor,
        *,
        limit: int = limits.DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> EffectiveSharedMemory:
        """A page of the active shared memories with the System Policy applied.

        The page is the one ``list_memories`` would return. The result has no
        policy wording (only the ids of the policies that won): it is what a user
        or an agent may be shown. Raises :class:`PolicySourceError` (and returns
        no memory) when the policy cannot be loaded.
        """
        view = await self._resolved_view(actor, limit=limit, offset=offset)
        return view.public()

    async def internal_effective_view(
        self,
        actor: Actor,
        *,
        limit: int = limits.DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> InternalEffectiveView:
        """``effective_view`` plus the policies that won, for the backend only.

        **Backend-internal:** the result holds the wording of the System
        Policies that overrode a memory (``applied_policies``), which users and
        agents never see (Decision 0009, section 10). Only the backend's own
        context assembly calls this; never return it, or anything made from its
        ``applied_policies``, to a user or an agent. Same authorization
        (``shared_memory.read``), same policy load and same fail-closed rule as
        ``effective_view``.
        """
        return await self._resolved_view(actor, limit=limit, offset=offset)

    async def _resolved_view(
        self, actor: Actor, *, limit: int, offset: int
    ) -> InternalEffectiveView:
        self._check_actor(actor)
        limit, offset = validate_page(limit, offset)
        await self._authorize(
            actor, Capability.SHARED_MEMORY_READ, Resource(kind=RESOURCE_MEMORY)
        )
        policies = await load_policies(
            self._policies, timeout_seconds=self._policy_timeout_seconds
        )
        memories = await self._page(statuses=_LIVE, limit=limit, offset=offset)
        view = resolve_effective_view(memories, policies)
        if not isinstance(view, InternalEffectiveView):
            raise RulesContractError("resolve_effective_view")
        return view

    # -- manage ----------------------------------------------------------------

    async def create_memory(
        self, actor: Actor, draft: SharedMemoryDraft
    ) -> SharedMemory:
        """Create a shared memory (version 1). Owner or Admin only."""
        self._check_actor(actor)
        _check_type("draft", draft, SharedMemoryDraft)
        capability, correlation_id = Capability.SHARED_MEMORY_CREATE, uuid4()
        manager = await self._authorize_manage(
            actor, capability, Resource(kind=RESOURCE_MEMORY), correlation_id
        )
        now = self._now()
        async with self._transaction() as session:
            memory_id = await self._insert_memory(session, draft, manager.user_id, now)
            await self._complete(
                session,
                manager,
                capability,
                RESOURCE_MEMORY,
                memory_id,
                correlation_id,
                now,
            )
            return await self._read_current(session, memory_id, include_deleted=True)

    async def edit_memory(
        self,
        actor: Actor,
        memory_id: UUID,
        expected_version: int,
        changes: SharedMemoryChanges,
    ) -> SharedMemory:
        """Edit a shared memory as a new version. Owner or Admin only.

        See the module docstring ("The current version and deletion") and
        ``lifecycle.plan_edit`` for the rules.
        """
        self._check_actor(actor)
        memory_uuid = validate_uuid("memory_id", memory_id)
        version = validate_int(
            "expected_version", expected_version, low=1, high=limits.MAX_VERSION_NUMBER
        )
        _check_type("changes", changes, SharedMemoryChanges)
        capability, correlation_id = Capability.SHARED_MEMORY_EDIT, uuid4()
        manager = await self._authorize_manage(
            actor,
            capability,
            Resource(kind=RESOURCE_MEMORY, id=memory_uuid),
            correlation_id,
        )
        now = self._now()
        async with self._transaction(memory_lock_key(memory_uuid)) as session:
            current = await self._read_current(
                session, memory_uuid, include_deleted=True
            )
            plan = plan_edit(current, version, changes)
            if plan is None:
                return current
            self._check_plan(plan, current, version)
            await self._set_status(
                session,
                current.version_id,
                MemoryStatus.ACTIVE,
                MemoryStatus.SUPERSEDED,
            )
            new_version_id = (
                await session.execute(
                    insert(_VERSIONS)
                    .values(
                        **self._version_values(
                            memory_uuid,
                            current.version_number + 1,
                            plan.draft,
                            manager.user_id,
                            now,
                        )
                    )
                    .returning(_VERSIONS.c.id)
                )
            ).scalar_one()
            await session.execute(
                insert(_RELATIONS).values(
                    from_version_id=new_version_id,
                    to_version_id=current.version_id,
                    relation_type=RelationType.SUPERSEDES.value,
                    reason=", ".join(plan.changed_fields),
                    created_at=now,
                )
            )
            await self._complete(
                session,
                manager,
                capability,
                RESOURCE_MEMORY,
                memory_uuid,
                correlation_id,
                now,
            )
            return await self._read_current(session, memory_uuid, include_deleted=True)

    @staticmethod
    def _check_plan(plan: object, current: SharedMemory, expected_version: int) -> None:
        """The plan of ``plan_edit`` must keep its contract, or nothing is written."""
        ok = (
            isinstance(plan, EditPlan)
            and isinstance(plan.draft, SharedMemoryDraft)
            and isinstance(plan.changed_fields, tuple)
            and len(plan.changed_fields) > 0
            and list(plan.changed_fields)
            == sorted(set(plan.changed_fields) & set(EDITABLE_FIELDS))
            and expected_version == current.version_number
            and current.status is SharedMemoryStatus.ACTIVE
        )
        if not ok:
            raise RulesContractError("plan_edit")

    async def delete_memory(self, actor: Actor, memory_id: UUID) -> SharedMemory:
        """Delete (deprecate) the current version; it is kept. Owner or Admin only."""
        return await self._change_state(actor, memory_id, delete=True)

    async def restore_memory(self, actor: Actor, memory_id: UUID) -> SharedMemory:
        """Restore a deleted shared memory. Owner or Admin only."""
        return await self._change_state(actor, memory_id, delete=False)

    async def _change_state(
        self, actor: Actor, memory_id: UUID, *, delete: bool
    ) -> SharedMemory:
        self._check_actor(actor)
        memory_uuid = validate_uuid("memory_id", memory_id)
        capability = (
            Capability.SHARED_MEMORY_DELETE
            if delete
            else Capability.SHARED_MEMORY_RESTORE
        )
        correlation_id = uuid4()
        manager = await self._authorize_manage(
            actor,
            capability,
            Resource(kind=RESOURCE_MEMORY, id=memory_uuid),
            correlation_id,
        )
        now = self._now()
        async with self._transaction(memory_lock_key(memory_uuid)) as session:
            current = await self._read_current(
                session, memory_uuid, include_deleted=True
            )
            if delete:
                check_deletable(current)
                old, new = MemoryStatus.ACTIVE, MemoryStatus.DEPRECATED
            else:
                check_restorable(current)
                old, new = MemoryStatus.DEPRECATED, MemoryStatus.ACTIVE
            await self._set_status(session, current.version_id, old, new)
            await self._complete(
                session,
                manager,
                capability,
                RESOURCE_MEMORY,
                memory_uuid,
                correlation_id,
                now,
            )
            return await self._read_current(session, memory_uuid, include_deleted=True)

    # -- candidates ------------------------------------------------------------

    async def propose_candidate(
        self, actor: Actor, proposal: CandidateProposal
    ) -> SharedMemoryCandidate:
        """Propose content for Shared Memory; it becomes a ``pending`` candidate.

        A user proposes for themselves; an agent for its delegating user
        (``memory.use``). Nothing is promoted.
        """
        self._check_actor(actor)
        _check_type("proposal", proposal, CandidateProposal)
        user_id = actor.delegator_id if isinstance(actor, AgentActor) else actor.user_id
        agent_id = actor.grant.agent_id if isinstance(actor, AgentActor) else None
        await self._authorize(
            actor,
            Capability.MEMORY_USE,
            Resource.owned_by(user_id, RESOURCE_CANDIDATE),
        )
        now = self._now()
        async with self._transaction(proposer_lock_key(user_id)) as session:
            pending = (
                await session.execute(
                    select(func.count())
                    .select_from(_CANDIDATES)
                    .where(
                        _CANDIDATES.c.proposer_user_id == user_id,
                        _CANDIDATES.c.state == CandidateState.PENDING.value,
                    )
                )
            ).scalar_one()
            if pending >= self._max_pending:
                raise CandidateLimitError
            row = (
                await session.execute(
                    insert(_CANDIDATES)
                    .values(
                        state=CandidateState.PENDING.value,
                        proposer_user_id=user_id,
                        proposer_agent_id=agent_id,
                        origin_scope=proposal.origin_scope.value,
                        origin_version_id=proposal.origin_version_id,
                        memory_type=proposal.memory_type,
                        title=proposal.title,
                        content=proposal.content,
                        importance=proposal.importance,
                        policy_subjects=list(proposal.policy_subjects),
                        reason=proposal.reason,
                        created_at=now,
                    )
                    .returning(*_CANDIDATES.c)
                )
            ).one()
            return _candidate_from_row(row)

    async def list_candidates(
        self,
        actor: Actor,
        *,
        state: CandidateState | None = None,
        limit: int = limits.DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[SharedMemoryCandidate]:
        """A page of candidates, oldest first (``created_at``, id). Owner or Admin."""
        self._check_actor(actor)
        if state is not None:
            validate_enum("state", state, CandidateState)
        limit, offset = validate_page(limit, offset)
        await self._authorize_manage(
            actor, Capability.SHARED_MEMORY_MANAGE, Resource(kind=RESOURCE_CANDIDATE)
        )
        statement = select(_CANDIDATES).order_by(
            _CANDIDATES.c.created_at, _CANDIDATES.c.id
        )
        if state is not None:
            statement = statement.where(_CANDIDATES.c.state == state.value)
        async with self._database.session() as session:
            rows = (await session.execute(statement.limit(limit).offset(offset))).all()
        return [_candidate_from_row(row) for row in rows]

    async def get_candidate(
        self, actor: Actor, candidate_id: UUID
    ) -> SharedMemoryCandidate:
        """One candidate. Owner or Admin only."""
        self._check_actor(actor)
        candidate_uuid = validate_uuid("candidate_id", candidate_id)
        await self._authorize_manage(
            actor,
            Capability.SHARED_MEMORY_MANAGE,
            Resource(kind=RESOURCE_CANDIDATE, id=candidate_uuid),
        )
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(_CANDIDATES).where(_CANDIDATES.c.id == candidate_uuid)
                )
            ).first()
        if row is None:
            raise CandidateNotFoundError
        return _candidate_from_row(row)

    @staticmethod
    async def _lock_candidate(
        session: AsyncSession, candidate_id: UUID
    ) -> SharedMemoryCandidate:
        row = (
            await session.execute(
                select(_CANDIDATES)
                .where(_CANDIDATES.c.id == candidate_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            raise CandidateNotFoundError
        return _candidate_from_row(row)

    async def approve_candidate(
        self, actor: Actor, candidate_id: UUID, *, reason: str | None = None
    ) -> CandidateDecision:
        """Approve a pending candidate: it becomes a shared memory. Owner or Admin only.

        One transaction: the candidate is locked, decided by
        ``next_candidate_state`` (a decided candidate is
        :class:`SharedMemoryStateError`), the memory is written from
        ``draft_from_candidate`` (version 1, ``confirmed``, source
        ``user_confirmation``) and the candidate becomes ``approved`` with the
        deciding user, the time, the optional ``reason`` and the new memory id.
        """
        self._check_actor(actor)
        candidate_uuid = validate_uuid("candidate_id", candidate_id)
        note = validate_optional_text(
            "reason", reason, max_chars=limits.MAX_REASON_CHARS
        )
        capability, correlation_id = Capability.SHARED_MEMORY_CANDIDATE_APPROVE, uuid4()
        manager = await self._authorize_manage(
            actor,
            capability,
            Resource(kind=RESOURCE_CANDIDATE, id=candidate_uuid),
            correlation_id,
        )
        user_id = manager.user_id
        now = self._now()
        async with self._transaction() as session:
            candidate = await self._lock_candidate(session, candidate_uuid)
            new_state = next_candidate_state(candidate.state, CandidateAction.APPROVE)
            if new_state is not CandidateState.APPROVED:
                raise RulesContractError("next_candidate_state")
            draft = draft_from_candidate(candidate)
            if not (
                isinstance(draft, SharedMemoryDraft)
                and (draft.memory_type, draft.title, draft.content, draft.importance)
                == (
                    candidate.memory_type,
                    candidate.title,
                    candidate.content,
                    candidate.importance,
                )
                and draft.policy_subjects == candidate.policy_subjects
            ):
                raise RulesContractError("draft_from_candidate")
            memory_id = await self._insert_memory(
                session,
                draft,
                user_id,
                now,
                source_ref=f"{SOURCE_REF_PREFIX}{candidate_uuid}",
            )
            await self._decide_row(
                session, candidate_uuid, new_state, user_id, now, note, memory_id
            )
            await self._complete(
                session,
                manager,
                capability,
                RESOURCE_CANDIDATE,
                candidate_uuid,
                correlation_id,
                now,
            )
            memory = await self._read_current(session, memory_id, include_deleted=True)
            return CandidateDecision(
                candidate=await self._reload_candidate(session, candidate_uuid),
                memory=memory,
            )

    async def reject_candidate(
        self, actor: Actor, candidate_id: UUID, *, reason: str | None = None
    ) -> CandidateDecision:
        """Reject a pending candidate. Owner or Admin only. Nothing else is written."""
        self._check_actor(actor)
        candidate_uuid = validate_uuid("candidate_id", candidate_id)
        note = validate_optional_text(
            "reason", reason, max_chars=limits.MAX_REASON_CHARS
        )
        capability, correlation_id = Capability.SHARED_MEMORY_CANDIDATE_REJECT, uuid4()
        manager = await self._authorize_manage(
            actor,
            capability,
            Resource(kind=RESOURCE_CANDIDATE, id=candidate_uuid),
            correlation_id,
        )
        now = self._now()
        async with self._transaction() as session:
            candidate = await self._lock_candidate(session, candidate_uuid)
            new_state = next_candidate_state(candidate.state, CandidateAction.REJECT)
            if new_state is not CandidateState.REJECTED:
                raise RulesContractError("next_candidate_state")
            await self._decide_row(
                session, candidate_uuid, new_state, manager.user_id, now, note, None
            )
            await self._complete(
                session,
                manager,
                capability,
                RESOURCE_CANDIDATE,
                candidate_uuid,
                correlation_id,
                now,
            )
            return CandidateDecision(
                candidate=await self._reload_candidate(session, candidate_uuid),
                memory=None,
            )

    @staticmethod
    async def _decide_row(
        session: AsyncSession,
        candidate_id: UUID,
        state: CandidateState,
        user_id: UUID,
        now: datetime,
        reason: str | None,
        memory_id: UUID | None,
    ) -> None:
        result = await session.execute(
            update(_CANDIDATES)
            .where(
                _CANDIDATES.c.id == candidate_id,
                _CANDIDATES.c.state == CandidateState.PENDING.value,
            )
            .values(
                state=state.value,
                decided_by=user_id,
                decided_at=now,
                decision_reason=reason,
                memory_id=memory_id,
            )
        )
        if result.rowcount != 1:
            raise SharedMemoryBusyError

    @staticmethod
    async def _reload_candidate(
        session: AsyncSession, candidate_id: UUID
    ) -> SharedMemoryCandidate:
        row = (
            await session.execute(
                select(_CANDIDATES).where(_CANDIDATES.c.id == candidate_id)
            )
        ).one()
        return _candidate_from_row(row)
