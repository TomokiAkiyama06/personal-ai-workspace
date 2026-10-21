"""The shared Codex / Claude connections' credentials over HTTP (issue #277,
Decision 0087, 2: an Owner / Admin hands the handle that
``paw-connection-credential-put`` printed to ``connect`` /
``replace_credential``; Decision 0016: the credential's replacement and removal
are Security-sensitive and take the Step-up once PAW-023 exists).

* ``GET /admin/connections``: every kind, configured or not, with its state
  (``connected`` / ``unavailable`` / ``expired``), its switch, its last check
  and its attached credential: whether the secret store holds it, its version
  and the ``revision`` a replacement names. Never the value, never the handle.
* ``POST /admin/connections/{kind}`` ``{"secret_handle"}``: attach a stored
  credential to a new connection (201; 409 ``connection_exists``).
* ``PUT /admin/connections/{kind}/credential`` ``{"secret_handle",
  "expected_revision"}``: replace it, only while the connection still has the
  credential of ``expected_revision`` (409 ``stale_connection_credential``
  otherwise: read the status again).
* ``DELETE /admin/connections/{kind}``: detach it (the connection goes, its
  usage history stays; a credential nothing refers to any more is deleted).

Every route needs ``admin.config.manage`` (Owner / Admin), and the service
authorizes again and audits (``connection.connect`` / ``.replace`` /
``.disconnect``, ids and enums only). The three changes need a recent
**Passkey Step-up** of the session, as the quota changes (Decision 0069, 5):
a refusal is audited as a denial of the action with the step-up reason. A
handle the store does not hold for that kind is 422 ``credential_not_stored``.
Without a configured secret store (``PAW_SECRET_STORE_DIR``) a handle could
not be checked: attaching and replacing answer 503
``secret_store_not_configured``.
A newly attached or replaced credential is ``unavailable`` until a health
check verifies it (Decision 0016). No route here takes or shows a plaintext.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from starlette import status

from paw_backend.api.v1.auth import _session_of, get_auth
from paw_backend.auth.errors import (
    AuthUnavailableError,
    InvalidAuthInputError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.authz import AuditEvent, Capability, Principal, require_capability
from paw_backend.authz.policy import Reason
from paw_backend.connections import (
    ConnectionBusyError,
    ConnectionCredentialStatus,
    ConnectionExistsError,
    ConnectionKind,
    ConnectionNotFoundError,
    ConnectionPermissionDeniedError,
    ConnectionService,
    CredentialNotStoredError,
    InvalidConnectionInputError,
    StaleConnectionCredentialError,
)
from paw_backend.connections.service import (
    ACTION_CONNECT,
    ACTION_DISCONNECT,
    ACTION_REPLACE,
)
from paw_backend.db import DatabaseDisposedError
from paw_backend.errors import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connections"])

_MANAGER = Annotated[
    Principal, Depends(require_capability(Capability.ADMIN_CONFIG_MANAGE))
]

_RESOURCE_CONNECTION = "connection"
_AUDIT_TIMEOUT_SECONDS = 3.0
# ``cred_`` and 32 hex digits; the service checks the shape (and never echoes it).
_HANDLE = Annotated[StrictStr, Field(min_length=1, max_length=64)]


# -- models ---------------------------------------------------------------------------


class AttachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_handle: _HANDLE


class ReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_handle: _HANDLE
    # The ``revision`` of ``GET /admin/connections`` this replacement is meant for.
    expected_revision: Annotated[StrictStr, Field(min_length=1, max_length=64)]


class CredentialOut(BaseModel):
    # Whether the secret store holds it for this kind; null: no store to ask.
    stored: bool | None
    # The store's current version (a refreshed login counts it up); null when
    # not stored or no store.
    version: int | None


class ConnectionOut(BaseModel):
    kind: Literal["codex", "claude"]
    configured: bool
    # null when not configured.
    status: Literal["connected", "unavailable", "expired"] | None = None
    enabled: bool | None = None
    available: bool | None = None
    checked_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    credential: CredentialOut | None = None
    # The compare-and-swap token of a replacement (not the handle).
    revision: str | None = None


class ConnectionsResponse(BaseModel):
    connections: list[ConnectionOut]


# -- helpers --------------------------------------------------------------------------


def _service(request: Request) -> ConnectionService:
    service: ConnectionService | None = getattr(request.app.state, "connections", None)
    if service is None:
        raise ApiError(503, "service_unavailable", "Service temporarily unavailable")
    return service


def _require_store(request: Request) -> None:
    if not getattr(request.app.state, "secret_store_configured", False):
        raise ApiError(
            503, "secret_store_not_configured", "No secret store is configured"
        )


@contextlib.contextmanager
def _errors() -> Iterator[None]:
    """The connection service's errors as the API's (fixed messages)."""
    try:
        yield
    except ConnectionPermissionDeniedError as error:
        if error.reason is Reason.AUDIT_UNAVAILABLE:
            raise ApiError(
                503, "service_unavailable", "Service temporarily unavailable"
            ) from None
        raise ApiError(403, "forbidden", "Permission denied") from None
    except ConnectionNotFoundError:
        raise ApiError(404, "not_found", "Not Found") from None
    except ConnectionExistsError:
        raise ApiError(
            409, "connection_exists", "A connection of this kind already exists"
        ) from None
    except StaleConnectionCredentialError:
        raise ApiError(
            409,
            "stale_connection_credential",
            "The connection's credential was changed; read it again",
        ) from None
    except CredentialNotStoredError:
        raise ApiError(
            422,
            "credential_not_stored",
            "The secret store holds no such credential for this kind",
        ) from None
    except InvalidConnectionInputError:
        raise ApiError(422, "validation_error", "Invalid request") from None
    except (ConnectionBusyError, TimeoutError, OSError, DatabaseDisposedError):
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


def _out(
    kind: ConnectionKind, found: ConnectionCredentialStatus | None
) -> ConnectionOut:
    if found is None:
        return ConnectionOut(kind=kind.value, configured=False)
    info = found.connection
    return ConnectionOut(
        kind=kind.value,
        configured=True,
        status=info.status.value,
        enabled=info.enabled,
        available=info.available,
        checked_at=info.checked_at,
        created_at=info.created_at,
        updated_at=info.updated_at,
        credential=CredentialOut(stored=found.stored, version=found.version),
        revision=found.revision,
    )


async def _status_of(
    service: ConnectionService, principal: Principal
) -> dict[ConnectionKind, ConnectionCredentialStatus]:
    with _errors():
        found = await service.credential_status(principal)
    return {item.connection.kind: item for item in found}


async def _require_step_up(request: Request, principal: Principal, action: str) -> None:
    """The session's Passkey Step-up (as ``usage._require_step_up``); a refusal
    is audited as a denial of ``action`` with the step-up reason, then 403."""
    auth = get_auth(request)
    session_id = _session_of(request).session.record.id
    try:
        await auth.service.require_passkey_step_up(principal, session_id)
    except StepUpMethodInsufficientError:
        await _audit_refusal(request, principal, action, "step_up_method_insufficient")
        raise ApiError(
            403,
            "step_up_method_insufficient",
            "This operation needs a Passkey step-up",
        ) from None
    except StepUpRequiredError:
        await _audit_refusal(request, principal, action, "step_up_required")
        raise ApiError(
            403, "step_up_required", "A recent step-up authentication is required"
        ) from None
    except InvalidAuthInputError:
        raise ApiError(422, "validation_error", "Invalid request") from None
    except AuthUnavailableError:
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


async def _audit_refusal(
    request: Request, principal: Principal, action: str, reason: str
) -> None:
    """Best effort, like the service's own events: a failure is logged by type."""
    try:
        event = AuditEvent(
            event_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            actor_id=principal.user_id,
            actor_role=principal.system_role.value,
            action=action,
            resource_kind=_RESOURCE_CONNECTION,
            resource_id=None,
            project_id=None,
            decision="deny",
            reason=reason,
        )
        async with asyncio.timeout(_AUDIT_TIMEOUT_SECONDS):
            await get_auth(request).audit_sink.record(event)
    except Exception as error:
        logger.error("connection step-up audit write failed (%s)", type(error).__name__)


