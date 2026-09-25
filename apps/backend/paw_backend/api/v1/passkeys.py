"""Passkeys: register, authenticate (Step-up), list, revoke.

``/api/v1/auth/passkeys/*`` (PAW-023; the design is Decision 0025). The browser side
is plain WebAuthn: ``.../begin`` returns the options (``navigator.credentials.create()``
/ ``get()``), the client posts the answer to ``.../finish``. The challenge is kept on
the server, tied to the signed-in session, single use and short lived; nothing about
it travels in a cookie or a URL.

Every route needs a session. The routes that exist to get a *restricted* session
(the Passkey policy requires a Passkey it does not have yet, or has not used yet) out
of that state accept it: ``register/*``, ``authenticate/*`` and the list. Revoking a
Passkey does not: a restricted session may not touch its credentials.

Registration and revocation are refused without a recent Step-up (a Passkey one
whenever the account has a Passkey / its role requires one); see
``paw_backend.auth.passkeys.service``. The origin rules are the ones of the other
authentication routes (``OriginCheckMiddleware`` refuses a cross-origin state change
before it gets here); the WebAuthn origin the BROWSER signs is checked against
``PAW_PASSKEY_ORIGINS`` inside the ceremony.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from paw_backend.api.v1.auth import (
    Auth,
    SessionResponse,
    _answer_committed,
    _clear_session_cookie,
    _context,
    _session_of,
    api_errors,
)
from paw_backend.auth.errors import PasskeyUnavailableError
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.passkeys.models import PASSKEY_NAME_MAX_LENGTH
from paw_backend.auth.passkeys.store import PasskeyRecord
from paw_backend.auth.passkeys.types import parse_assertion_credential
from paw_backend.auth.state import StepUpEvidence
from paw_backend.authz import Capability, require_capability

router = APIRouter(prefix="/auth/passkeys", tags=["auth", "passkeys"])

# The client answers with the ``toJSON()`` of a ``PublicKeyCredential``; its shape
# and every size in it are checked by ``paw_backend.auth.passkeys.types`` (a value
# that is not exactly that is a 422). It is only ever a JSON object here.
Credential = dict[str, Any]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterFinishRequest(_Body):
    credential: Credential
    # A name for the device, shown in the list (default: "Passkey").
    name: StrictStr | None = Field(default=None, max_length=PASSKEY_NAME_MAX_LENGTH)


class AuthenticateFinishRequest(_Body):
    credential: Credential


class OptionsResponse(BaseModel):
    # The JSON options for ``navigator.credentials.create()`` / ``get()``.
    options: dict[str, Any]


class PasskeyOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    # A synced (multi-device) credential, and whether it is backed up right now.
    backup_eligible: bool
    backed_up: bool


class PasskeyListResponse(BaseModel):
    passkeys: list[PasskeyOut]


class RegisteredResponse(BaseModel):
    passkey: PasskeyOut
    # Set when registering lifted the session's gate: the session has a NEW id (the
    # new cookie is in this response).
    session: SessionResponse | None = None


class RevokedResponse(BaseModel):
    revoked: bool = True
    # Sessions the revoked Passkey had opened; they ended with it.
    sessions_ended: int
    # This very session was one of them: the cookie is cleared, sign in again.
    signed_out: bool


def _out(record: PasskeyRecord) -> PasskeyOut:
    return PasskeyOut(
        id=record.id,
        name=record.name,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        backup_eligible=record.backup_eligible,
        backed_up=record.backed_up,
    )


# ``allow_restricted``: these routes are how a restricted session gets out of it.
_MANAGE_RESTRICTED = Depends(
    require_capability(Capability.ACCOUNT_MANAGE, allow_restricted=True)
)
_READ_RESTRICTED = Depends(
    require_capability(Capability.ACCOUNT_READ, allow_restricted=True)
)


@router.post(
    "/enroll/begin",
    response_model=OptionsResponse,
    dependencies=[_MANAGE_RESTRICTED],
    summary="Start registering a Passkey (options for credentials.create)",
)
async def register_begin(request: Request, services: Auth) -> OptionsResponse:
    auth = _session_of(request).session
    with api_errors():
        options = await services.passkeys.register_begin(auth, _context(request))
    return OptionsResponse(options=options)


@router.post(
    "/enroll/finish",
    response_model=RegisteredResponse,
    dependencies=[_MANAGE_RESTRICTED],
    summary="Finish registering a Passkey (the answer of navigator.credentials.create)",
)
async def register_finish(
    body: RegisterFinishRequest, request: Request, response: Response, services: Auth
) -> RegisteredResponse:
    auth = _session_of(request).session
    with api_errors():
        result = await services.passkeys.register_finish(
            auth, body.credential, body.name, _context(request)
        )
    if result.login is None:
        return RegisteredResponse(passkey=_out(result.passkey))
    # The registration lifted the session's gate and rotated its id: the cookie goes
    # out first (the old one is dead), as for every committed rotation.
    session = await _answer_committed(response, services, result.login)
    return RegisteredResponse(passkey=_out(result.passkey), session=session)


@router.post(
    "/authenticate/begin",
    response_model=OptionsResponse,
    dependencies=[_MANAGE_RESTRICTED],
    summary="Start a Passkey authentication (options for credentials.get)",
)
async def authenticate_begin(request: Request, services: Auth) -> OptionsResponse:
    auth = _session_of(request).session
    with api_errors():
        options = await services.passkeys.authenticate_begin(auth, _context(request))
    return OptionsResponse(options=options)


@router.post(
    "/authenticate/finish",
    response_model=SessionResponse,
    dependencies=[_MANAGE_RESTRICTED],
    summary=(
        "Finish a Passkey authentication: the Step-up of sensitive operations, and "
        "the second half of a restricted sign-in (the session id is rotated)"
    ),
)
async def authenticate_finish(
    body: AuthenticateFinishRequest,
    request: Request,
    response: Response,
    services: Auth,
) -> SessionResponse:
    auth = _session_of(request).session
    with api_errors():
        if not services.passkeys.available:
            raise PasskeyUnavailableError
        evidence = StepUpEvidence(
            AuthMethod.PASSKEY, assertion=parse_assertion_credential(body.credential)
        )
        result = await services.service.step_up(auth, evidence, _context(request))
    return await _answer_committed(response, services, result)


@router.get(
    "",
    response_model=PasskeyListResponse,
    dependencies=[_READ_RESTRICTED],
    summary="The user's registered Passkeys",
)
async def list_passkeys(request: Request, services: Auth) -> PasskeyListResponse:
    auth = _session_of(request).session
    with api_errors():
        records = await services.passkeys.list_passkeys(auth)
    return PasskeyListResponse(passkeys=[_out(record) for record in records])


@router.delete(
    "/{passkey_id}",
    response_model=RevokedResponse,
    # Not for a restricted session: no ``allow_restricted``.
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Revoke one of the user's Passkeys (needs a recent Step-up)",
)
async def revoke_passkey(
    passkey_id: uuid.UUID, request: Request, response: Response, services: Auth
) -> RevokedResponse:
    auth = _session_of(request).session
    with api_errors():
        result = await services.passkeys.revoke(auth, passkey_id, _context(request))
    if result.current_session_ended:
        _clear_session_cookie(response, services)
    return RevokedResponse(
        sessions_ended=result.sessions_ended, signed_out=result.current_session_ended
    )
