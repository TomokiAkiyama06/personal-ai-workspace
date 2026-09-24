"""Value objects of Shared Memory administration.

Input objects (:class:`SharedMemoryDraft`, :class:`SharedMemoryChanges`,
:class:`CandidateProposal`, :class:`AgentActor`) validate themselves when they
are built, with the functions of ``validation.py``; a value that is not valid
cannot be constructed. Result objects (:class:`SharedMemory`,
:class:`SharedMemoryCandidate`, :class:`EffectiveSharedMemory`, ...) are plain
records built by the service from database rows; they carry no behaviour.

Ids are :class:`uuid.UUID`. Nothing here talks to a database.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from paw_backend.authz import AgentGrant, Principal
from paw_backend.memory.shared import limits
from paw_backend.memory.shared.errors import InputProblem, InvalidSharedMemoryInputError
from paw_backend.memory.shared.validation import (
    normalize_subjects,
    validate_enum,
    validate_int,
    validate_memory_type,
    validate_optional_text,
    validate_optional_uuid,
    validate_policy_id,
    validate_subject,
    validate_text,
    validate_uuid,
)

DEFAULT_IMPORTANCE = 50
# The fields a version of a shared memory has that an edit can change, in the
# alphabetical order in which ``changed_fields`` reports them.
EDITABLE_FIELDS = ("content", "importance", "memory_type", "policy_subjects", "title")


class SharedMemoryStatus(StrEnum):
    """State of a shared memory, seen through its current version."""

    ACTIVE = "active"  # the current version has status ``active``
    DELETED = "deleted"  # the current version has status ``deprecated``


class CandidateState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CandidateAction(StrEnum):
    """A decision of an Owner or Admin on a pending candidate."""

    APPROVE = "approve"
    REJECT = "reject"


class OriginScope(StrEnum):
    """The scope of the memory a candidate was derived from (never ``shared``)."""

    USER = "user"
    PROJECT = "project"
    PROJECT_GROUP = "project_group"
    REPO = "repo"


@dataclass(frozen=True, slots=True)
class AgentActor:
    """An agent acting for the user ``delegator_id`` under ``grant``.

    An agent can read Shared Memory (when the grant lists ``shared_memory.read``
    and covers all projects) and propose candidates (``memory.use``). It can
    never manage Shared Memory: see ``AutomaticPromotionRefusedError``.
    """

    delegator_id: UUID
    grant: AgentGrant

    def __post_init__(self) -> None:
        validate_uuid("delegator_id", self.delegator_id)
        if not isinstance(self.grant, AgentGrant):
            raise InvalidSharedMemoryInputError("grant", InputProblem.WRONG_TYPE)


Actor = Principal | AgentActor


@dataclass(frozen=True, slots=True)
class SharedMemoryDraft:
    """The complete, validated field set of one version of a shared memory.

    Used to create a memory, as the result of applying an edit, and as the
    memory a candidate becomes when it is approved. ``policy_subjects`` is
    normalised (sorted, without duplicates, see ``normalize_subjects``);
    ``reason`` is the version's ``change_reason``.
    """

    memory_type: str
    title: str
    content: str
    importance: int = DEFAULT_IMPORTANCE
    policy_subjects: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        validate_memory_type("memory_type", self.memory_type)
        validate_text("title", self.title, max_chars=limits.MAX_TITLE_CHARS)
        validate_text("content", self.content, max_chars=limits.MAX_CONTENT_CHARS)
        validate_int("importance", self.importance, low=0, high=100)
        object.__setattr__(
            self,
            "policy_subjects",
            normalize_subjects("policy_subjects", self.policy_subjects),
        )
        validate_optional_text("reason", self.reason, max_chars=limits.MAX_REASON_CHARS)


@dataclass(frozen=True, slots=True)
class SharedMemoryChanges:
    """An edit: the fields to change. A field that is ``None`` is left as it is.

    ``policy_subjects=()`` (or ``[]``) clears the subjects; ``None`` keeps them.
    At least one of ``title``, ``content``, ``memory_type``, ``importance`` and
    ``policy_subjects`` must be given (``reason`` alone is not a change):
    otherwise ``InvalidSharedMemoryInputError("changes", REQUIRED)``.
    ``reason`` is stored as the new version's ``change_reason``.
    """

    title: str | None = None
    content: str | None = None
    memory_type: str | None = None
    importance: int | None = None
    policy_subjects: tuple[str, ...] | None = None
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
        if self.policy_subjects is not None:
            object.__setattr__(
                self,
                "policy_subjects",
                normalize_subjects("policy_subjects", self.policy_subjects),
            )
        validate_optional_text("reason", self.reason, max_chars=limits.MAX_REASON_CHARS)
        if (
            self.title is None
            and self.content is None
            and self.memory_type is None
            and self.importance is None
            and self.policy_subjects is None
        ):
            raise InvalidSharedMemoryInputError("changes", InputProblem.REQUIRED)


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    """A proposal to promote content to Shared Memory (a Shared Memory Candidate).

    ``origin_scope`` and ``origin_version_id`` say which memory the content was
    derived from. They are the proposer's claim: the service does not check them
    against the memory tables (it never reads a private memory).
    """

    memory_type: str
    title: str
    content: str
    origin_scope: OriginScope
    importance: int = DEFAULT_IMPORTANCE
    policy_subjects: tuple[str, ...] = ()
    origin_version_id: UUID | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        validate_memory_type("memory_type", self.memory_type)
        validate_text("title", self.title, max_chars=limits.MAX_TITLE_CHARS)
        validate_text("content", self.content, max_chars=limits.MAX_CONTENT_CHARS)
        validate_enum("origin_scope", self.origin_scope, OriginScope)
        validate_int("importance", self.importance, low=0, high=100)
        object.__setattr__(
            self,
            "policy_subjects",
            normalize_subjects("policy_subjects", self.policy_subjects),
        )
        validate_optional_uuid("origin_version_id", self.origin_version_id)
        validate_optional_text("reason", self.reason, max_chars=limits.MAX_REASON_CHARS)


@dataclass(frozen=True, slots=True)
class SharedMemory:
    """A shared memory as seen through its current (highest numbered) version.

    ``created_at`` is when the memory was first created, ``updated_at`` when the
    current version was written (a delete or a restore changes ``status`` but
    not ``updated_at``).
    """

    memory_id: UUID
    version_id: UUID
    version_number: int
    memory_type: str
    title: str
    content: str
    importance: int
    policy_subjects: tuple[str, ...]
    status: SharedMemoryStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SharedMemoryCandidate:
    """A proposed shared memory, with its proposer and its provenance.

    ``proposer_user_id`` is the user the proposal is for (the delegating user
    when an agent proposed); ``proposer_agent_id`` is the agent, or ``None`` for
    a proposal by the user. ``decided_by`` / ``decided_at`` /
    ``decision_reason`` are set once the candidate is approved or rejected;
    ``memory_id`` only when it was approved.
    """

    candidate_id: UUID
    state: CandidateState
    proposer_user_id: UUID
    proposer_agent_id: UUID | None
    origin_scope: OriginScope
    origin_version_id: UUID | None
    memory_type: str
    title: str
    content: str
    importance: int
    policy_subjects: tuple[str, ...]
    reason: str | None
    created_at: datetime
    decided_by: UUID | None
    decided_at: datetime | None
    decision_reason: str | None
    memory_id: UUID | None


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """The outcome of approving (``memory`` is the new shared memory) or rejecting."""

    candidate: SharedMemoryCandidate
    memory: SharedMemory | None


@dataclass(frozen=True, slots=True)
class EditPlan:
    """What an edit does: the complete new field set and the names that changed.

    ``changed_fields`` is a sorted tuple of names from ``EDITABLE_FIELDS``.
    """

    draft: SharedMemoryDraft
    changed_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SystemPolicyItem:
    """One item of the System Security Policy, as far as precedence needs it.

    ``policy_id`` identifies the item, ``subject`` says what it governs (a
    dotted subject, see ``validate_subject``) and ``statement`` is the rule text.
    The statement is opaque: it is carried, never interpreted, and never put in
    an error or a log line. The policy itself is defined and enforced elsewhere
    (Admin configuration, the tool broker); this type is only the reference the
    precedence rule works with.
    """

    policy_id: str
    subject: str
    statement: str

    def __post_init__(self) -> None:
        validate_policy_id("policy_id", self.policy_id)
        validate_subject(self.subject, "subject")
        validate_text(
            "statement", self.statement, max_chars=limits.MAX_POLICY_STATEMENT_CHARS
        )


@dataclass(frozen=True, slots=True)
class OverriddenMemory:
    """A shared memory that a System Security Policy item overrides.

    Only ids: the content of an overridden memory is not returned.
    ``policy_ids`` is sorted and without duplicates.
    """

    memory_id: UUID
    version_id: UUID
    policy_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EffectiveSharedMemory:
    """Shared Memory as it may be shown to a model, after the policy has won.

    ``memories`` are the active memories no policy overrides (input order);
    ``overridden`` names the ones a policy suppressed (input order);
    ``applied_policies`` are the policy items that suppressed at least one
    memory, sorted by ``policy_id``.
    """

    memories: tuple[SharedMemory, ...]
    overridden: tuple[OverriddenMemory, ...]
    applied_policies: tuple[SystemPolicyItem, ...]