# -- routes ---------------------------------------------------------------------------


@router.get(
    "/admin/connections",
    response_model=ConnectionsResponse,
    summary="The shared connections and their attached credentials (no values)",
)
async def list_connections(
    request: Request, principal: _MANAGER
) -> ConnectionsResponse:
    found = await _status_of(_service(request), principal)
    return ConnectionsResponse(
        connections=[_out(kind, found.get(kind)) for kind in ConnectionKind]
    )


@router.post(
    "/admin/connections/{kind}",
    response_model=ConnectionOut,
    status_code=status.HTTP_201_CREATED,
    summary=(
        "Attach a stored credential (its handle) to a new connection "
        "(needs a recent Passkey step-up)"
    ),
)
async def attach(
    kind: ConnectionKind, body: AttachRequest, request: Request, principal: _MANAGER
) -> ConnectionOut:
    service = _service(request)
    _require_store(request)
    await _require_step_up(request, principal, ACTION_CONNECT)
    with _errors():
        await service.connect(principal, kind, body.secret_handle)
    return _out(kind, (await _status_of(service, principal)).get(kind))


@router.put(
    "/admin/connections/{kind}/credential",
    response_model=ConnectionOut,
    summary=(
        "Replace the connection's credential, only while it is still the one of "
        "expected_revision (needs a recent Passkey step-up)"
    ),
)
async def replace(
    kind: ConnectionKind, body: ReplaceRequest, request: Request, principal: _MANAGER
) -> ConnectionOut:
    service = _service(request)
    _require_store(request)
    await _require_step_up(request, principal, ACTION_REPLACE)
    with _errors():
        await service.replace_credential(
            principal,
            kind,
            body.secret_handle,
            expected_revision=body.expected_revision,
        )
    return _out(kind, (await _status_of(service, principal)).get(kind))


@router.delete(
    "/admin/connections/{kind}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Detach the credential: remove the connection (needs a Passkey step-up)",
)
async def detach(
    kind: ConnectionKind, request: Request, principal: _MANAGER
) -> Response:
    service = _service(request)  # removing needs no store to check a handle
    await _require_step_up(request, principal, ACTION_DISCONNECT)
    with _errors():
        await service.disconnect(principal, kind)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
