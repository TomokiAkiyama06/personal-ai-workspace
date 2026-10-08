"""The Inferred Preference confirmation flow over HTTP (issue #38, PAW-044).

``/api/v1/memory/preferences/*``. The service is
``paw_backend.memory.preferences.PreferenceConfirmationService``; this module only
translates. Decision 0081 records the choices.

* ``GET /candidates``: the person's candidates with their evidence, the recommended
  scope, the buttons and ``ready`` (ask now).
* ``POST /memories/{memory_id}/{confirm|reject|interpret}``: an unconfirmed private
  memory, at ``expected_version``.
* ``POST /held/{entry_id}/{item_index}/{confirm|reject|interpret}``: a candidate the
  consolidator held for the person (the latest unanswered item of its key).

``confirm`` takes a button (``scope`` = ``repo`` / ``project`` / ``user`` with the
ids it needs) or the [その他...] preview (``preference``, possibly corrected by the
person), and ``acknowledge_high_risk``: a high-risk preference without it answers
409 ``preference_high_risk_unacknowledged`` and writes nothing. ``reject`` is
[保存しない]. ``interpret`` turns free text into the structured preview and writes
nothing.

Every route needs a session (not restricted by the Passkey policy) whose person
holds ``memory.read`` on their own memory, as the Memory screen
(``api/v1/memory.py``). Only the person's own candidates exist for them: anything
else answers 404 ``memory_not_found`` or 409 ``preference_candidate_changed`` (the
candidate was answered or replaced; reload). Writing to a project or repository
needs ``project.memory.use`` there (403 ``forbidden``).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from paw_backend.api.v1.memory import (
    _READER,
    _VERSION,
    MemoryVersionOut,
    _database,
    _memory_errors,
    _named,
    _version,
)
from paw_backend.errors import ApiError
from paw_backend.memory.preferences import (
    CandidateRef,
    Confirmation,
    HeldCandidateRef,
    InterpretedScope,
    MemoryCandidateRef,
    PreferenceCandidate,
    PreferenceCandidateChangedError,
    PreferenceConfirmationService,
    PreferenceHighRiskError,
    PreferencePreview,
    Strength,
    StructuredPreference,
    TargetScope,
)
from paw_backend.memory.preferences import limits as preference_limits
from paw_backend.memory.preferences.records import MAX_ITEM_INDEX
from paw_backend.memory.versioning import limits as version_limits

router = APIRouter(prefix="/memory/preferences", tags=["memory"])


# -- answers ------------------------------------------------------------------------


class EvidenceOut(BaseModel):
    frequency: int
    project_count: int
    repo_count: int
    outside_projects: int
    last_observed_at: datetime | None
    language_strength: str
    consistency: str
    risk_level: str


class OptionOut(BaseModel):
    scope: str
    project_id: uuid.UUID | None
    repo_id: uuid.UUID | None
    recommended: bool


class RecommendationOut(BaseModel):
    scope: str
    project_id: uuid.UUID | None
    repo_id: uuid.UUID | None


class CandidateOut(BaseModel):
    kind: str
    key: str
    title: str
    content: str
    evidence: EvidenceOut
    recommendation: RecommendationOut
    options: list[OptionOut]
    ready: bool
    observed_at: datetime
    memory_id: uuid.UUID | None
    version_number: int | None
    confirmation_state: str | None
    entry_id: uuid.UUID | None
    item_index: int | None
    held_reason: str | None


class CandidatesOut(BaseModel):
    candidates: list[CandidateOut]


class PreferenceOut(BaseModel):
    scope: str
    project_id: uuid.UUID | None
    repo_id: uuid.UUID | None
    apply_to: str | None
    rule: str
    exceptions: list[str]
    strength: str
    expires_at: datetime | None


class PreviewOut(BaseModel):
    preference: PreferenceOut
    content: str
    risk_level: str
    requires_acknowledgement: bool
    interpreted_by: str


class RejectOut(BaseModel):
    # The rejected version of a memory candidate; null for a held candidate.
    version: MemoryVersionOut | None


# -- requests -----------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreferenceIn(_Body):
    """The [その他...] preview as the person confirms it."""

    scope: Literal["repo", "project", "user", "project_group"]
    project_id: uuid.UUID | None = None
    repo_id: uuid.UUID | None = None
    apply_to: StrictStr | None = Field(
        default=None, max_length=preference_limits.MAX_APPLY_TO_CHARS
    )
    rule: StrictStr = Field(min_length=1, max_length=preference_limits.MAX_RULE_CHARS)
    exceptions: list[
        Annotated[
            StrictStr,
            Field(min_length=1, max_length=preference_limits.MAX_EXCEPTION_CHARS),
        ]
    ] = Field(default_factory=list, max_length=preference_limits.MAX_EXCEPTIONS)
    strength: Literal["default", "required"] = "default"
    expires_at: datetime | None = None


_REASON = Field(default=None, max_length=version_limits.MAX_REASON_CHARS)


class ConfirmFields(_Body):
    scope: Literal["repo", "project", "user"] | None = None
    project_id: uuid.UUID | None = None
    repo_id: uuid.UUID | None = None
    preference: PreferenceIn | None = None
    acknowledge_high_risk: StrictBool = False
    reason: StrictStr | None = _REASON


class MemoryConfirmRequest(ConfirmFields):
    expected_version: StrictInt = _VERSION


class MemoryRejectRequest(_Body):
    expected_version: StrictInt = _VERSION
    reason: StrictStr | None = _REASON


class HeldRejectRequest(_Body):
    reason: StrictStr | None = _REASON


_TEXT = Field(min_length=1, max_length=preference_limits.MAX_FREE_TEXT_CHARS)


class MemoryInterpretRequest(_Body):
    expected_version: StrictInt = _VERSION
    text: StrictStr = _TEXT


class HeldInterpretRequest(_Body):
    text: StrictStr = _TEXT


_ITEM_INDEX = Path(ge=0, le=MAX_ITEM_INDEX)


# -- translation ----------------------------------------------------------------------


def _candidate(candidate: PreferenceCandidate) -> CandidateOut:
    facts = candidate.evidence
    recommendation = candidate.recommendation
    return CandidateOut(
        kind=candidate.kind.value,
        key=candidate.key,
        title=candidate.title,
        content=candidate.content,
        evidence=EvidenceOut(
            frequency=facts.frequency,
            project_count=facts.project_count,
            repo_count=facts.repo_count,
            outside_projects=facts.outside_projects,
            last_observed_at=facts.last_observed_at,
            language_strength=facts.language_strength.value,
            consistency=facts.consistency.value,
            risk_level=facts.risk_level.value,
        ),
        recommendation=RecommendationOut(
            scope=recommendation.scope.value,
            project_id=recommendation.project_id,
            repo_id=recommendation.repo_id,
        ),
        options=[
            OptionOut(
                scope=option.scope.value,
                project_id=option.project_id,
                repo_id=option.repo_id,
                recommended=option.recommended,
            )
            for option in candidate.options
        ],
        ready=candidate.ready,
        observed_at=candidate.observed_at,
        memory_id=candidate.memory_id,
        version_number=candidate.version_number,
        confirmation_state=None
        if candidate.confirmation_state is None
        else candidate.confirmation_state.value,
        entry_id=candidate.entry_id,
        item_index=candidate.item_index,
        held_reason=None
        if candidate.held_reason is None
        else candidate.held_reason.value,
    )


def _preview(preview: PreferencePreview) -> PreviewOut:
    preference = preview.preference
    return PreviewOut(
        preference=PreferenceOut(
            scope=preference.scope.value,
            project_id=preference.project_id,
            repo_id=preference.repo_id,
            apply_to=preference.apply_to,
            rule=preference.rule,
            exceptions=list(preference.exceptions),
            strength=preference.strength.value,
            expires_at=preference.expires_at,
        ),
        content=preview.content,
        risk_level=preview.risk_level.value,
        requires_acknowledgement=preview.requires_acknowledgement,
        interpreted_by=preview.interpreted_by.value,
    )


def _confirmation(body: ConfirmFields) -> Confirmation:
    preference = None
    if body.preference is not None:
        given = body.preference
        preference = StructuredPreference(
            scope=InterpretedScope(given.scope),
            rule=given.rule,
            exceptions=tuple(given.exceptions),
            apply_to=given.apply_to,
            strength=Strength(given.strength),
            expires_at=given.expires_at,
            project_id=given.project_id,
            repo_id=given.repo_id,
        )
    return Confirmation(
        scope=None if body.scope is None else TargetScope(body.scope),
        project_id=body.project_id,
        repo_id=body.repo_id,
        preference=preference,
        acknowledge_high_risk=body.acknowledge_high_risk,
        reason=body.reason,
    )


def _service(request: Request) -> PreferenceConfirmationService:
    # The application's Authorizer and (optional) model interpreter, read per
    # request (tests swap them).
    return PreferenceConfirmationService(
        _database(request),
        request.app.state.authorizer,
        interpreter=getattr(request.app.state, "preference_interpreter", None),
    )


@contextmanager
def _preference_errors() -> Iterator[None]:
    """The flow's own errors first, then the Memory errors."""
    try:
        with _memory_errors():
            yield
    except PreferenceHighRiskError:
        raise ApiError(
            409,
            "preference_high_risk_unacknowledged",
            "A high-risk preference must be acknowledged explicitly",
        ) from None
    except PreferenceCandidateChangedError:
        raise ApiError(
            409, "preference_candidate_changed", "The candidate changed; reload it"
        ) from None


