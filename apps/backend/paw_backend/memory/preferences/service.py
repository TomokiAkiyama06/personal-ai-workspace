"""Inferred Preference confirmation (PAW-044): candidates, preview, confirm, reject.

``PreferenceConfirmationService`` extends ``MemoryVersioningService`` (PAW-042): a
confirmation is a person's change of their own memory, written with the same
transaction, lock, authorization and history rules (Decision 0034), plus what the
requirements give only to this flow (Decision 0018: "範囲を広げるのは確認 Flow
(PAW-044) が新しい Version で行う"). The choices the requirements leave open are
decided in Decision 0081; ``rules.py`` holds the pure ones.

Candidates (``candidates``)
---------------------------
Read for the person only, from their own rows (``memory.read`` on their own memory,
like the Memory screen: an allowed read writes no audit row):

* **memory** candidates: the person's private (``user``), ``active`` and unconfirmed
  (``observed`` / ``inferred``) memory versions that the Immediate Journal wrote for
  a worker key (``memory_consolidation_keys``);
* **held** candidates: per key, the latest unanswered item of a consolidated journal
  entry that the consolidator held for the person (``held_high_risk``,
  ``held_confirmed``, ``held_widened``), unless a later observation of the key was
  written anyway (the held one is outdated).

Each comes with its evidence (the observations of its key: the journal outcome items
of the person's entries, with the project / repository the conversation was in and
the strength of the person's words, read from their own messages and never
returned), the recommended scope, the buttons and whether to ask now.

Confirm / reject
----------------
* A **memory** candidate is confirmed as version ``n + 1`` of the same memory:
  ``active``, ``confirmed``, written by the person, at the scope the person chose
  (their own User Memory, a project or a repository: widening needs the right to
  write there, ``project.memory.use``). Version ``n`` becomes ``superseded``; the
  relations ``supersedes`` and ``confirmed_from`` point from ``n + 1`` to ``n``; the
  sources are carried over with the person's ``user_confirmation`` (Decision 0045).
  Earlier private versions stay private (``history`` shows them to their audience
  only), so widening never shows the person's other private wording.
* A **held** candidate is written the same way as the next version of the key's
  memory (or as a new memory, registered for the key), with the source of its own
  observation; its held items are answered (``memory_preference_resolutions``).
  When the key's memory is no longer the person's private one (they widened it
  before), the candidate can only stay at that audience or be narrowed to the
  person's own memory.
* **Reject** ([保存しない]): a memory candidate gets version ``n + 1`` with
  ``confirmation_state = rejected`` and status ``deprecated`` (the consolidator then
  never brings the key back, ``blocked_by_user``); a held candidate is answered
  ``rejected`` (a newer held item of the key is a new question).
* **High risk** (merge, delete, visibility, ACL / role / permission, credentials,
  sending outside, or a "mandatory" rule): never applied without
  ``acknowledge_high_risk`` (:class:`PreferenceHighRiskError`, nothing written).
  Even then the result is a memory and nothing else: the version carries
  ``attributes.preference.policy_effect = "none"``; no ACL, role, merge policy,
  approval or permission is read from or changed by a memory (the Tool Broker and
  Approval decide those).

Locks: the journal key's advisory lock (``journal.applier.lock_keys``) first, then
the memory's (``memory_lock_key``), then rows. The consolidator takes the key lock
only, the other versioning operations the memory lock only, so no two of them wait
for each other in a circle.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import sqltypes

from paw_backend.authz import (
    Authorizer,
    Capability,
    Principal,
    Reason,
    RepoAcl,
    RepoPermission,
    Resource,
)
from paw_backend.db import Database
from paw_backend.memory.acl import Principal as AclPrincipal
from paw_backend.memory.acl import readable_memory_versions
from paw_backend.memory.journal.applier import lock_keys
from paw_backend.memory.journal.domain import ItemResult
from paw_backend.memory.journal.models import ConsolidationKey
from paw_backend.memory.journal.rules import OrderKey, is_high_risk, is_newer
from paw_backend.memory.models import (
    ActorType,
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
    MemoryStatus,
    MemoryVersion,
    RelationType,
    SourceType,
)
from paw_backend.memory.preferences import limits
from paw_backend.memory.preferences.domain import Resolution
from paw_backend.memory.preferences.errors import (
    PreferenceCandidateChangedError,
    PreferenceHighRiskError,
)
from paw_backend.memory.preferences.interpretation import (
    Interpreter,
    PreferenceInterpreter,
    PreferencePreview,
    RuleInterpreter,
    StructuredPreference,
    check_interpreter,
    parse_interpreter_output,
)
from paw_backend.memory.preferences.models import PreferenceResolution
from paw_backend.memory.preferences.records import (
    CandidateRef,
    Confirmation,
    HeldCandidateRef,
    MemoryCandidateRef,
    PreferenceCandidate,
)
from paw_backend.memory.preferences.rules import (
    HELD_RESULTS,
    OBSERVED_RESULTS,
    CandidateKind,
    Observation,
    Option,
    RiskLevel,
    TargetScope,
    evidence,
    is_ready,
    language_strength,
    options,
    recommend_scope,
)
from paw_backend.memory.versioning import limits as version_limits
from paw_backend.memory.versioning.errors import (
    InputProblem,
    MemoryPermissionError,
    MemoryStateError,
    StateProblem,
)
from paw_backend.memory.versioning.records import FreshnessSpec, MemoryVersionView
from paw_backend.memory.versioning.rules import check_manual_freshness
from paw_backend.memory.versioning.service import (
    _COPIED_SOURCE_COLUMNS,
    _LOCK_SQL,
    _MEMORIES,
    _SOURCES,
    _VERSIONS,
    RESOURCE_MEMORY,
    Clock,
    MemoryVersioningService,
    _freshness_columns,
    _utc_now,
    confirmation_source_ref,
    memory_lock_key,
    version_view,
)
from paw_backend.memory.versioning.validation import reject, validate_text
from paw_backend.repositories.models import RepositoryRow

logger = logging.getLogger(__name__)

MEMORY_TYPE = "preference"
CONFIRM_REASON = "preference confirmation"
REJECT_REASON = "preference rejected"
# The policy effect every confirmed preference records: none. A memory is read by
# an LLM as context; it never grants or changes a permission (Decision 0081 point 10).
POLICY_EFFECT = "none"

_KEYS = ConsolidationKey.__table__
_RESOLUTIONS = PreferenceResolution.__table__
_REPOSITORIES = RepositoryRow.__table__

# The current statuses a held candidate can be written over.
_ANSWERABLE = frozenset({MemoryStatus.ACTIVE, MemoryStatus.DEPRECATED})
_UNCONFIRMED = (ConfirmationState.OBSERVED.value, ConfirmationState.INFERRED.value)
_WRITTEN = (
    ItemResult.CREATED.value,
    ItemResult.UPDATED.value,
    ItemResult.DUPLICATE.value,
)


def _texts(name: str) -> Any:
    return bindparam(name, type_=ARRAY(sqltypes.Text()))


# The unanswered held items (``:key`` NULL: of every key) that no LATER written
# observation of their key made outdated. "Later" is the journal's own order
# (``journal.rules.is_newer``): in one conversation the event sequence, across
# conversations the recorded time and then the conversation id (Codex P1 on #205:
# the time alone is wrong when the clock ties or steps back in one conversation).
# The latest item per key is chosen with ``is_newer`` too (``_live_held``). The
# person's own consolidated entries only, newest first, at most ``:limit``.
_LIVE_HELD = text(
    "SELECT item->>'key' AS key, e.id AS entry_id,"
    "  CAST(item->>'index' AS integer) AS item_index,"
    "  item->>'result' AS result, item->'candidate'->>'content' AS content,"
    "  e.conversation_id, e.message_id, e.event_sequence, e.recorded_at"
    " FROM memory_journal_entries e"
    " CROSS JOIN LATERAL jsonb_array_elements(e.outcome->'items') AS item"
    " WHERE e.owner_user_id = :owner AND e.state = 'consolidated'"
    "  AND item->>'result' = ANY(:held)"
    "  AND item->'candidate'->>'content' IS NOT NULL"
    "  AND (CAST(:key AS text) IS NULL OR item->>'key' = CAST(:key AS text))"
    "  AND NOT EXISTS (SELECT 1 FROM memory_preference_resolutions r"
    "   WHERE r.entry_id = e.id AND r.item_index = CAST(item->>'index' AS integer))"
    "  AND NOT EXISTS ("
    "   SELECT 1 FROM memory_journal_entries later"
    "   CROSS JOIN LATERAL jsonb_array_elements(later.outcome->'items') AS li"
    "   WHERE later.owner_user_id = :owner AND later.state = 'consolidated'"
    "    AND li->>'key' = item->>'key' AND li->>'result' = ANY(:written)"
    "    AND CASE WHEN later.conversation_id = e.conversation_id"
    "     THEN later.event_sequence > e.event_sequence"
    "     ELSE (later.recorded_at, CAST(later.conversation_id AS text))"
    "      > (e.recorded_at, CAST(e.conversation_id AS text)) END)"
    " ORDER BY e.recorded_at DESC LIMIT :limit"
).bindparams(_texts("held"), _texts("written"))

# The observations of some keys: the person's consolidated entries, newest first,
# at most ``:per_key`` per key, with the message (for the language strength only).
_OBSERVATIONS = text(
    "SELECT * FROM ("
    " SELECT item->>'key' AS key, e.id AS entry_id, e.project_id, e.repo_id,"
    "  e.recorded_at, item->>'result' AS result, m.content AS message,"
    "  row_number() OVER (PARTITION BY item->>'key'"
    "   ORDER BY e.recorded_at DESC, e.event_sequence DESC) AS n"
    " FROM memory_journal_entries e"
    " JOIN conversations c ON c.id = e.conversation_id AND c.owner_user_id = :owner"
    " JOIN messages m ON m.id = e.message_id AND m.conversation_id = e.conversation_id"
    " CROSS JOIN LATERAL jsonb_array_elements(e.outcome->'items') AS item"
    " WHERE e.owner_user_id = :owner AND e.state = 'consolidated'"
    "  AND item->>'key' = ANY(:keys) AND item->>'result' = ANY(:observed)"
    ") observed WHERE n <= :per_key"
).bindparams(_texts("keys"), _texts("observed"))

# Which of some versions a ``conflicts_with`` relation touches (ids only).
_CONFLICTS = text(
    "SELECT from_version_id AS a, to_version_id AS b FROM memory_relations"
    " WHERE relation_type = 'conflicts_with'"
    " AND (from_version_id = ANY(:ids) OR to_version_id = ANY(:ids))"
).bindparams(bindparam("ids", type_=ARRAY(sqltypes.Uuid())))

# Answer every unanswered held item of a key (``:memory`` for a confirmation).
_RESOLVE = text(
    "INSERT INTO memory_preference_resolutions"
    " (entry_id, item_index, owner_user_id, resolution, memory_id, resolved_at)"
    " SELECT e.id, CAST(item->>'index' AS integer), e.owner_user_id, :resolution,"
    "  CAST(:memory AS uuid), :now"
    " FROM memory_journal_entries e"
    " CROSS JOIN LATERAL jsonb_array_elements(e.outcome->'items') AS item"
    " WHERE e.owner_user_id = :owner AND e.state = 'consolidated'"
    "  AND item->>'key' = :key AND item->>'result' = ANY(:held)"
    " ON CONFLICT (entry_id, item_index) DO NOTHING"
).bindparams(_texts("held"))

_HELD_VALUES = [r.value for r in HELD_RESULTS]
_OBSERVED_VALUES = [r.value for r in OBSERVED_RESULTS]


@dataclass(frozen=True, slots=True)
class _Held:
    """The latest unanswered held item of a key (see ``_LIVE_HELD``)."""

    key: str
    entry_id: UUID
    item_index: int
    result: ItemResult
    content: str
    conversation_id: UUID
    message_id: UUID
    event_sequence: int
    recorded_at: datetime

    @property
    def order(self) -> OrderKey:
        return OrderKey(self.conversation_id, self.event_sequence, self.recorded_at)


@dataclass(frozen=True, slots=True)
class _Audience:
    """Where a version is (its scope columns), no content."""

    scope: MemoryScope
    owner_user_id: UUID | None = None
    project_id: UUID | None = None
    repo_id: UUID | None = None
    # The version's status, where it was read (``_latest_audiences``).
    status: MemoryStatus | None = None

    @classmethod
    def of(cls, version: Any) -> "_Audience":
        return cls(
            MemoryScope(version.scope),
            version.owner_user_id,
            version.project_id,
            version.repo_id,
            MemoryStatus(version.status),
        )

    def columns(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "owner_user_id": self.owner_user_id,
            "project_id": self.project_id,
            "project_group_id": None,
            "repo_id": self.repo_id,
        }


def _own(version: Any, user_id: UUID) -> bool:
    """Is ``version`` the person's own private one?"""
    return version.scope == MemoryScope.USER and version.owner_user_id == user_id


