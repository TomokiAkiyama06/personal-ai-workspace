"""Sign in, sessions, password and Step-up, the Owner token, the auth policy.

``/api/v1/auth/*`` (PAW-022; the design is Decision 0015). The session is an
opaque random id in a ``Secure``, ``HttpOnly``, ``SameSite=Strict`` cookie
(``__Host-paw_session``); the server keeps only its hash. State-changing
requests from another browser origin are refused before they get here
(``OriginCheckMiddleware``).

Public (no session; each is on the route list of ``tests/test_authz_routes.py``
with the reason):

* ``POST /login``: rate limited per account and per source (progressive
  backoff); the answer never says whether the account exists.
* ``POST /token/redeem``: spends an Owner setup / recovery token and sets
  the Owner's password; rate limited per source and in total *before* the token
  is looked at (Decision 0005).

Every other route needs a session (``account.read`` / ``account.manage``, held by
every human role) or a role (``admin.users.manage`` to unlock an account,
``admin.auth_policy.view`` / ``owner.auth_policy.manage`` for the policy).

A session that the Passkey policy restricts (PAW-023: a required Passkey not yet
registered / used) is refused by EVERY route with 403 ``passkey_required`` except
``GET /session``, ``POST /logout`` and the Passkey ceremonies of
``paw_backend.api.v1.passkeys`` (``require_capability(..., allow_restricted=True)``).
Unlocking an account and changing the policy need a recent Passkey step-up.
"""

import contextlib
import logging
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from starlette import status

from paw_backend.auth import tokens
from paw_backend.auth.context import RequestContext
from paw_backend.auth.errors import (
    AccountNotFoundError,
    AuthPermissionError,
    AuthUnavailableError,
    InvalidAuthInputError,
    InvalidCredentialsError,
    LastPasskeyError,
    NoPasskeyError,
    PasskeyChallengeError,
    PasskeyExistsError,
    PasskeyLimitError,
    PasskeyNotFoundError,
    PasskeyRequiredError,
    PasskeyUnavailableError,
    PasskeyVerificationError,
    PasswordPolicyError,
    PolicyVersionConflictError,
    SessionEndedError,
    SessionNotFoundError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
    ThrottledError,
    TokenRejectedError,
)
from paw_backend.auth.limits import (
    DEVICE_LABEL_MAX_LENGTH,
    LOGIN_NAME_MAX_INPUT,
    PASSWORD_MAX_LENGTH,
    SESSION_COOKIE_NAME,
)
from paw_backend.auth.models import AuthMethod
from paw_backend.auth.principals import AuthContext, authenticated_context
from paw_backend.auth.service import LoginResult, SessionView
from paw_backend.auth.sessions import AuthenticatedSession, SessionRecord
from paw_backend.auth.state import StepUpEvidence
from paw_backend.auth.wiring import AuthServices
from paw_backend.authz import Capability, Principal, require_capability
from paw_backend.errors import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# The password is measured in characters after normalisation; the request may be
# a little longer before it (NFKC can shorten a string), and the service refuses
# what is really too long.
_PASSWORD_INPUT_MAX = PASSWORD_MAX_LENGTH * 4
_TOKEN_MAX = 512


def get_auth(request: Request) -> AuthServices:
    return request.app.state.auth


Auth = Annotated[AuthServices, Depends(get_auth)]


# -- request and response models ------------------------------------------------


class _Body(BaseModel):
    # An unknown or misspelled field is an error, never silently ignored.
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_Body):
    login_name: StrictStr = Field(min_length=1, max_length=LOGIN_NAME_MAX_INPUT)
    password: StrictStr = Field(min_length=1, max_length=_PASSWORD_INPUT_MAX)
    remember_me: StrictBool = False
    device_name: StrictStr | None = Field(
        default=None, max_length=DEVICE_LABEL_MAX_LENGTH
    )


class ChangePasswordRequest(_Body):
    current_password: StrictStr = Field(min_length=1, max_length=_PASSWORD_INPUT_MAX)
    new_password: StrictStr = Field(min_length=1, max_length=_PASSWORD_INPUT_MAX)
    # REQUIREMENTS.md: the user chooses; the default keeps the other sessions.
    revoke_other_sessions: StrictBool = False


