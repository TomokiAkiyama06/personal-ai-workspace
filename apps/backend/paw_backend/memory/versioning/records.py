"""Value objects of Memory versioning and freshness (PAW-042). No database, no I/O.

Every record validates itself in ``__post_init__`` (``validation.py``), so a
service method only ever sees well-formed input.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from paw_backend.memory.models import (
    ActorType,
    ConfirmationState,
    FreshnessPolicy,
    MemoryScope,
    MemoryStatus,
    RelationType,
)
from paw_backend.memory.versioning import limits
from paw_backend.memory.versioning.errors import InputProblem
from paw_backend.memory.versioning.validation import (
    reject,
    validate_aware_datetime,
    validate_commit_sha,
    validate_enum,
    validate_int,
    validate_memory_type,
    validate_optional_text,
    validate_text,
    validate_timedelta,
    validate_uuid,
)


class RevalidateTrigger(StrEnum):
    """Events that make a ``revalidate`` memory a stale candidate (Decision 0034).

    The requirements name the examples: a related setting, the members of a
    project, the model in use (for example the primary Local LLM), an external
    service, the development phase. The list is closed so that an event and the
    memories that wait for it always spell the trigger the same way.
    """

    RELATED_SETTING_CHANGED = "related_setting_changed"
    MEMBER_CHANGED = "member_changed"
    MODEL_CHANGED = "model_changed"
    EXTERNAL_SERVICE_CHANGED = "external_service_changed"
    PHASE_CHANGED = "phase_changed"


class ManualRelation(StrEnum):
    """The relations a person may record between two memories (Decision 0034).

    ``confirmed_from`` and ``revalidated_from`` are written by the operations that
    make them (an edit of an unconfirmed version, a revalidation); ``merged_from``
    belongs to the merge flow, which is not part of this issue.
    """

    SUPERSEDES = "supersedes"
    EXTENDS = "extends"
    CONFLICTS_WITH = "conflicts_with"

    @property
    def relation_type(self) -> RelationType:
        return RelationType(self.value)


class RelationClassification(StrEnum):
    """How a new memory relates to an existing one (REQUIREMENTS.md: an LLM may say)."""

    SAME = "same"
    EXTENDS = "extends"
    SUPERSEDES = "supersedes"
    CONFLICTS = "conflicts"
    UNRELATED = "unrelated"


@dataclass(frozen=True, slots=True)
class RelationPlan:
    """What a classification means for the two memories (``rules.plan_relation``).

    * ``writes_new``: the newer statement is stored as a memory at all.
    * ``relation``: the edge from the newer to the older version, if any.
    * ``retires_older``: the older version becomes ``superseded``.
    * ``needs_confirmation``: the person decides (an ambiguous conflict). Nothing
      is retired, both stay ``active``, and retrieval shows them as a conflict.
    """

    writes_new: bool
    relation: RelationType | None
    retires_older: bool
    needs_confirmation: bool


@dataclass(frozen=True, slots=True)
class FreshnessSpec:
    """The freshness policy of a version and the fields that policy needs.

    Only the fields of ``policy`` may be given (``NOT_ALLOWED`` otherwise):

    * ``permanent``: none.
    * ``revalidate``: ``revalidate_after`` (1 hour to 10 years) and optionally
      ``revalidate_triggers``. ``verified_at`` is never given: the service stamps
      the time it writes the version (a person verified it then).
    * ``expiring``: ``expires_at`` (aware); the service checks it lies ahead.
    * ``repo_commit``: ``commit_sha`` (40 or 64 lower-case hex digits) and
      optionally ``branch``.
    * ``session_only``: none.

    ``revalidate_triggers`` is normalised to a sorted tuple without duplicates.
    """

    policy: FreshnessPolicy
    revalidate_after: timedelta | None = None
    revalidate_triggers: tuple[RevalidateTrigger, ...] = ()
    expires_at: datetime | None = None
    commit_sha: str | None = None
    branch: str | None = None

    def __post_init__(self) -> None:
        policy = validate_enum("policy", self.policy, FreshnessPolicy)
        triggers = self.revalidate_triggers
        if not isinstance(triggers, list | tuple | set | frozenset):
            raise reject("revalidate_triggers", InputProblem.WRONG_TYPE)
        if len(triggers) > len(RevalidateTrigger):
            raise reject("revalidate_triggers", InputProblem.TOO_MANY)
        checked = [
            validate_enum("revalidate_triggers", trigger, RevalidateTrigger)
            for trigger in triggers
        ]
        object.__setattr__(
            self, "revalidate_triggers", tuple(sorted(set(checked), key=str))
        )
        allowed = {
            FreshnessPolicy.PERMANENT: set(),
            FreshnessPolicy.REVALIDATE: {"revalidate_after", "revalidate_triggers"},
            FreshnessPolicy.EXPIRING: {"expires_at"},
            FreshnessPolicy.REPO_COMMIT: {"commit_sha", "branch"},
            FreshnessPolicy.SESSION_ONLY: set(),
        }[policy]
        given = {
            name
            for name in (
                "revalidate_after",
                "expires_at",
                "commit_sha",
                "branch",
            )
            if getattr(self, name) is not None
        }
        if self.revalidate_triggers:
            given.add("revalidate_triggers")
        for name in sorted(given - allowed):
            raise reject(name, InputProblem.NOT_ALLOWED)
        if policy is FreshnessPolicy.REVALIDATE:
            validate_timedelta(
                "revalidate_after",
                self.revalidate_after,
                low=limits.MIN_REVALIDATE_AFTER,
                high=limits.MAX_REVALIDATE_AFTER,
            )
        if policy is FreshnessPolicy.EXPIRING:
            validate_aware_datetime("expires_at", self.expires_at)
        if policy is FreshnessPolicy.REPO_COMMIT:
            validate_commit_sha("commit_sha", self.commit_sha)
            validate_optional_text(
                "branch", self.branch, max_chars=limits.MAX_BRANCH_CHARS
            )

    @classmethod
    def permanent(cls) -> "FreshnessSpec":
        return cls(FreshnessPolicy.PERMANENT)

    @classmethod
    def revalidate(
        cls,
        after: timedelta,
        triggers: tuple[RevalidateTrigger, ...] = (),
    ) -> "FreshnessSpec":
        return cls(
            FreshnessPolicy.REVALIDATE,
            revalidate_after=after,
            revalidate_triggers=triggers,
        )

    @classmethod
    def expiring(cls, expires_at: datetime) -> "FreshnessSpec":
        return cls(FreshnessPolicy.EXPIRING, expires_at=expires_at)

    @classmethod
    def repo_commit(cls, commit_sha: str, branch: str | None = None) -> "FreshnessSpec":
        return cls(FreshnessPolicy.REPO_COMMIT, commit_sha=commit_sha, branch=branch)


PERMANENT = FreshnessSpec(FreshnessPolicy.PERMANENT)

# Scopes a person creates and edits through this service (Decision 0034).
EDITABLE_SCOPES = frozenset({MemoryScope.USER, MemoryScope.PROJECT})


@dataclass(frozen=True, slots=True)
class MemoryDraft:
    """A new memory written by a person (version 1, ``confirmed``).

    ``scope`` is ``user`` (the actor's own; ``project_id`` must be ``None``) or
    ``project`` (``project_id`` required). Other scopes are ``NOT_ALLOWED``.
    """

    scope: MemoryScope
    memory_type: str
    title: str
    content: str
    importance: int = limits.DEFAULT_IMPORTANCE
    freshness: FreshnessSpec = PERMANENT
    project_id: UUID | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        scope = validate_enum("scope", self.scope, MemoryScope)
        if scope not in EDITABLE_SCOPES:
            raise reject("scope", InputProblem.NOT_ALLOWED)
        if scope is MemoryScope.PROJECT:
            validate_uuid("project_id", self.project_id)
        elif self.project_id is not None:
            raise reject("project_id", InputProblem.NOT_ALLOWED)
        validate_memory_type("memory_type", self.memory_type)
        validate_text("title", self.title, max_chars=limits.MAX_TITLE_CHARS)
        validate_text("content", self.content, max_chars=limits.MAX_CONTENT_CHARS)
        validate_int("importance", self.importance, low=0, high=100)
        if not isinstance(self.freshness, FreshnessSpec):
            raise reject("freshness", InputProblem.WRONG_TYPE)
        validate_optional_text("reason", self.reason, max_chars=limits.MAX_REASON_CHARS)


@dataclass(frozen=True, slots=True)
class MemoryChanges:
    """A manual edit: the fields to change. ``None`` leaves a field as it is.

    At least one of ``title``, ``content``, ``memory_type``, ``importance`` and
    ``freshness`` must be given (``reason`` alone is not a change). The scope is not
    editable here: widening it needs the confirmation flow (REQUIREMENTS.md
    "Scope変更"), and a narrower audience is a new memory.
    """

    title: str | None = None
    content: str | None = None
    memory_type: str | None = None
    importance: int | None = None
    freshness: FreshnessSpec | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.title is not None:
            validate_text("title", self.title, max_chars=limits.MAX_TITLE_CHARS)
        if self.content is not None:
            validate_text("content", self.content, max_chars=limits.MAX_CONTENT_CHARS)
        if self.memory_type is not None:
            validate_memory_type("memory_type", self.memory_type)
        if self.importance is not None:
            validate_int("importance", self.importance, low=0, high=100)
        if self.freshness is not None and not isinstance(self.freshness, FreshnessSpec):
            raise reject("freshness", InputProblem.WRONG_TYPE)
        validate_optional_text("reason", self.reason, max_chars=limits.MAX_REASON_CHARS)
        if all(
            getattr(self, name) is None
            for name in ("title", "content", "memory_type", "importance", "freshness")
        ):
            raise reject("changes", InputProblem.REQUIRED)


class TargetKind(StrEnum):
    USER = "user"
    PROJECT = "project"
    REPO = "repo"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class TriggerTarget:
    """Which memories an event concerns (``FreshnessMaintenance.mark_triggered``).

    ``user``: the private memories of one user. ``project``: the memories of one
    project. ``repo``: the memories of one repository. ``workspace``: every memory
    (a workspace-wide change such as the primary Local LLM).
    """

    kind: TargetKind
    id: UUID | None = None

    def __post_init__(self) -> None:
        kind = validate_enum("kind", self.kind, TargetKind)
        if kind is TargetKind.WORKSPACE:
            if self.id is not None:
                raise reject("id", InputProblem.NOT_ALLOWED)
        else:
            validate_uuid("id", self.id)

    @classmethod
    def user(cls, user_id: UUID) -> "TriggerTarget":
        return cls(TargetKind.USER, user_id)

    @classmethod
    def project(cls, project_id: UUID) -> "TriggerTarget":
        return cls(TargetKind.PROJECT, project_id)

    @classmethod
    def repo(cls, repo_id: UUID) -> "TriggerTarget":
        return cls(TargetKind.REPO, repo_id)

    @classmethod
    def workspace(cls) -> "TriggerTarget":
        return cls(TargetKind.WORKSPACE)


@dataclass(frozen=True, slots=True)
class MemoryVersionView:
    """One stored version, as the service returns it."""

    memory_id: UUID
    version_id: UUID
    version_number: int
    scope: MemoryScope
    owner_user_id: UUID | None
    project_id: UUID | None
    project_group_id: UUID | None
    repo_id: UUID | None
    memory_type: str
    title: str
    content: str
    importance: int
    pinned: bool
    status: MemoryStatus
    confirmation_state: ConfirmationState
    freshness_policy: FreshnessPolicy
    verified_at: datetime | None
    revalidate_after: timedelta | None
    revalidate_triggers: tuple[str, ...]
    expires_at: datetime | None
    commit_sha: str | None
    branch: str | None
    stale_since: datetime | None
    actor_type: ActorType
    actor_user_id: UUID | None
    change_reason: str | None
    created_at: datetime

    @property
    def audience(
        self,
    ) -> tuple[str, UUID | None, UUID | None, UUID | None, UUID | None]:
        """Who can read the version: its scope and scope columns."""
        return (
            self.scope.value,
            self.owner_user_id,
            self.project_id,
            self.project_group_id,
            self.repo_id,
        )


@dataclass(frozen=True, slots=True)
class MemoryRelationView:
    """An edge of the history graph that a call wrote (newer to older)."""

    from_version_id: UUID
    to_version_id: UUID
    relation: RelationType
    reason: str | None
