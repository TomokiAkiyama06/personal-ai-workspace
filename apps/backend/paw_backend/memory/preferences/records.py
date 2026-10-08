"""Value objects of the Inferred Preference flow (PAW-044). No database, no I/O.

Every record that comes from a caller validates itself on construction.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from paw_backend.memory.journal.domain import ItemResult
from paw_backend.memory.models import ConfirmationState
from paw_backend.memory.preferences.interpretation import StructuredPreference
from paw_backend.memory.preferences.rules import (
    CandidateKind,
    Evidence,
    Option,
    Recommendation,
    TargetScope,
)
from paw_backend.memory.versioning import limits as version_limits
from paw_backend.memory.versioning.errors import InputProblem
from paw_backend.memory.versioning.validation import (
    reject,
    validate_enum,
    validate_int,
    validate_optional_text,
    validate_uuid,
    validate_version_number,
)

# ``outcome.items[*].index``: the place of an item in a worker output (PAW-041
# bounds the output to far fewer items; the column is a smallint).
MAX_ITEM_INDEX = 32_767


@dataclass(frozen=True, slots=True)
class MemoryCandidateRef:
    """An unconfirmed private memory, at the version the person saw."""

    memory_id: UUID
    expected_version: int

    def __post_init__(self) -> None:
        validate_uuid("memory_id", self.memory_id)
        validate_version_number("expected_version", self.expected_version)


@dataclass(frozen=True, slots=True)
class HeldCandidateRef:
    """A held candidate: the outcome item (entry, index) the person saw."""

    entry_id: UUID
    item_index: int

    def __post_init__(self) -> None:
        validate_uuid("entry_id", self.entry_id)
        validate_int("item_index", self.item_index, low=0, high=MAX_ITEM_INDEX)


type CandidateRef = MemoryCandidateRef | HeldCandidateRef


@dataclass(frozen=True, slots=True)
class PreferenceCandidate:
    """One question of the confirmation UI, with its evidence.

    ``kind`` = ``memory``: ``memory_id`` / ``version_number`` / ``confirmation_state``
    (``observed`` or ``inferred``) name the candidate version. ``kind`` = ``held``:
    ``entry_id`` / ``item_index`` name the latest unanswered held item of the key,
    ``held_reason`` why it was held, and ``memory_id`` the key's memory if it has
    one (the candidate would change it). ``content`` is the candidate's text, read
    from the owner's own rows only.
    """

    kind: CandidateKind
    key: str
    title: str
    content: str
    evidence: Evidence
    recommendation: Recommendation
    options: tuple[Option, ...]
    ready: bool
    observed_at: datetime
    memory_id: UUID | None = None
    version_number: int | None = None
    confirmation_state: ConfirmationState | None = None
    entry_id: UUID | None = None
    item_index: int | None = None
    held_reason: ItemResult | None = None


@dataclass(frozen=True, slots=True)
class Confirmation:
    """The person's answer: a button (``scope`` with its ids) or [その他...]'s
    structured preference (``preference``), never both.

    ``acknowledge_high_risk`` is the explicit "yes, apply this" a high-risk
    preference needs (:class:`~.errors.PreferenceHighRiskError` without it).
    """

    scope: TargetScope | None = None
    project_id: UUID | None = None
    repo_id: UUID | None = None
    preference: StructuredPreference | None = None
    acknowledge_high_risk: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.scope is None) == (self.preference is None):
            raise reject("scope", InputProblem.REQUIRED)
        if self.preference is not None:
            if not isinstance(self.preference, StructuredPreference):
                raise reject("preference", InputProblem.WRONG_TYPE)
            if self.project_id is not None:
                raise reject("project_id", InputProblem.NOT_ALLOWED)
            if self.repo_id is not None:
                raise reject("repo_id", InputProblem.NOT_ALLOWED)
        else:
            scope = validate_enum("scope", self.scope, TargetScope)
            needs_project = scope in (TargetScope.PROJECT, TargetScope.REPO)
            if needs_project != (self.project_id is not None):
                raise reject(
                    "project_id",
                    InputProblem.REQUIRED
                    if needs_project
                    else InputProblem.NOT_ALLOWED,
                )
            if (scope is TargetScope.REPO) != (self.repo_id is not None):
                raise reject(
                    "repo_id",
                    InputProblem.REQUIRED
                    if scope is TargetScope.REPO
                    else InputProblem.NOT_ALLOWED,
                )
            if self.project_id is not None:
                validate_uuid("project_id", self.project_id)
            if self.repo_id is not None:
                validate_uuid("repo_id", self.repo_id)
        if not isinstance(self.acknowledge_high_risk, bool):
            raise reject("acknowledge_high_risk", InputProblem.WRONG_TYPE)
        validate_optional_text(
            "reason", self.reason, max_chars=version_limits.MAX_REASON_CHARS
        )

    @property
    def target(self) -> TargetScope:
        if self.preference is not None:
            return self.preference.target
        assert self.scope is not None
        return self.scope

    @property
    def target_project_id(self) -> UUID | None:
        return self.preference.project_id if self.preference else self.project_id

    @property
    def target_repo_id(self) -> UUID | None:
        return self.preference.repo_id if self.preference else self.repo_id