class StepUpRequest(_Body):
    # The password Step-up. A Passkey Step-up has its own ceremony
    # (``/auth/passkeys/authenticate/begin`` and ``/finish``); naming it here is a
    # validation error, not a proof that fails.
    method: Literal[AuthMethod.PASSWORD] = AuthMethod.PASSWORD
    password: StrictStr | None = Field(default=None, max_length=_PASSWORD_INPUT_MAX)


class RedeemTokenRequest(_Body):
    token: StrictStr = Field(min_length=1, max_length=_TOKEN_MAX)
    new_password: StrictStr = Field(min_length=1, max_length=_PASSWORD_INPUT_MAX)


class PolicyRequest(_Body):
    expected_version: StrictInt = Field(ge=1)
    passkey_owner: StrictStr
    passkey_admin: StrictStr
    passkey_user: StrictStr
    recommend_passkey_to_users: StrictBool
    stepup_window_minutes: StrictInt


class UserOut(BaseModel):
    id: uuid.UUID
    login_name: str
    system_role: str


class SessionOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_used_at: datetime
    # When it ends if unused (idle limit), capped by ``absolute_expires_at``.
    expires_at: datetime
    absolute_expires_at: datetime
    remember_me: bool
    device_name: str | None
    current: bool


class PasskeyOut(BaseModel):
    requirement: str
    enrolled: bool
    enrollment_required: bool
    recommended: bool
    # Passkeys are configured on this server; if not, nothing is enforced.
    available: bool
    # What THIS session may do: ``open``, or restricted (``enrollment_required``:
    # only register a Passkey; ``assertion_required``: only complete a Passkey
    # authentication) until ``next`` is done. See ``/auth/passkeys``.
    gate: str
    next: str | None


class StepUpOut(BaseModel):
    method: str | None
    verified_at: datetime | None
    valid_until: datetime | None
    window_minutes: int
    satisfied: bool


class AuthOut(BaseModel):
    method: str
    passkey: PasskeyOut
    step_up: StepUpOut


class SessionResponse(BaseModel):
    user: UserOut
    session: SessionOut
    # ``null`` only in the answer to a login, password change or step-up whose
    # change COMMITTED but whose policy read failed afterwards: the cookie is in
    # that response and the change is done; ``GET /auth/session`` has the rest.
    auth: AuthOut | None = None


class SessionListResponse(BaseModel):
    sessions: list[SessionOut]


class RevokedResponse(BaseModel):
    revoked: int


class RedeemResponse(BaseModel):
    purpose: str
    passkey_required: bool
    # What the client does next: the password is set, nobody is signed in.
    next: str = "login"


class PolicyResponse(BaseModel):
    version: int
    passkey_owner: str
    passkey_admin: str
    passkey_user: str
    recommend_passkey_to_users: bool
    stepup_window_minutes: int
    updated_at: datetime
    updated_by: uuid.UUID | None
    # The policy applies to new sign-ins and new sessions; a change never
    # ends or downgrades an existing session.
    applies_to: str = "new_sign_ins"


# -- helpers --------------------------------------------------------------------