class PreferenceConfirmationService(MemoryVersioningService):
    """The confirmation flow of Inferred Preferences (see the module docstring)."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        *,
        interpreter: PreferenceInterpreter | None = None,
        clock: Clock = _utc_now,
        lock_timeout_ms: int = version_limits.DEFAULT_LOCK_TIMEOUT_MS,
        interpreter_timeout_seconds: float = limits.INTERPRETER_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            database, authorizer, clock=clock, lock_timeout_ms=lock_timeout_ms
        )
        if interpreter is not None:
            try:
                check_interpreter(interpreter)
            except TypeError:
                raise reject("interpreter", InputProblem.WRONG_TYPE) from None
        if isinstance(interpreter_timeout_seconds, bool) or not isinstance(
            interpreter_timeout_seconds, int | float
        ):
            raise reject("interpreter_timeout_seconds", InputProblem.WRONG_TYPE)
        if not 0 < interpreter_timeout_seconds <= 120:
            raise reject("interpreter_timeout_seconds", InputProblem.OUT_OF_RANGE)
        self._interpreter = interpreter
        self._interpreter_timeout = float(interpreter_timeout_seconds)
        self._rules = RuleInterpreter()

    # -- authorization ----------------------------------------------------------------

    async def _authorize_own(self, actor: Principal, capability: Capability) -> None:
        decision = await self._decide(
            actor, capability, Resource.owned_by(actor.user_id, RESOURCE_MEMORY)
        )
        if not decision.allowed:
            raise MemoryPermissionError(decision.reason.value)

    async def _authorize_target(
        self,
        session: AsyncSession,
        actor: Principal,
        target: TargetScope,
        project_id: UUID | None,
        repo_id: UUID | None,
    ) -> _Audience:
        """May the person write a memory there? The audience to write, if so.

        ``user``: ``memory.use`` on their own memory. ``project``:
        ``project.memory.use`` on the project, with the role and state read from the
        database (Contributor or above of an active project). ``repo``: the same
        capability on the repository with its stored ACL (Decision 0081 point 7: an
        override that removes ``read`` refuses it). A project or repository the
        person is not a member of is ``not_project_member``, whether it exists or
        not.
        """
        if target is TargetScope.USER:
            await self._authorize_own(actor, Capability.MEMORY_USE)
            return _Audience(MemoryScope.USER, owner_user_id=actor.user_id)
        if project_id is None:
            raise reject("project_id", InputProblem.REQUIRED)
        resource_acl: RepoAcl | None = None
        if target is TargetScope.REPO:
            if repo_id is None:
                raise reject("repo_id", InputProblem.REQUIRED)
            row = (
                await session.execute(
                    select(_REPOSITORIES.c.project_id, _REPOSITORIES.c.acl_allowed)
                    .where(_REPOSITORIES.c.id == repo_id)
                    .with_for_update(read=True)
                )
            ).first()
            if row is None or row.project_id != project_id:
                raise MemoryPermissionError(Reason.NOT_PROJECT_MEMBER.value)
            resource_acl = (
                RepoAcl.inherit(repo_id, project_id)
                if row.acl_allowed is None
                else RepoAcl.override(
                    repo_id,
                    project_id,
                    (RepoPermission(name) for name in row.acl_allowed),
                )
            )
        member = await self._project_member(session, actor, project_id, lock=True)
        if member is None:
            raise MemoryPermissionError(Reason.NOT_PROJECT_MEMBER.value)
        principal, state = member
        resource = (
            Resource.project(project_id, state)
            if resource_acl is None
            else Resource.repository(project_id, state, resource_acl)
        )
        decision = await self._decide(
            principal, Capability.PROJECT_MEMORY_USE, resource
        )
        if not decision.allowed:
            raise MemoryPermissionError(decision.reason.value)
        if target is TargetScope.REPO:
            return _Audience(MemoryScope.REPO, repo_id=repo_id)
        return _Audience(MemoryScope.PROJECT, project_id=project_id)

    async def _own_or_target(
        self, session: AsyncSession, actor: Principal, confirmation: Confirmation
    ) -> _Audience:
        """The audience of a candidate that is the person's own private memory.

        Its own scope was authorized (``memory.use``, audited) when the current
        version was read for writing, so keeping it there is not decided twice.
        """
        if confirmation.target is TargetScope.USER:
            return _Audience(MemoryScope.USER, owner_user_id=actor.user_id)
        return await self._authorize_target(
            session,
            actor,
            confirmation.target,
            confirmation.target_project_id,
            confirmation.target_repo_id,
        )

    # -- reads ------------------------------------------------------------------------

    @staticmethod
    async def _live_held(
        session: AsyncSession, owner: UUID, key: str | None = None
    ) -> list[_Held]:
        rows = await session.execute(
            _LIVE_HELD,
            {
                "owner": owner,
                "held": _HELD_VALUES,
                "written": list(_WRITTEN),
                "key": key,
                # One key (a confirmation's check): every item, so that the latest
                # by the journal's order is never cut off (Codex P2 on #205).
                # ``LIMIT NULL`` is no limit.
                "limit": None if key is not None else limits.MAX_HELD_ITEMS,
            },
        )
        latest: dict[str, _Held] = {}
        for held in (
            _Held(
                key=row.key,
                entry_id=row.entry_id,
                item_index=row.item_index,
                result=ItemResult(row.result),
                content=row.content,
                conversation_id=row.conversation_id,
                message_id=row.message_id,
                event_sequence=row.event_sequence,
                recorded_at=row.recorded_at,
            )
            for row in rows
        ):
            best = latest.get(held.key)
            if best is None or is_newer(held.order, best.order):
                latest[held.key] = held
        found = sorted(latest.values(), key=lambda h: h.recorded_at, reverse=True)
        return found[: limits.MAX_CANDIDATES]

    @staticmethod
    async def _observations(
        session: AsyncSession, owner: UUID, keys: set[str]
    ) -> dict[str, list[Observation]]:
        found: dict[str, list[Observation]] = {key: [] for key in keys}
        if not keys:
            return found
        rows = await session.execute(
            _OBSERVATIONS,
            {
                "owner": owner,
                "keys": sorted(keys),
                "observed": _OBSERVED_VALUES,
                "per_key": limits.MAX_OBSERVATIONS_PER_KEY,
            },
        )
        for row in rows:
            found[row.key].append(
                Observation(
                    entry_id=row.entry_id,
                    project_id=row.project_id,
                    repo_id=row.repo_id,
                    recorded_at=row.recorded_at,
                    result=ItemResult(row.result),
                    strength=language_strength(row.message),
                )
            )
        return found

    @staticmethod
    async def _registered(
        session: AsyncSession,
        owner: UUID,
        *,
        keys: set[str] | None = None,
        memory_id: UUID | None = None,
    ) -> dict[str, UUID]:
        """key -> memory of the person's registry rows (by keys, or by a memory)."""
        statement = select(_KEYS.c.key, _KEYS.c.memory_id).where(
            _KEYS.c.owner_user_id == owner
        )
        if keys is not None:
            if not keys:
                return {}
            statement = statement.where(_KEYS.c.key.in_(sorted(keys)))
        if memory_id is not None:
            statement = statement.where(_KEYS.c.memory_id == memory_id)
        return {row.key: row.memory_id for row in await session.execute(statement)}

    @staticmethod
    async def _latest_audiences(
        session: AsyncSession, memory_ids: set[UUID]
    ) -> dict[UUID, _Audience]:
        """The scope columns of each memory's latest version (no content): the
        ``_AUDIENCE_COLUMNS`` exception of the versioning service, for memories the
        person's own registry names."""
        if not memory_ids:
            return {}
        rows = await session.execute(
            select(
                _VERSIONS.c.memory_id,
                _VERSIONS.c.scope,
                _VERSIONS.c.owner_user_id,
                _VERSIONS.c.project_id,
                _VERSIONS.c.repo_id,
                _VERSIONS.c.status,
            )
            .where(_VERSIONS.c.memory_id.in_(sorted(memory_ids)))
            .distinct(_VERSIONS.c.memory_id)
            .order_by(_VERSIONS.c.memory_id, _VERSIONS.c.version_number.desc())
        )
        return {row.memory_id: _Audience.of(row) for row in rows}

    async def candidates(self, actor: Principal) -> tuple[PreferenceCandidate, ...]:
        """The person's candidates, newest observation first (see the module text)."""
        actor = self._check_actor(actor)
        owner = actor.user_id
        async with self._transaction() as session:
            await self._authorize_own(actor, Capability.MEMORY_READ)
            memory_rows = list(
                await session.execute(
                    select(
                        _KEYS.c.key,
                        _VERSIONS.c.id,
                        _VERSIONS.c.memory_id,
                        _VERSIONS.c.version_number,
                        _VERSIONS.c.title,
                        _VERSIONS.c.content,
                        _VERSIONS.c.confirmation_state,
                        _VERSIONS.c.created_at,
                    )
                    .select_from(
                        _KEYS.join(
                            _VERSIONS, _VERSIONS.c.memory_id == _KEYS.c.memory_id
                        )
                    )
                    .where(
                        _KEYS.c.owner_user_id == owner,
                        readable_memory_versions(AclPrincipal(owner), MemoryVersion),
                        _VERSIONS.c.scope == MemoryScope.USER.value,
                        _VERSIONS.c.owner_user_id == owner,
                        _VERSIONS.c.status == MemoryStatus.ACTIVE.value,
                        _VERSIONS.c.confirmation_state.in_(_UNCONFIRMED),
                    )
                    .order_by(_VERSIONS.c.created_at.desc())
                    .limit(limits.MAX_CANDIDATES)
                )
            )
            held = await self._live_held(session, owner)
            keys = {row.key for row in memory_rows} | {item.key for item in held}
            observed = await self._observations(session, owner, keys)
            conflicted: set[UUID] = set()
            if memory_rows:
                for row in await session.execute(
                    _CONFLICTS, {"ids": [row.id for row in memory_rows]}
                ):
                    conflicted.update((row.a, row.b))
            held_memories = await self._registered(
                session, owner, keys={item.key for item in held}
            )
            audiences = await self._latest_audiences(
                session, set(held_memories.values())
            )

        found: list[PreferenceCandidate] = []
        for row in memory_rows:
            observations = observed.get(row.key, [])
            facts = evidence(
                observations,
                texts=(row.key, row.content),
                conflicting=row.id in conflicted,
            )
            recommendation = recommend_scope(observations)
            found.append(
                PreferenceCandidate(
                    kind=CandidateKind.MEMORY,
                    key=row.key,
                    title=row.title,
                    content=row.content,
                    evidence=facts,
                    recommendation=recommendation,
                    options=options(observations, recommendation),
                    ready=is_ready(facts),
                    observed_at=facts.last_observed_at or row.created_at,
                    memory_id=row.memory_id,
                    version_number=row.version_number,
                    confirmation_state=ConfirmationState(row.confirmation_state),
                )
            )
        for item in held:
            observations = observed.get(item.key, [])
            facts = evidence(observations, texts=(item.key, item.content))
            recommendation = recommend_scope(observations)
            memory_id = held_memories.get(item.key)
            audience = audiences.get(memory_id) if memory_id else None
            offered = options(observations, recommendation)
            if audience is not None and not (
                audience.scope is MemoryScope.USER and audience.owner_user_id == owner
            ):
                offered = _options_for_widened(audience)
            if audience is not None and audience.status not in _ANSWERABLE:
                # Retired by another memory (``superseded`` / ``history``): only
                # [保存しない] is left (Codex P2 on #205).
                offered = ()
            found.append(
                PreferenceCandidate(
                    kind=CandidateKind.HELD,
                    key=item.key,
                    title=item.key,
                    content=item.content,
                    evidence=facts,
                    recommendation=recommendation,
                    options=offered,
                    ready=is_ready(facts),
                    observed_at=facts.last_observed_at or item.recorded_at,
                    memory_id=memory_id,
                    entry_id=item.entry_id,
                    item_index=item.item_index,
                    held_reason=item.result,
                )
            )
        found.sort(key=lambda c: c.observed_at, reverse=True)
        return tuple(found)

    # -- the free-text answer ---------------------------------------------------

    async def _subject(
        self, session: AsyncSession, actor: Principal, ref: CandidateRef
    ) -> tuple[str, str, list[Observation]]:
        """(key, content, observations) of a candidate the person may answer now."""
        owner = actor.user_id
        if isinstance(ref, MemoryCandidateRef):
            row = (
                await session.execute(
                    select(_KEYS.c.key, _VERSIONS.c.content)
                    .select_from(
                        _KEYS.join(
                            _VERSIONS, _VERSIONS.c.memory_id == _KEYS.c.memory_id
                        )
                    )
                    .where(
                        _KEYS.c.owner_user_id == owner,
                        _KEYS.c.memory_id == ref.memory_id,
                        readable_memory_versions(AclPrincipal(owner), MemoryVersion),
                        _VERSIONS.c.scope == MemoryScope.USER.value,
                        _VERSIONS.c.owner_user_id == owner,
                        _VERSIONS.c.version_number == ref.expected_version,
                        _VERSIONS.c.status == MemoryStatus.ACTIVE.value,
                        _VERSIONS.c.confirmation_state.in_(_UNCONFIRMED),
                    )
                )
            ).first()
            if row is None:
                raise PreferenceCandidateChangedError
            key, content = row.key, row.content
        else:
            held = await self._held_ref(session, owner, ref)
            key, content = held.key, held.content
        observed = await self._observations(session, owner, {key})
        return key, content, observed[key]

    async def interpret(
        self, actor: Principal, ref: CandidateRef, free_text: str
    ) -> PreferencePreview:
        """[その他...]: the structured preview of the person's free text.

        Nothing is written. The model interpreter (if configured) answers first;
        on any failure, timeout or answer outside the contract the rule interpreter
        answers instead (``interpreted_by`` says which). A repository or project the
        interpretation names is the one the candidate was observed in most; the
        risk is the higher of the model's and the backend's own.
        """
        actor = self._check_actor(actor)
        _check_ref(ref)
        text_ = validate_text("text", free_text, max_chars=limits.MAX_FREE_TEXT_CHARS)
        if not text_.strip():
            raise reject("text", InputProblem.BLANK)
        async with self._transaction() as session:
            await self._authorize_own(actor, Capability.MEMORY_READ)
            key, content, observations = await self._subject(session, actor, ref)
        recommendation = recommend_scope(observations)
        offered = options(observations, recommendation)
        preference: StructuredPreference | None = None
        model_risk = RiskLevel.LOW
        interpreted_by = Interpreter.RULES
        if self._interpreter is not None:
            try:
                raw = await asyncio.wait_for(
                    self._interpreter.interpret(text_, content),
                    timeout=self._interpreter_timeout,
                )
                preference, model_risk = parse_interpreter_output(raw)
                interpreted_by = Interpreter.MODEL
            except Exception as error:  # noqa: BLE001 - InterpreterOutputError too
                # The model is optional: its failure is the rule interpreter's turn.
                # Only the type is logged, never the text.
                logger.warning(
                    "preference interpreter failed: %s", type(error).__name__
                )
                preference = None
                model_risk = RiskLevel.LOW
        if preference is None:
            preference = self._rules.interpret_text(
                text_, content, recommendation.scope
            )
        preference = _with_ids(preference, offered)
        risk = preference.risk_level(key, content)
        if model_risk is RiskLevel.HIGH:
            risk = RiskLevel.HIGH
        return PreferencePreview(preference, risk, interpreted_by)

    # -- confirm / reject --------------------------------------------------------

    async def _held_ref(
        self, session: AsyncSession, owner: UUID, ref: HeldCandidateRef
    ) -> _Held:
        """The held item ``ref`` names, if it is still its key's live question."""
        row = (
            await session.execute(
                text(
                    "SELECT item->>'key' AS key FROM memory_journal_entries e"
                    " CROSS JOIN LATERAL jsonb_array_elements(e.outcome->'items')"
                    "  AS item"
                    " WHERE e.id = :entry AND e.owner_user_id = :owner"
                    "  AND e.state = 'consolidated'"
                    "  AND CAST(item->>'index' AS integer) = :index"
                ),
                {"entry": ref.entry_id, "owner": owner, "index": ref.item_index},
            )
        ).first()
        if row is None:
            raise PreferenceCandidateChangedError
        live = await self._live_held(session, owner, row.key)
        if not live or (live[0].entry_id, live[0].item_index) != (
            ref.entry_id,
            ref.item_index,
        ):
            raise PreferenceCandidateChangedError
        return live[0]

    @staticmethod
    def _check_risk(
        confirmation: Confirmation, held: bool, *texts: str | None
    ) -> RiskLevel:
        risky = held or is_high_risk(*texts)
        if confirmation.preference is not None:
            risky = risky or (
                confirmation.preference.risk_level(*texts) is RiskLevel.HIGH
            )
        risk = RiskLevel.HIGH if risky else RiskLevel.LOW
        if risk is RiskLevel.HIGH and not confirmation.acknowledge_high_risk:
            raise PreferenceHighRiskError
        return risk

    @staticmethod
    def _freshness(
        confirmation: Confirmation, scope: MemoryScope, now: datetime
    ) -> dict[str, Any]:
        preference = confirmation.preference
        if preference is not None and preference.expires_at is not None:
            spec = FreshnessSpec(
                FreshnessPolicy.EXPIRING, expires_at=preference.expires_at
            )
        else:
            # REQUIREMENTS.md "Freshness": a User Preference is permanent.
            spec = FreshnessSpec(FreshnessPolicy.PERMANENT)
        check_manual_freshness(spec, scope, now)
        return _freshness_columns(spec, now)

    @staticmethod
    def _attributes(
        key: str,
        confirmation: Confirmation,
        risk: RiskLevel,
        *,
        from_version: int | None,
        held: _Held | None = None,
    ) -> dict[str, Any]:
        preference = confirmation.preference
        return {
            "confirmed_from_version": from_version,
            "preference": {
                "key": key,
                "choice": "other" if preference is not None else "option",
                "target": confirmation.target.value,
                "risk_level": risk.value,
                "acknowledged_high_risk": confirmation.acknowledge_high_risk,
                "policy_effect": POLICY_EFFECT,
                "structured": None
                if preference is None
                else preference.as_attributes(),
                "held": None
                if held is None
                else {
                    "result": held.result.value,
                    "entry_id": str(held.entry_id),
                    "item_index": held.item_index,
                },
            },
        }

    @staticmethod
    async def _add_source(
        session: AsyncSession,
        version_id: UUID,
        held: _Held,
        actor: Principal,
        now: datetime,
    ) -> None:
        """The held item's own observation, and the person's confirmation."""
        await session.execute(
            insert(_SOURCES).values(
                memory_version_id=version_id,
                source_type=SourceType.CONVERSATION.value,
                conversation_id=held.conversation_id,
                message_id=held.message_id,
                created_at=now,
            )
        )
        await session.execute(
            insert(_SOURCES).values(
                memory_version_id=version_id,
                source_type=SourceType.USER_CONFIRMATION.value,
                source_ref=confirmation_source_ref(actor.user_id),
                created_at=now,
            )
        )

    async def _next_version(
        self,
        session: AsyncSession,
        actor: Principal,
        current: MemoryVersionView,
        audience: _Audience,
        *,
        content: str,
        values: dict[str, Any],
        now: datetime,
        reason: str,
    ) -> MemoryVersionView:
        """``current`` -> ``superseded``; ``n + 1`` at ``audience``; the relations."""
        await self._set_status(session, current, MemoryStatus.SUPERSEDED, actor)
        new = await self._insert_version(
            session,
            current,
            values={
                **audience.columns(),
                "content": content,
                "memory_type": MEMORY_TYPE,
                **values,
            },
            actor=actor,
            now=now,
        )
        await self._relate(
            session,
            new.version_id,
            current.version_id,
            RelationType.SUPERSEDES,
            reason,
            now,
        )
        if (
            new.confirmation_state is ConfirmationState.CONFIRMED
            and current.confirmation_state.value in _UNCONFIRMED
        ):
            await self._relate(
                session,
                new.version_id,
                current.version_id,
                RelationType.CONFIRMED_FROM,
                reason,
                now,
            )
        return new

    async def _lock_memory(self, session: AsyncSession, memory_id: UUID) -> None:
        await session.execute(_LOCK_SQL, {"key": memory_lock_key(memory_id)})

    async def confirm(
        self, actor: Principal, ref: CandidateRef, confirmation: Confirmation
    ) -> MemoryVersionView:
        """The person's choice, written as a confirmed memory version."""
        actor = self._check_actor(actor)
        _check_ref(ref)
        if not isinstance(confirmation, Confirmation):
            raise reject("confirmation", InputProblem.WRONG_TYPE)
        now = self._now()
        if isinstance(ref, MemoryCandidateRef):
            return await self._confirm_memory(actor, ref, confirmation, now)
        return await self._confirm_held(actor, ref, confirmation, now)

    async def _own_candidate(
        self,
        session: AsyncSession,
        actor: Principal,
        ref: MemoryCandidateRef,
    ) -> tuple[str, MemoryVersionView]:
        """Lock and read the person's unconfirmed private candidate ``ref``."""
        registered = await self._registered(
            session, actor.user_id, memory_id=ref.memory_id
        )
        if len(registered) != 1:
            raise PreferenceCandidateChangedError
        (key,) = registered
        await lock_keys(session, actor.user_id, [key])
        await self._lock_memory(session, ref.memory_id)
        current = await self._current(session, actor, ref.memory_id, write=True)
        self._check_expected(current, ref.expected_version)
        if not (
            _own(current, actor.user_id)
            and current.status is MemoryStatus.ACTIVE
            and current.confirmation_state.value in _UNCONFIRMED
        ):
            raise PreferenceCandidateChangedError
        return key, current

    async def _confirm_memory(
        self,
        actor: Principal,
        ref: MemoryCandidateRef,
        confirmation: Confirmation,
        now: datetime,
    ) -> MemoryVersionView:
        async with self._transaction(
            race=(ref.memory_id, ref.expected_version)
        ) as session:
            key, current = await self._own_candidate(session, actor, ref)
            risk = self._check_risk(confirmation, False, key, current.content)
            audience = await self._own_or_target(session, actor, confirmation)
            content = (
                confirmation.preference.content()
                if confirmation.preference is not None
                else current.content
            )
            new = await self._next_version(
                session,
                actor,
                current,
                audience,
                content=content,
                values={
                    **self._freshness(confirmation, audience.scope, now),
                    "change_reason": confirmation.reason or CONFIRM_REASON,
                    "attributes": self._attributes(
                        key, confirmation, risk, from_version=current.version_number
                    ),
                },
                now=now,
                reason=CONFIRM_REASON,
            )
            await self._carry_sources(session, current, new, actor, now)
            return new

    async def _confirm_held(
        self,
        actor: Principal,
        ref: HeldCandidateRef,
        confirmation: Confirmation,
        now: datetime,
    ) -> MemoryVersionView:
        owner = actor.user_id
        async with self._transaction() as session:
            held = await self._held_ref(session, owner, ref)
            await lock_keys(session, owner, [held.key])
            # Read again under the key's lock: another answer may have come first.
            held = await self._held_ref(session, owner, ref)
            risk = self._check_risk(
                confirmation,
                held.result is ItemResult.HELD_HIGH_RISK,
                held.key,
                held.content,
            )
            content = (
                confirmation.preference.content()
                if confirmation.preference is not None
                else held.content
            )
            registered = await self._registered(session, owner, keys={held.key})
            memory_id = registered.get(held.key)
            if memory_id is not None:
                new = await self._confirm_held_on_memory(
                    session, actor, held, memory_id, confirmation, risk, content, now
                )
            else:
                new = await self._confirm_held_as_new(
                    session, actor, held, confirmation, risk, content, now
                )
            await self._resolve(
                session, owner, held.key, Resolution.CONFIRMED, new, now
            )
            return new

    async def _confirm_held_on_memory(
        self,
        session: AsyncSession,
        actor: Principal,
        held: _Held,
        memory_id: UUID,
        confirmation: Confirmation,
        risk: RiskLevel,
        content: str,
        now: datetime,
    ) -> MemoryVersionView:
        """The held candidate as the next version of its key's memory."""
        await self._lock_memory(session, memory_id)
        current = await self._current(session, actor, memory_id, write=True)
        # A deprecated memory (the person rejected or retired it) can be written
        # again by the person's explicit answer, as a restore would (Codex P2 on
        # #205); one retired by another memory cannot.
        if current.status not in _ANSWERABLE:
            raise MemoryStateError(StateProblem.NOT_ACTIVE)
        if _own(current, actor.user_id):
            audience = await self._own_or_target(session, actor, confirmation)
        else:
            # The person widened this memory before: the candidate stays there, or
            # is narrowed to their own memory. It is never moved to another audience.
            target = confirmation.target
            same = (
                current.scope is MemoryScope.PROJECT
                and target is TargetScope.PROJECT
                and confirmation.target_project_id == current.project_id
            )
            if not (same or target is TargetScope.USER):
                raise reject("scope", InputProblem.NOT_ALLOWED)
            audience = (
                _Audience(MemoryScope.PROJECT, project_id=current.project_id)
                if same
                else await self._authorize_target(
                    session, actor, TargetScope.USER, None, None
                )
            )
        new = await self._next_version(
            session,
            actor,
            current,
            audience,
            content=content,
            values={
                **self._freshness(confirmation, audience.scope, now),
                "change_reason": confirmation.reason or CONFIRM_REASON,
                "attributes": self._attributes(
                    held.key,
                    confirmation,
                    risk,
                    from_version=current.version_number,
                    held=held,
                ),
            },
            now=now,
            reason=CONFIRM_REASON,
        )
        await self._add_source(session, new.version_id, held, actor, now)
        # The registry's order guard moves to the held observation if it is newer,
        # so that an older observation finishing later cannot replace the answer.
        guard = (
            await session.execute(
                select(
                    _KEYS.c.applied_conversation_id,
                    _KEYS.c.applied_event_sequence,
                    _KEYS.c.applied_recorded_at,
                ).where(_KEYS.c.owner_user_id == actor.user_id, _KEYS.c.key == held.key)
            )
        ).one()
        if is_newer(held.order, OrderKey(*guard)):
            await session.execute(
                update(_KEYS)
                .where(_KEYS.c.owner_user_id == actor.user_id, _KEYS.c.key == held.key)
                .values(
                    applied_conversation_id=held.conversation_id,
                    applied_event_sequence=held.event_sequence,
                    applied_recorded_at=held.recorded_at,
                )
            )
        return new

    async def _confirm_held_as_new(
        self,
        session: AsyncSession,
        actor: Principal,
        held: _Held,
        confirmation: Confirmation,
        risk: RiskLevel,
        content: str,
        now: datetime,
    ) -> MemoryVersionView:
        """The held candidate as a new memory, registered for its key."""
        audience = await self._authorize_target(
            session,
            actor,
            confirmation.target,
            confirmation.target_project_id,
            confirmation.target_repo_id,
        )
        memory_id = (
            await session.execute(
                insert(_MEMORIES).values(created_at=now).returning(_MEMORIES.c.id)
            )
        ).scalar_one()
        inserted = (
            await session.execute(
                insert(_VERSIONS)
                .values(
                    memory_id=memory_id,
                    version_number=1,
                    **audience.columns(),
                    memory_type=MEMORY_TYPE,
                    title=held.key[: version_limits.MAX_TITLE_CHARS],
                    content=content,
                    status=MemoryStatus.ACTIVE.value,
                    confirmation_state=ConfirmationState.CONFIRMED.value,
                    attributes=self._attributes(
                        held.key, confirmation, risk, from_version=None, held=held
                    ),
                    actor_type=ActorType.USER.value,
                    actor_user_id=actor.user_id,
                    change_reason=confirmation.reason or CONFIRM_REASON,
                    created_at=now,
                    **self._freshness(confirmation, audience.scope, now),
                )
                .returning(*_VERSIONS.c)
            )
        ).first()
        assert inserted is not None
        new = version_view(inserted)
        await self._add_source(session, new.version_id, held, actor, now)
        await session.execute(
            insert(_KEYS).values(
                owner_user_id=actor.user_id,
                key=held.key,
                memory_id=memory_id,
                applied_conversation_id=held.conversation_id,
                applied_event_sequence=held.event_sequence,
                applied_recorded_at=held.recorded_at,
            )
        )
        return new

    @staticmethod
    async def _resolve(
        session: AsyncSession,
        owner: UUID,
        key: str,
        resolution: Resolution,
        written: MemoryVersionView | None,
        now: datetime,
    ) -> None:
        await session.execute(
            _RESOLVE,
            {
                "owner": owner,
                "key": key,
                "held": _HELD_VALUES,
                "resolution": resolution.value,
                "memory": None if written is None else written.memory_id,
                "now": now,
            },
        )

    async def reject_candidate(
        self, actor: Principal, ref: CandidateRef, *, reason: str | None = None
    ) -> MemoryVersionView | None:
        """[保存しない]. A memory candidate: version ``n + 1``, ``rejected`` and
        ``deprecated`` (returned). A held candidate: answered (``None``)."""
        actor = self._check_actor(actor)
        _check_ref(ref)
        if reason is not None:
            reason = validate_text(
                "reason", reason, max_chars=version_limits.MAX_REASON_CHARS
            )
        now = self._now()
        owner = actor.user_id
        if isinstance(ref, HeldCandidateRef):
            async with self._transaction() as session:
                held = await self._held_ref(session, owner, ref)
                await lock_keys(session, owner, [held.key])
                held = await self._held_ref(session, owner, ref)
                await self._authorize_own(actor, Capability.MEMORY_USE)
                await self._resolve(
                    session, owner, held.key, Resolution.REJECTED, None, now
                )
                return None
        async with self._transaction(
            race=(ref.memory_id, ref.expected_version)
        ) as session:
            key, current = await self._own_candidate(session, actor, ref)
            new = await self._next_version(
                session,
                actor,
                current,
                _Audience(MemoryScope.USER, owner_user_id=owner),
                content=current.content,
                values={
                    "memory_type": current.memory_type,
                    "status": MemoryStatus.DEPRECATED.value,
                    "confirmation_state": ConfirmationState.REJECTED.value,
                    "change_reason": reason or REJECT_REASON,
                    "attributes": {
                        "rejected_from_version": current.version_number,
                        "preference": {"key": key, "policy_effect": POLICY_EFFECT},
                    },
                    "freshness_policy": current.freshness_policy.value,
                    "verified_at": current.verified_at,
                    "revalidate_after": current.revalidate_after,
                    "revalidate_triggers": list(current.revalidate_triggers),
                    "expires_at": current.expires_at,
                    "commit_sha": current.commit_sha,
                    "branch": current.branch,
                },
                now=now,
                reason=REJECT_REASON,
            )
            # The same content, so the same sources: the deletion flow finds the
            # rejected copy through them (Decision 0045; Codex P1 on #205). No
            # ``user_confirmation``: the person did not confirm it.
            await session.execute(
                insert(_SOURCES).from_select(
                    ["memory_version_id", *(c.name for c in _COPIED_SOURCE_COLUMNS)],
                    select(
                        literal(new.version_id, _SOURCES.c.memory_version_id.type),
                        *_COPIED_SOURCE_COLUMNS,
                    ).where(
                        _SOURCES.c.memory_version_id == current.version_id,
                        ~(
                            (_SOURCES.c.source_type == SourceType.CONVERSATION.value)
                            & _SOURCES.c.conversation_id.is_(None)
                            & _SOURCES.c.message_id.is_(None)
                        ),
                    ),
                )
            )
            return new


def _check_ref(ref: object) -> None:
    if not isinstance(ref, MemoryCandidateRef | HeldCandidateRef):
        raise reject("candidate", InputProblem.WRONG_TYPE)


def _options_for_widened(audience: _Audience) -> tuple[Option, ...]:
    """A memory the person widened before: stay there, or narrow to their own."""
    if audience.scope is MemoryScope.PROJECT:
        return (
            Option(TargetScope.PROJECT, audience.project_id, recommended=True),
            Option(TargetScope.USER),
        )
    # A repository memory is not changed through this flow (Decision 0034 point 4):
    # only [保存しない] is left.
    return ()


def _with_ids(
    preference: StructuredPreference, offered: tuple[Option, ...]
) -> StructuredPreference:
    """The project / repository of the matching button, when there is one."""
    for option in offered:
        if option.scope.value == preference.scope.value:
            return replace(
                preference, project_id=option.project_id, repo_id=option.repo_id
            )
    return preference


__all__ = [
    "MEMORY_TYPE",
    "POLICY_EFFECT",
    "PreferenceConfirmationService",
]