async def _confirmed(
    request: Request, principal, ref: CandidateRef, body: ConfirmFields
) -> MemoryVersionOut:
    with _preference_errors():
        written = await _service(request).confirm(principal, ref, _confirmation(body))
    return _version(await _named(request, written))


# -- routes ---------------------------------------------------------------------------


@router.get(
    "/candidates",
    response_model=CandidatesOut,
    summary="The person's Inferred Preference candidates with their evidence",
)
async def preference_candidates(request: Request, principal: _READER) -> CandidatesOut:
    with _preference_errors():
        found = await _service(request).candidates(principal)
    return CandidatesOut(candidates=[_candidate(candidate) for candidate in found])


@router.post(
    "/memories/{memory_id}/confirm",
    response_model=MemoryVersionOut,
    summary="Confirm a memory candidate at the chosen scope",
)
async def confirm_memory_candidate(
    request: Request,
    principal: _READER,
    memory_id: uuid.UUID,
    body: MemoryConfirmRequest,
) -> MemoryVersionOut:
    with _preference_errors():
        ref = MemoryCandidateRef(memory_id, body.expected_version)
    return await _confirmed(request, principal, ref, body)


@router.post(
    "/memories/{memory_id}/reject",
    response_model=RejectOut,
    summary="Do not keep a memory candidate (保存しない)",
)
async def reject_memory_candidate(
    request: Request,
    principal: _READER,
    memory_id: uuid.UUID,
    body: MemoryRejectRequest,
) -> RejectOut:
    with _preference_errors():
        written = await _service(request).reject_candidate(
            principal,
            MemoryCandidateRef(memory_id, body.expected_version),
            reason=body.reason,
        )
    assert written is not None  # a memory candidate gets a rejected version
    return RejectOut(version=_version(await _named(request, written)))