@contextlib.contextmanager
def api_errors(
    *, credentials_status: int = status.HTTP_403_FORBIDDEN
) -> Iterator[None]:
    """Turn the errors of the authentication code into the API's error responses.

    Every message is fixed: nothing from the request, the account or the
    database appears in a response.
    """
    try:
        yield
    except ThrottledError as error:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts; try again later",
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from None
    except InvalidCredentialsError:
        raise ApiError(
            credentials_status, "invalid_credentials", "Invalid credentials"
        ) from None
    except TokenRejectedError:
        raise ApiError(400, "invalid_token", "The token was not accepted") from None
    except PasswordPolicyError as error:
        raise ApiError(
            422,
            "password_policy",
            f"The password does not meet the policy ({error.problem.value})",
        ) from None
    except InvalidAuthInputError:
        raise ApiError(422, "validation_error", "Invalid request") from None
    except SessionNotFoundError:
        raise ApiError(404, "not_found", "Not Found") from None
    except AccountNotFoundError:
        raise ApiError(404, "not_found", "Not Found") from None
    except SessionEndedError:
        raise ApiError(401, "unauthorized", "Authentication required") from None
    except PasskeyUnavailableError:
        raise ApiError(
            503, "passkey_unavailable", "Passkeys are not configured on this server"
        ) from None
    except PasskeyRequiredError:
        raise ApiError(
            403, "passkey_required", "A passkey is required for this session"
        ) from None
    except PasskeyChallengeError:
        raise ApiError(
            400, "challenge_invalid", "The challenge is not valid; start again"
        ) from None
    except PasskeyVerificationError:
        raise ApiError(
            400, "passkey_rejected", "The passkey response was not accepted"
        ) from None
    except NoPasskeyError:
        raise ApiError(409, "no_passkey", "The account has no passkey") from None
    except PasskeyNotFoundError:
        raise ApiError(404, "not_found", "Not Found") from None
    except PasskeyExistsError:
        raise ApiError(
            409, "passkey_exists", "That credential is already registered"
        ) from None
    except PasskeyLimitError:
        raise ApiError(
            409, "passkey_limit", "Too many passkeys are registered"
        ) from None
    except LastPasskeyError:
        raise ApiError(
            409, "last_passkey", "The last required passkey cannot be revoked"
        ) from None
    except StepUpMethodInsufficientError:
        raise ApiError(
            403,
            "step_up_method_insufficient",
            "This operation needs a Passkey step-up",
        ) from None
    except StepUpRequiredError:
        raise ApiError(
            403, "step_up_required", "A recent step-up authentication is required"
        ) from None
    except PolicyVersionConflictError:
        raise ApiError(
            409, "version_conflict", "The policy was changed by someone else"
        ) from None
    except AuthPermissionError:
        raise ApiError(403, "forbidden", "Permission denied") from None
    except AuthUnavailableError:
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


def _context(request: Request) -> RequestContext:
    correlation = getattr(request.state, "audit_correlation_id", None)
    if not isinstance(correlation, uuid.UUID):
        correlation = uuid.uuid4()
        request.state.audit_correlation_id = correlation
    host = request.client.host if request.client else None
    return RequestContext(
        correlation_id=correlation,
        source=tokens.source_bucket(host),
        client_request_id=getattr(request.state, "request_id", None),
    )


def _session_of(request: Request) -> AuthContext:
    """The session the request's guard resolved (a guarded route always has one)."""
    context = authenticated_context(request)
    if context is None:  # cannot happen behind require_capability
        raise ApiError(401, "unauthorized", "Authentication required")
    return context


def _record_out(record: SessionRecord, current_id: uuid.UUID) -> SessionOut:
    return SessionOut(
        id=record.id,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
        absolute_expires_at=record.absolute_expires_at,
        remember_me=record.remember_me,
        device_name=record.device_label,
        current=record.id == current_id,
    )


def _view_out(view: SessionView) -> SessionResponse:
    auth = view.auth
    session = view.session
    return SessionResponse(
        user=UserOut(
            id=view.user_id,
            login_name=session.login_name,
            system_role=session.system_role.value,
        ),
        session=_record_out(session.record, session.record.id),
        auth=AuthOut(
            method=auth.method.value,
            passkey=PasskeyOut(
                requirement=auth.passkey.requirement.value,
                enrolled=auth.passkey.enrolled,
                enrollment_required=auth.passkey.enrollment_required,
                recommended=auth.passkey.recommended,
                available=auth.passkey.available,
                gate=auth.passkey.gate.value,
                next=auth.passkey.next_step,
            ),
            step_up=StepUpOut(
                method=auth.step_up.method.value if auth.step_up.method else None,
                verified_at=auth.step_up.verified_at,
                valid_until=auth.step_up.valid_until,
                window_minutes=auth.step_up.window_minutes,
                satisfied=auth.step_up.satisfied,
            ),
        ),
    )


