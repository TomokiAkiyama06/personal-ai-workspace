"""Invitations, device pairing and the user lifecycle (PAW-024, Decision 0033).

``/api/v1/auth/invitations/*``, ``/api/v1/auth/users/*``, ``/api/v1/auth/pairing/*``.
The behaviour is in ``paw_backend.auth.onboarding``; this module only translates.

Public (no session; each is on the route list of ``tests/test_authz_routes.py``
with the reason):

* ``POST /invitations/redeem``: an invited person sets their password with the
  one-time invitation token (rate limited per source and in total, like the Owner's
  token, before the token is looked at);
* ``POST /pairing/claim`` and ``POST /pairing/complete``: a new device hands in the
  pairing token (QR code / link) and, for an Owner / Admin, completes with its claim
  once a trusted device approved it (rate limited per source and in total; a
  correct token or claim gives its attempt back).

Every other route needs a session that is not restricted by the Passkey policy
(``require_capability`` without ``allow_restricted``): inviting and deleting need
``admin.users.manage`` (the service applies the role rules: an Admin manages Users,
the Owner also Admins), restoring ``owner.user_restore``; pairing is the user's own
(``account.manage`` / ``account.read``). Inviting, deleting, restoring and approving
an Owner's / Admin's new device need a recent Passkey Step-up of the session.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from starlette import status

from paw_backend.api.v1.auth import (
    _PASSWORD_INPUT_MAX,
    _TOKEN_MAX,
    Auth,
    SessionResponse,
    _answer_committed,
    _context,
    _session_of,
    api_errors,
)
from paw_backend.auth.limits import (
    DEVICE_LABEL_MAX_LENGTH,
    LOGIN_NAME_MAX_INPUT,
    SESSION_COOKIE_NAME,
)
from paw_backend.auth.onboarding.invitations import IssuedInvitation
from paw_backend.auth.onboarding.pairing import PairingOutcome
from paw_backend.authz import Capability, Principal, require_capability
from paw_backend.authz.roles import SystemRole

router = APIRouter(prefix="/auth", tags=["auth", "accounts"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteRequest(_Body):
    login_name: StrictStr = Field(min_length=1, max_length=LOGIN_NAME_MAX_INPUT)
    # An Owner is never invited (the server-local CLI creates it).
    system_role: Literal["user", "admin"] = "user"


class InvitationResponse(BaseModel):
    user_id: uuid.UUID
    login_name: str
    system_role: str
    status: str = "invited"
    # Shown once: hand it to the invited person (it is not stored and cannot be
    # shown again; reissue to get a new one).
    token: str
    expires_at: datetime


class RedeemInvitationRequest(_Body):
    token: StrictStr = Field(min_length=1, max_length=_TOKEN_MAX)
    new_password: StrictStr = Field(min_length=1, max_length=_PASSWORD_INPUT_MAX)


class RedeemInvitationResponse(BaseModel):
    # The password is set and the account is active; nobody is signed in.
    next: str = "login"


class UserStatusResponse(BaseModel):
    user_id: uuid.UUID
    status: str


class PairingResponse(BaseModel):
    pairing_id: uuid.UUID
    # Shown once, on this trusted device: as a QR code of the public origin plus
    # ``link_path``, or as that link itself (the token is in the URL fragment).
    token: str
    link_path: str
    expires_at: datetime
    # The new device will wait for this user's explicit approval (Owner / Admin).
    approval_required: bool


class PairingRevokedResponse(BaseModel):
    revoked: int


class PendingPairingOut(BaseModel):
    pairing_id: uuid.UUID
    device_name: str | None
    claimed_at: datetime
    expires_at: datetime


class PendingPairingsResponse(BaseModel):
    pending: list[PendingPairingOut]


class ClaimRequest(_Body):
    token: StrictStr = Field(min_length=1, max_length=_TOKEN_MAX)
    device_name: StrictStr = Field(min_length=1, max_length=DEVICE_LABEL_MAX_LENGTH)
    remember_me: StrictBool = False


class CompleteRequest(_Body):
    claim: StrictStr = Field(min_length=1, max_length=_TOKEN_MAX)


class PairingProgressResponse(BaseModel):
    # ``completed``: the cookie is in this response and ``session`` describes it.
    # ``pending_approval``: wait for a trusted device, then ``POST /pairing/complete``
    # with ``claim`` (given once, in the answer to the claim) before ``expires_at``.
    status: Literal["completed", "pending_approval"]
    session: SessionResponse | None = None
    claim: str | None = None
    expires_at: datetime | None = None


def _invitation_out(issued: IssuedInvitation) -> InvitationResponse:
    return InvitationResponse(
        user_id=issued.user_id,
        login_name=issued.login_name,
        system_role=issued.system_role.value,
        token=issued.token,
        expires_at=issued.expires_at,
    )


_ADMIN = Annotated[
    Principal, Depends(require_capability(Capability.ADMIN_USERS_MANAGE))
]


# -- invitations --------------------------------------------------------------------


@router.post(
    "/invitations",
    status_code=status.HTTP_201_CREATED,
    response_model=InvitationResponse,
    summary="Invite a user (an Admin: a User; the Owner: a User or an Admin)",
)
async def invite(
    body: InviteRequest, request: Request, services: Auth, principal: _ADMIN
) -> InvitationResponse:
    with api_errors():
        issued = await services.invitations.invite(
            principal,
            body.login_name,
            SystemRole(body.system_role),
            _context(request),
            session_id=_session_of(request).session.record.id,
        )
    return _invitation_out(issued)


@router.post(
    "/invitations/redeem",
    response_model=RedeemInvitationResponse,
    summary="Set the password of an invited account with its invitation token",
)
async def redeem_invitation(
    body: RedeemInvitationRequest, request: Request, services: Auth
) -> RedeemInvitationResponse:
    with api_errors():
        await services.invitations.redeem(
            body.token, body.new_password, _context(request)
        )
    return RedeemInvitationResponse()


@router.post(
    "/invitations/{user_id}/reissue",
    response_model=InvitationResponse,
    summary="Issue a new invitation token (the outstanding one ends)",
)
async def reissue_invitation(
    user_id: uuid.UUID, request: Request, services: Auth, principal: _ADMIN
) -> InvitationResponse:
    with api_errors():
        issued = await services.invitations.reissue(
            principal,
            user_id,
            _context(request),
            session_id=_session_of(request).session.record.id,
        )
    return _invitation_out(issued)


@router.post(
    "/invitations/{user_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the outstanding invitation token (the user stays invited)",
)
async def revoke_invitation(
    user_id: uuid.UUID, request: Request, services: Auth, principal: _ADMIN
) -> Response:
    with api_errors():
        await services.invitations.revoke(
            principal,
            user_id,
            _context(request),
            session_id=_session_of(request).session.record.id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- the user lifecycle -------------------------------------------------------------


@router.delete(
    "/users/{user_id}",
    response_model=UserStatusResponse,
    summary=(
        "Delete a user (pending deletion for 30 days) or cancel an invitation "
        "(needs a recent Passkey step-up)"
    ),
)
async def delete_user(
    user_id: uuid.UUID, request: Request, services: Auth, principal: _ADMIN
) -> UserStatusResponse:
    with api_errors():
        new_status = await services.lifecycle.delete_user(
            principal,
            user_id,
            _context(request),
            session_id=_session_of(request).session.record.id,
        )
    return UserStatusResponse(user_id=user_id, status=new_status.value)


@router.post(
    "/users/{user_id}/restore",
    response_model=UserStatusResponse,
    summary="Restore a user pending deletion (the Owner, within 30 days)",
)
async def restore_user(
    user_id: uuid.UUID,
    request: Request,
    services: Auth,
    principal: Annotated[
        Principal, Depends(require_capability(Capability.OWNER_USER_RESTORE))
    ],
) -> UserStatusResponse:
    with api_errors():
        new_status = await services.lifecycle.restore_user(
            principal,
            user_id,
            _context(request),
            session_id=_session_of(request).session.record.id,
        )
    return UserStatusResponse(user_id=user_id, status=new_status.value)


# -- pairing: the trusted device ------------------------------------------------------


@router.post(
    "/pairing",
    status_code=status.HTTP_201_CREATED,
    response_model=PairingResponse,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Add a new device: a one-time pairing token for a QR code or a link",
)
async def issue_pairing(request: Request, services: Auth) -> PairingResponse:
    with api_errors():
        issued = await services.pairing.issue(
            _session_of(request).session, _context(request)
        )
    return PairingResponse(
        pairing_id=issued.pairing_id,
        token=issued.token,
        link_path=issued.link_path,
        expires_at=issued.expires_at,
        approval_required=issued.approval_required,
    )


@router.delete(
    "/pairing",
    response_model=PairingRevokedResponse,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Revoke the pairing token that has not been used (and any waiting device)",
)
async def revoke_pairing(request: Request, services: Auth) -> PairingRevokedResponse:
    with api_errors():
        count = await services.pairing.revoke(
            _session_of(request).session, _context(request)
        )
    return PairingRevokedResponse(revoked=count)


@router.get(
    "/pairing/pending",
    response_model=PendingPairingsResponse,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_READ))],
    summary="New devices of the user that wait for an approval",
)
async def pending_pairings(request: Request, services: Auth) -> PendingPairingsResponse:
    with api_errors():
        pending = await services.pairing.pending(_session_of(request).session)
    return PendingPairingsResponse(
        pending=[
            PendingPairingOut(
                pairing_id=item.pairing_id,
                device_name=item.device_label,
                claimed_at=item.claimed_at,
                expires_at=item.expires_at,
            )
            for item in pending
        ]
    )


@router.post(
    "/pairing/{pairing_id}/approve",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Approve a waiting new device (needs a recent Passkey step-up)",
)
async def approve_pairing(
    pairing_id: uuid.UUID, request: Request, services: Auth
) -> Response:
    with api_errors():
        await services.pairing.approve(
            _session_of(request).session, pairing_id, _context(request)
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/pairing/{pairing_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Refuse a waiting new device",
)
async def reject_pairing(
    pairing_id: uuid.UUID, request: Request, services: Auth
) -> Response:
    with api_errors():
        await services.pairing.reject(
            _session_of(request).session, pairing_id, _context(request)
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- pairing: the new device (public) ------------------------------------------------


async def _progress(
    outcome: PairingOutcome, response: Response, services
) -> PairingProgressResponse:
    if outcome.login is not None:
        session = await _answer_committed(response, services, outcome.login)
        return PairingProgressResponse(status="completed", session=session)
    response.status_code = status.HTTP_202_ACCEPTED
    return PairingProgressResponse(
        status="pending_approval", claim=outcome.claim, expires_at=outcome.expires_at
    )


@router.post(
    "/pairing/claim",
    response_model=PairingProgressResponse,
    responses={202: {"model": PairingProgressResponse}},
    summary="A new device hands in the pairing token of a QR code / link",
)
async def claim_pairing(
    body: ClaimRequest, request: Request, response: Response, services: Auth
) -> PairingProgressResponse:
    with api_errors():
        outcome = await services.pairing.claim(
            body.token,
            body.device_name,
            _context(request),
            remember_me=body.remember_me,
            replace_token=request.cookies.get(SESSION_COOKIE_NAME),
        )
    return await _progress(outcome, response, services)


@router.post(
    "/pairing/complete",
    response_model=PairingProgressResponse,
    responses={202: {"model": PairingProgressResponse}},
    summary="A new device that waited for its approval gets its session",
)
async def complete_pairing(
    body: CompleteRequest, request: Request, response: Response, services: Auth
) -> PairingProgressResponse:
    with api_errors():
        outcome = await services.pairing.complete(
            body.claim,
            _context(request),
            replace_token=request.cookies.get(SESSION_COOKIE_NAME),
        )
    return await _progress(outcome, response, services)