@router.post(
    "/memories/{memory_id}/interpret",
    response_model=PreviewOut,
    summary="The structured preview of a free-text answer (その他)",
)
async def interpret_memory_candidate(
    request: Request,
    principal: _READER,
    memory_id: uuid.UUID,
    body: MemoryInterpretRequest,
) -> PreviewOut:
    with _preference_errors():
        preview = await _service(request).interpret(
            principal, MemoryCandidateRef(memory_id, body.expected_version), body.text
        )
    return _preview(preview)


@router.post(
    "/held/{entry_id}/{item_index}/confirm",
    response_model=MemoryVersionOut,
    summary="Confirm a held candidate at the chosen scope",
)
async def confirm_held_candidate(
    request: Request,
    principal: _READER,
    entry_id: uuid.UUID,
    item_index: Annotated[int, _ITEM_INDEX],
    body: ConfirmFields,
) -> MemoryVersionOut:
    return await _confirmed(
        request, principal, HeldCandidateRef(entry_id, item_index), body
    )


@router.post(
    "/held/{entry_id}/{item_index}/reject",
    response_model=RejectOut,
    summary="Do not keep a held candidate (保存しない)",
)
async def reject_held_candidate(
    request: Request,
    principal: _READER,
    entry_id: uuid.UUID,
    item_index: Annotated[int, _ITEM_INDEX],
    body: HeldRejectRequest,
) -> RejectOut:
    with _preference_errors():
        await _service(request).reject_candidate(
            principal, HeldCandidateRef(entry_id, item_index), reason=body.reason
        )
    return RejectOut(version=None)


@router.post(
    "/held/{entry_id}/{item_index}/interpret",
    response_model=PreviewOut,
    summary="The structured preview of a free-text answer to a held candidate",
)
async def interpret_held_candidate(
    request: Request,
    principal: _READER,
    entry_id: uuid.UUID,
    item_index: Annotated[int, _ITEM_INDEX],
    body: HeldInterpretRequest,
) -> PreviewOut:
    with _preference_errors():
        preview = await _service(request).interpret(
            principal, HeldCandidateRef(entry_id, item_index), body.text
        )
    return _preview(preview)