def _set_session_cookie(
    response: Response, services: AuthServices, result: LoginResult
):
    record = result.session.record
    max_age = None
    if record.remember_me:
        # Kept across browser restarts, for as long as the server would honour it.
        max_age = max(
            1,
            int(
                (record.absolute_expires_at - result.session.checked_at).total_seconds()
            ),
        )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        result.token,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite=services.samesite,  # type: ignore[arg-type]
    )


def _summary_out(session: AuthenticatedSession) -> SessionResponse:
    """The session and its user, from what the change itself returned (no query)."""
    return SessionResponse(
        user=UserOut(
            id=session.record.user_id,
            login_name=session.login_name,
            system_role=session.system_role.value,
        ),
        session=_record_out(session.record, session.record.id),
        auth=None,
    )


async def _answer_committed(
    response: Response, services: AuthServices, result: LoginResult
) -> SessionResponse:
    """The answer to a login / password change / step-up that has COMMITTED.

    The cookie is set first, before anything that can fail: after a rotation the
    browser's old cookie is already dead, so a failure here must not cost the new
    one (a 503 without it would sign the user out while telling them the change
    failed). Describing the session reads the policy (and, later, the Passkey
    state), which can fail on its own; then the answer degrades to the session and
    its user with ``auth: null`` instead of failing (the type of the error is
    logged, never its text).
    """
    _set_session_cookie(response, services, result)
    try:
        return _view_out(await services.service.view(result.session))
    except Exception as error:
        logger.warning(
            "The session state could not be read after a committed change (%s)",
            type(error).__name__,
        )
        return _summary_out(result.session)


def _clear_session_cookie(response: Response, services: AuthServices) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite=services.samesite,  # type: ignore[arg-type]
    )


# -- public ---------------------------------------------------------------------


@router.post(
    "/login",
    response_model=SessionResponse,
    summary="Sign in with a login name and password",
)
async def login(
    body: LoginRequest, request: Request, response: Response, services: Auth
) -> SessionResponse:
    with api_errors(credentials_status=status.HTTP_401_UNAUTHORIZED):
        result = await services.service.login(
            body.login_name,
            body.password,
            _context(request),
            remember_me=body.remember_me,
            device_label=body.device_name,
            replace_token=request.cookies.get(SESSION_COOKIE_NAME),
        )
    return await _answer_committed(response, services, result)


@router.post(
    "/token/redeem",
    response_model=RedeemResponse,
    summary="Spend an Owner setup / recovery token and set the Owner's password",
)
async def redeem_owner_token(
    body: RedeemTokenRequest, request: Request, services: Auth
) -> RedeemResponse:
    with api_errors():
        result = await services.service.redeem_owner_token(
            body.token, body.new_password, _context(request)
        )
    return RedeemResponse(
        purpose=result.purpose.value, passkey_required=result.passkey_required
    )


# -- the signed-in user -----------------------------------------------------------


@router.get(
    "/session",
    response_model=SessionResponse,
    dependencies=[
        Depends(require_capability(Capability.ACCOUNT_READ, allow_restricted=True))
    ],
    summary="The current session, its user and the authentication state",
)
async def current_session(request: Request, services: Auth) -> SessionResponse:
    with api_errors():
        return _view_out(await services.service.view(_session_of(request).session))


@router.get(
    "/sessions",
    response_model=SessionListResponse,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_READ))],
    summary="The user's signed-in devices",
)
async def list_sessions(request: Request, services: Auth) -> SessionListResponse:
    auth = _session_of(request).session
    with api_errors():
        records = await services.service.list_sessions(auth)
    return SessionListResponse(
        sessions=[_record_out(record, auth.record.id) for record in records]
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require_capability(Capability.ACCOUNT_MANAGE, allow_restricted=True))
    ],
    summary="End the current session",
)
async def logout(request: Request, response: Response, services: Auth) -> Response:
    with api_errors():
        await services.service.logout(_session_of(request).session, _context(request))
    _clear_session_cookie(response, services)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Sign one of the user's own devices out",
)
async def revoke_session(
    session_id: uuid.UUID, request: Request, response: Response, services: Auth
) -> Response:
    auth = _session_of(request).session
    with api_errors():
        await services.service.revoke_session(auth, session_id, _context(request))
    if session_id == auth.record.id:
        _clear_session_cookie(response, services)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/sessions/revoke-others",
    response_model=RevokedResponse,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Sign every other device out",
)
async def revoke_other_sessions(request: Request, services: Auth) -> RevokedResponse:
    with api_errors():
        count = await services.service.revoke_other_sessions(
            _session_of(request).session, _context(request)
        )
    return RevokedResponse(revoked=count)


