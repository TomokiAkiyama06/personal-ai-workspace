"""The Admin's policy of the CLI agents' models over HTTP (issue #341,
Decision 0103, 2 A, 3 A, 5 A and 7 A; approved 2026-11-08).

* ``GET /admin/agent-models``: per kind (Codex, Claude) the stored policy (the
  allowed models, the default, the Admin's descriptions, when it was written),
  the runtime configuration's model (the only allowed one while no policy is
  stored: ``source`` ``configuration``) and the candidates, the models the
  repository verified for the kind with the CLI versions each was verified on
  (``agent_models.verified``).
* ``PUT /admin/agent-models`` ``{"policies": [{"kind", "models": [{"model",
  "default", "description"}]}]}``: replace the policy of each kind listed
  (others are kept). Each kind lists 1 to 16 models, no model twice, exactly
  one default; a model the repository has not verified for the kind is 422
  ``model_not_verified``, any other break of the rules 422
  ``validation_error``. The answer is the list as ``GET`` shows it.

Both routes need ``admin.config.manage`` (Owner / Admin: no new capability);
the change also needs a recent **Passkey Step-up** of the session (as the
connections' changes): a refusal is audited as a denial of
``admin.agent_models.update`` with the step-up reason. The change and its
``admin.agent_models.update`` audit event are one transaction (no record, no
change: 503 when it cannot be written); the event holds ids and enums only,
never a model name (Decision 0103, 7 A: the names are in the policy's table
and its history).

A change of a kind's default model starts that connection's health probe once
(trigger ``policy``, Decision 0103, 5 A), in the background and best effort as
after an attach / replace: the change stands whatever the probe does.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from paw_backend.agent_models import (
    ACTION_UPDATE,
    AgentModelPermissionDeniedError,
    AgentModelService,
    AllowedModel,
    InvalidModelPolicyError,
    KindModels,
    ModelNotVerifiedError,
    verified_versions,
)
from paw_backend.agent_models.policy import (
    MAX_DESCRIPTION_CHARS,
    MAX_MODELS_PER_KIND,
    RESOURCE_KIND,
)
from paw_backend.api.v1.auth import _session_of, get_auth
from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidAuthInputError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.authz import AuditEvent, Capability, Principal, require_capability
from paw_backend.authz.policy import Reason
from paw_backend.connections import ConnectionKind, ProbeRefusedError, ProbeTrigger
from paw_backend.connections.errors import ConnectionNotFoundError
from paw_backend.db import DatabaseDisposedError
from paw_backend.errors import ApiError
from paw_backend.research.providers.broker import log_type_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-models"])

_MANAGER = Annotated[
    Principal, Depends(require_capability(Capability.ADMIN_CONFIG_MANAGE))
]
_AUDIT_TIMEOUT_SECONDS = 3.0
_MODEL = Annotated[StrictStr, Field(min_length=1, max_length=100)]
_KINDS = Literal["codex", "claude"]


# -- models ---------------------------------------------------------------------------


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: _MODEL
    default: StrictBool = False
    description: (
        Annotated[StrictStr, Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)]
        | None
    ) = None


class KindPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: _KINDS
    models: Annotated[
        list[ModelIn], Field(min_length=1, max_length=MAX_MODELS_PER_KIND)
    ]


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policies: Annotated[list[KindPolicyIn], Field(min_length=1, max_length=2)]


class ModelOut(BaseModel):
    model: str
    default: bool
    description: str | None = None


class CandidateOut(BaseModel):
    model: str
    # The CLI versions the model was verified on.
    versions: list[str]


class KindOut(BaseModel):
    kind: _KINDS
    # ``policy``: the Admin's; ``configuration``: none stored, the runtime
    # configuration's model is the only allowed one; ``none``: neither.
    source: Literal["policy", "configuration", "none"]
    models: list[ModelOut]
    default_model: str | None = None
    # The runtime configuration's ``[agents.<kind>].model`` (null: none).
    configured_model: str | None = None
    candidates: list[CandidateOut]
    # When the policy was written (null without one).
    updated_at: datetime | None = None


class AgentModelsResponse(BaseModel):
    kinds: list[KindOut]


# -- helpers --------------------------------------------------------------------------


def _service(request: Request) -> AgentModelService:
    service: AgentModelService | None = getattr(request.app.state, "agent_models", None)
    if service is None:
        raise ApiError(503, "service_unavailable", "Service temporarily unavailable")
    return service


@contextlib.contextmanager
def _errors() -> Iterator[None]:
    """The service's errors as the API's (fixed messages)."""
    try:
        yield
    except AgentModelPermissionDeniedError as error:
        if error.reason is Reason.AUDIT_UNAVAILABLE:
            raise ApiError(
                503, "service_unavailable", "Service temporarily unavailable"
            ) from None
        raise ApiError(403, "forbidden", "Permission denied") from None
    except ModelNotVerifiedError:
        raise ApiError(
            422,
            "model_not_verified",
            "A model is not verified for its kind in this release",
        ) from None
    except InvalidModelPolicyError:
        raise ApiError(422, "validation_error", "Invalid request") from None
    except (TimeoutError, psycopg.Error, OSError, DatabaseDisposedError):
        # The change was not committed (or, at the deadline of the COMMIT
        # itself, is unknown): read the list again.
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


def _kind_out(found: KindModels) -> KindOut:
    policy = found.policy
    if policy is not None:
        source = "policy"
        models = [
            ModelOut(model=m.model, default=m.default, description=m.description)
            for m in policy.models
        ]
    elif found.configured is not None:
        source = "configuration"
        models = [ModelOut(model=found.configured, default=True)]
    else:
        source, models = "none", []
    return KindOut(
        kind=found.kind.value,
        source=source,
        models=models,
        default_model=found.effective_default,
        configured_model=found.configured,
        candidates=[
            CandidateOut(
                model=model, versions=list(verified_versions(found.kind, model))
            )
            for model in sorted(found.candidates)
        ],
        updated_at=None if policy is None else policy.updated_at,
    )


async def _require_step_up(request: Request, principal: Principal) -> None:
    """The session's Passkey Step-up (as the connections' routes); a refusal is
    audited as a denial of the change with the step-up reason, then 403."""
    auth = get_auth(request)
    session_id = _session_of(request).session.record.id
    try:
        await auth.service.require_passkey_step_up(principal, session_id)
    except StepUpMethodInsufficientError:
        await _audit_refusal(request, principal, "step_up_method_insufficient")
        raise ApiError(
            403,
            "step_up_method_insufficient",
            "This operation needs a Passkey step-up",
        ) from None
    except StepUpRequiredError:
        await _audit_refusal(request, principal, "step_up_required")
        raise ApiError(
            403, "step_up_required", "A recent step-up authentication is required"
        ) from None
    except InvalidAuthInputError:
        raise ApiError(422, "validation_error", "Invalid request") from None
    except AuthUnavailableError:
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


async def _audit_refusal(request: Request, principal: Principal, reason: str) -> None:
    """Best effort (the change is refused anyway): a failure is logged by type."""
    try:
        event = AuditEvent(
            event_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            actor_id=principal.user_id,
            actor_role=principal.system_role.value,
            action=ACTION_UPDATE,
            resource_kind=RESOURCE_KIND,
            decision="deny",
            reason=reason,
        )
        async with asyncio.timeout(_AUDIT_TIMEOUT_SECONDS):
            await get_auth(request).audit_sink.record(event)
    except Exception as error:
        logger.error(
            "agent models step-up audit write failed (%s)", type(error).__name__
        )


async def _probe_after_change(
    request: Request, principal: Principal, kind: ConnectionKind
) -> None:
    """Start the probe that follows a change of the default model (Decision
    0103, 5 A). The change stands whatever happens here: no connection, no
    probe of the kind or a refusal is the service's log line; any other
    failure is logged by type."""
    connections = getattr(request.app.state, "connections", None)
    if connections is None:
        return
    try:
        await connections.start_probe(principal, kind, ProbeTrigger.POLICY)
    except (ProbeRefusedError, ConnectionNotFoundError):
        return
    except Exception as error:
        logger.warning(
            "connection probe not started: kind=%s trigger=policy exception_type=%s",
            kind.value,
            log_type_name(error),
        )


# -- routes ---------------------------------------------------------------------------


@router.get(
    "/admin/agent-models",
    response_model=AgentModelsResponse,
    summary="The CLI agents' allowed and default models, and the verified ones",
)
async def list_agent_models(
    request: Request, principal: _MANAGER
) -> AgentModelsResponse:
    service = _service(request)
    with _errors():
        found = await service.read(principal)
    return AgentModelsResponse(kinds=[_kind_out(item) for item in found])


@router.put(
    "/admin/agent-models",
    response_model=AgentModelsResponse,
    summary=(
        "Replace the allowed and default models of the kinds listed "
        "(verified models only; needs a recent Passkey step-up)"
    ),
)
async def update_agent_models(
    body: UpdateRequest, request: Request, principal: _MANAGER
) -> AgentModelsResponse:
    service = _service(request)
    kinds = [item.kind for item in body.policies]
    if len(set(kinds)) != len(kinds):
        raise ApiError(422, "validation_error", "Invalid request")
    await _require_step_up(request, principal)
    policies = {
        ConnectionKind(item.kind): tuple(
            AllowedModel(m.model, m.default, m.description) for m in item.models
        )
        for item in body.policies
    }
    with _errors():
        found, changed = await service.update(principal, policies)
    for kind in changed:
        await _probe_after_change(request, principal, kind)
    return AgentModelsResponse(kinds=[_kind_out(item) for item in found])