@router.post(
    "/password/change",
    response_model=SessionResponse,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Change the password (needs the current one)",
)
async def change_password(
    body: ChangePasswordRequest, request: Request, response: Response, services: Auth
) -> SessionResponse:
    with api_errors():
        result = await services.service.change_password(
            _session_of(request).session,
            body.current_password,
            body.new_password,
            _context(request),
            revoke_other_sessions=body.revoke_other_sessions,
        )
    return await _answer_committed(response, services, result)


@router.post(
    "/step-up",
    response_model=SessionResponse,
    dependencies=[Depends(require_capability(Capability.ACCOUNT_MANAGE))],
    summary="Prove again that you are the account's owner (before a sensitive change)",
)
async def step_up(
    body: StepUpRequest, request: Request, response: Response, services: Auth
) -> SessionResponse:
    with api_errors():
        result = await services.service.step_up(
            _session_of(request).session,
            StepUpEvidence(body.method, body.password),
            _context(request),
        )
    return await _answer_committed(response, services, result)


# -- administration -----------------------------------------------------------------


@router.post(
    "/users/{user_id}/unlock",
    status_code=status.HTTP_204_NO_CONTENT,
    summary=(
        "Lift an account's login lock (an Admin for a User, the Owner for anyone; "
        "needs a recent Passkey step-up)"
    ),
)
async def unlock_account(
    user_id: uuid.UUID,
    request: Request,
    services: Auth,
    principal: Annotated[
        Principal, Depends(require_capability(Capability.ADMIN_USERS_MANAGE))
    ],
) -> Response:
    with api_errors():
        await services.service.unlock_account(
            principal,
            user_id,
            _context(request),
            session_id=_session_of(request).session.record.id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _policy_out(policy) -> PolicyResponse:
    return PolicyResponse(
        version=policy.version,
        passkey_owner=policy.passkey_owner.value,
        passkey_admin=policy.passkey_admin.value,
        passkey_user=policy.passkey_user.value,
        recommend_passkey_to_users=policy.recommend_passkey_to_users,
        stepup_window_minutes=policy.stepup_window_minutes,
        updated_at=policy.updated_at,
        updated_by=policy.updated_by,
    )


@router.get(
    "/policy",
    response_model=PolicyResponse,
    dependencies=[Depends(require_capability(Capability.ADMIN_AUTH_POLICY_VIEW))],
    summary="The workspace authentication policy (Admin or Owner)",
)
async def get_policy(services: Auth) -> PolicyResponse:
    with api_errors():
        return _policy_out(await services.service.policy.get())


@router.put(
    "/policy",
    response_model=PolicyResponse,
    summary="Change the workspace authentication policy (the Owner, after a step-up)",
)
async def put_policy(
    body: PolicyRequest,
    request: Request,
    services: Auth,
    principal: Annotated[
        Principal, Depends(require_capability(Capability.OWNER_AUTH_POLICY_MANAGE))
    ],
) -> PolicyResponse:
    session = _session_of(request).session
    with api_errors():
        policy = await services.service.policy.update(
            principal,
            session.record.id,
            _context(request),
            expected_version=body.expected_version,
            passkey_owner=body.passkey_owner,
            passkey_admin=body.passkey_admin,
            passkey_user=body.passkey_user,
            recommend_passkey_to_users=body.recommend_passkey_to_users,
            stepup_window_minutes=body.stepup_window_minutes,
        )
    return _policy_out(policy)
