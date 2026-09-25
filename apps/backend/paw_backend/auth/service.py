"""Login, logout, sessions, password change, Step-up, token redemption, unlock.

``AuthService`` is the whole behaviour of the authentication endpoints; the HTTP
layer (``paw_backend.api.v1.auth``) only translates. It makes no authorization
decision about *who may call it* (the routes' ``require_capability`` does, and
the operations that need more than a capability check it themselves); it decides
whether the *credentials* are right.

The rules, and where each lives:

* **No user enumeration.** A wrong password, an unknown login name, an account
  that cannot log in and an account without a password all raise the same
  ``InvalidCredentialsError`` after the same work: both counters are reserved,
  the same number of database round trips are made, and a password is verified
  against a dummy hash of the same cost when there is nothing to verify against.
  An unknown name is throttled exactly like a real one (the counter is keyed by
  the hash of the name). The remaining difference is the audit row of a known
  account's failure (a few milliseconds against ~50 ms of hashing); the timing
  is *not* fully equalised.
* **Progressive backoff.** ``Throttle`` (per account and per source), reserved
  before the password is compared.
* **Sessions.** ``SessionStore``: a new session (a new random id) at every login,
  whatever session the browser still held is revoked; the id is rotated on
  password change and Step-up.
* **Passwords.** Argon2id (``passwords``); a password change needs the current
  password; every "reset" (recovery, setup) ends all sessions.
* **Audit.** Every event is recorded (``audit``): a change commits with its
  event or not at all; a refusal is recorded on its own, best effort.

The user row is the lock of an account's credentials: whatever changes them
(password change, recovery) holds ``users`` ``FOR UPDATE``, and a login holds it
``FOR SHARE`` while it re-reads the credential and creates the session. A login
that verified the OLD password therefore cannot create a session after a reset
has ended the old ones.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth import tokens
from paw_backend.auth.audit import AuthAction, AuthAudit, AuthReason
from paw_backend.auth.auth_policy import AuthPolicyService
from paw_backend.auth.context import RequestContext
from paw_backend.auth.db import run
from paw_backend.auth.errors import (
    AccountNotFoundError,
    AuthError,
    AuthPermissionError,
    AuthUnavailableError,
    InvalidAuthInputError,
    InvalidCredentialsError,
    PasswordPolicyError,
    PasswordProblem,
    SessionEndedError,
    SessionNotFoundError,
    ThrottledError,
    TokenRejectedError,
)
from paw_backend.auth.limits import LOGIN_NAME_MAX_INPUT
from paw_backend.auth.models import AuthMethod, RevokeReason, ThrottleScope
from paw_backend.auth.passwords import (
    PasswordHasher,
    normalize_password,
    validate_new_password,
)
from paw_backend.auth.sessions import (
    AuthenticatedSession,
    SessionRecord,
    SessionStore,
    validate_device_label,
    validate_reason,
)
from paw_backend.auth.state import (
    AuthPolicy,
    AuthState,
    NoPasskeys,
    PasskeyEnrollment,
    StepUpEvidence,
    StepUpVerifier,
    build_auth_state,
)
from paw_backend.auth.throttle import Refused, Reservation, Throttle
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal
from paw_backend.db import Database
from paw_backend.identity import (
    IdentityError,
    InvalidLoginNameError,
    Redemption,
    SetupTokenRejectedError,
    TokenPurpose,
    TokenRedeemer,
    normalize_login_name,
)

logger = logging.getLogger(__name__)

# Runs in the redemption's transaction, after the password is set: PAW-023 adds
# one that revokes the user's Passkeys (Decision 0005, point 7).
CredentialInvalidator = Callable[[AsyncSession, uuid.UUID], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A successful login. ``token`` goes into the cookie and nowhere else."""

    token: str = field(repr=False)
    session: AuthenticatedSession


@dataclass(frozen=True, slots=True)
class SessionView:
    """What ``GET /auth/session`` says about a session."""

    session: AuthenticatedSession
    user_id: uuid.UUID
    auth: AuthState
    policy: AuthPolicy


@dataclass(frozen=True, slots=True)
class RedeemResult:
    """A token redeemed: the password is set and every session of the user ended."""

    purpose: TokenPurpose
    user_id: uuid.UUID
    passkey_required: bool


@dataclass(frozen=True, slots=True)
class _Account:
    id: uuid.UUID
    system_role: SystemRole
    status: str
    password_hash: str | None = field(repr=False)


class _Rejected(Exception):
    """Internal: the login ended inside its transaction for an audit-only reason."""

    def __init__(self, reason: AuthReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class PasswordStepUpVerifier:
    """Step-up by re-entering the password (the fallback until PAW-023)."""

    method = AuthMethod.PASSWORD

    def __init__(
        self, database: Database, hasher: PasswordHasher, *, timeout_seconds: float
    ) -> None:
        self._database = database
        self._hasher = hasher
        self._timeout = float(timeout_seconds)

    async def verify(
        self, user_id: uuid.UUID, login_name: str, evidence: StepUpEvidence
    ) -> bool:
        if evidence.method is not AuthMethod.PASSWORD or evidence.password is None:
            return False
        stored = await _credential_hash(self._database, user_id, self._timeout)
        if stored is None:
            await self._hasher.verify_unknown(evidence.password)
            return False
        return await self._hasher.verify(stored, evidence.password)


async def _credential_hash(
    database: Database, user_id: uuid.UUID, limit_seconds: float
) -> str | None:
    async def work(session: AsyncSession) -> str | None:
        return (
            await session.execute(
                text("SELECT hash FROM password_credentials WHERE user_id = :id"),
                {"id": user_id},
            )
        ).scalar_one_or_none()

    return await run(database, work, limit_seconds)


class AuthService:
    """The authentication behaviour. See the module docstring."""

    def __init__(
        self,
        database: Database,
        *,
        hasher: PasswordHasher,
        sessions: SessionStore,
        throttle: Throttle,
        audit: AuthAudit,
        policy: AuthPolicyService,
        redeemer: TokenRedeemer | None = None,
        passkeys: PasskeyEnrollment | None = None,
        step_up_verifiers: Mapping[AuthMethod, StepUpVerifier] | None = None,
        credential_invalidators: Sequence[CredentialInvalidator] = (),
        timeout_seconds: float = 3.0,
    ) -> None:
        for name, value, kind in (
            ("database", database, Database),
            ("hasher", hasher, PasswordHasher),
            ("sessions", sessions, SessionStore),
            ("throttle", throttle, Throttle),
            ("audit", audit, AuthAudit),
            ("policy", policy, AuthPolicyService),
        ):
            if not isinstance(value, kind):
                raise TypeError(f"{name} must be a {kind.__name__}")
        if redeemer is not None and not isinstance(redeemer, TokenRedeemer):
            raise TypeError("redeemer must be a TokenRedeemer or None")
        if passkeys is not None and not callable(
            getattr(passkeys, "is_enrolled", None)
        ):
            raise TypeError("passkeys must have an is_enrolled(user_id) method")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be in (0, 60]")
        verifiers = dict(step_up_verifiers or {})
        for method, verifier in verifiers.items():
            if not isinstance(method, AuthMethod) or not callable(
                getattr(verifier, "verify", None)
            ):
                raise TypeError("step_up_verifiers maps AuthMethod to a verifier")
        invalidators = tuple(credential_invalidators)
        if not all(callable(invalidator) for invalidator in invalidators):
            raise TypeError("credential_invalidators must be callables")
        self._database = database
        self._hasher = hasher
        self._sessions = sessions
        self._throttle = throttle
        self._audit = audit
        self._policy = policy
        self._redeemer = redeemer
        self._passkeys = passkeys or NoPasskeys()
        self._timeout = float(timeout_seconds)
        self._verifiers: dict[AuthMethod, StepUpVerifier] = {
            AuthMethod.PASSWORD: PasswordStepUpVerifier(
                database, hasher, timeout_seconds=self._timeout
            )
        }
        self._verifiers.update(verifiers)
        self._invalidators = invalidators

    @property
    def policy(self) -> AuthPolicyService:
        return self._policy

    @property
    def sessions(self) -> SessionStore:
        return self._sessions

    # -- login ------------------------------------------------------------------

    async def login(
        self,
        login_name: str,
        password: str,
        context: RequestContext,
        *,
        remember_me: bool = False,
        device_label: str | None = None,
        replace_token: str | None = None,
    ) -> LoginResult:
        """Check the credentials and start a session.

        Raises ``InvalidCredentialsError`` (always the same, whatever was wrong),
        ``ThrottledError`` (a lock: the account's or the source's) and
        ``AuthUnavailableError`` (nothing was done). ``replace_token`` is the
        session cookie the browser still holds, if any: that session ends.
        """
        _require_text("login_name", login_name, LOGIN_NAME_MAX_INPUT)
        _require_context(context)
        if not isinstance(remember_me, bool):
            raise InvalidAuthInputError("remember_me")
        device_label = validate_device_label(device_label)
        if replace_token is not None and not isinstance(replace_token, str):
            raise InvalidAuthInputError("replace_token")
        # A password that cannot be anybody's is refused before any work is done.
        candidate = _password_or_invalid(password)
        try:
            name: str | None = normalize_login_name(login_name)
        except InvalidLoginNameError:
            name = None

        source, account_reservation, account = await self._prepare_login(
            context,
            name if name is not None else tokens.raw_account_name(login_name),
            name,
        )
        reason = AuthReason.INVALID_CREDENTIALS
        verified: str | None = None
        if account is None:
            await self._hasher.verify_unknown(candidate)
        elif account.status != "active":
            reason = AuthReason.ACCOUNT_NOT_ACTIVE
            await self._hasher.verify_unknown(candidate)
        elif account.password_hash is None:
            reason = AuthReason.NO_PASSWORD
            await self._hasher.verify_unknown(candidate)
        elif await self._hasher.verify(account.password_hash, candidate):
            verified = account.password_hash

        if verified is not None and account is not None:
            rehash = (
                await self._hasher.hash(candidate)
                if self._hasher.needs_rehash(verified)
                else None
            )
            try:
                return await self._start_session(
                    account,
                    verified,
                    rehash,
                    context,
                    remember_me=remember_me,
                    device_label=device_label,
                    replace_token=replace_token,
                    reservations=(source, account_reservation),
                    login_name=name or "",
                )
            except _Rejected as rejected:
                reason = rejected.reason
        await self._login_failed(account, reason, account_reservation, context)
        raise InvalidCredentialsError

    async def _prepare_login(
        self, context: RequestContext, key_name: str, name: str | None
    ) -> tuple[Reservation, Reservation, _Account | None]:
        """Count the attempt (source, account) and look the account up: ONE transaction.

        Either lock refuses with ``ThrottledError`` (the attempts counted so far
        stay). The lookup is made whether or not the name can be one (a name that
        is not a valid login name asks the database something that finds nothing),
        so that the database work does not tell the cases apart.
        """

        async def work(session: AsyncSession):
            source = await self._throttle.reserve_in(
                session, ThrottleScope.LOGIN_SOURCE, tokens.source_key(context.source)
            )
            if isinstance(source, Refused):
                return source, None, None
            account_reservation = await self._throttle.reserve_in(
                session, ThrottleScope.LOGIN_ACCOUNT, tokens.account_key(key_name)
            )
            if isinstance(account_reservation, Refused):
                return account_reservation, None, None
            return (
                source,
                account_reservation,
                await self._load_account(session, name),
            )

        first, second, account = await run(self._database, work, self._timeout)
        if isinstance(first, Refused):
            raise ThrottledError(first.retry_after_seconds)
        return first, second, account

    async def _load_account(
        self, session: AsyncSession, login_name: str | None
    ) -> _Account | None:
        row = (
            await session.execute(
                text(
                    "SELECT u.id, u.system_role, u.status, c.hash "
                    "FROM users u LEFT JOIN password_credentials c "
                    "ON c.user_id = u.id WHERE u.login_name = :name"
                ),
                {"name": login_name if login_name is not None else ""},
            )
        ).first()
        if row is None:
            return None
        return _Account(row.id, SystemRole(row.system_role), row.status, row.hash)

    async def _reserve(
        self, context: RequestContext, name: str
    ) -> tuple[Reservation, Reservation]:
        """Count the attempt against the source and the account (either may refuse)."""
        source, account = await self._throttle.reserve_many(
            [
                (ThrottleScope.LOGIN_SOURCE, tokens.source_key(context.source)),
                (ThrottleScope.LOGIN_ACCOUNT, tokens.account_key(name)),
            ]
        )
        return source, account

    async def _start_session(
        self,
        account: _Account,
        verified_hash: str,
        rehash: str | None,
        context: RequestContext,
        *,
        remember_me: bool,
        device_label: str | None,
        replace_token: str | None,
        reservations: tuple[Reservation, Reservation],
        login_name: str,
    ) -> LoginResult:
        async def work(session: AsyncSession) -> LoginResult:
            # The account's credentials are stable while this is held (whatever
            # changes them takes the exclusive lock).
            locked = (
                await session.execute(
                    text("SELECT status FROM users WHERE id = :id FOR SHARE"),
                    {"id": account.id},
                )
            ).scalar_one_or_none()
            stored = (
                await session.execute(
                    text("SELECT hash FROM password_credentials WHERE user_id = :id"),
                    {"id": account.id},
                )
            ).scalar_one_or_none()
            if locked != "active":
                raise _Rejected(AuthReason.ACCOUNT_NOT_ACTIVE)
            if stored != verified_hash:
                raise _Rejected(AuthReason.CREDENTIALS_CHANGED)
            if replace_token is not None:
                await self._sessions.revoke_by_token(
                    session, replace_token, RevokeReason.REPLACED
                )
            if rehash is not None:
                await session.execute(
                    text(
                        "UPDATE password_credentials SET hash = :new "
                        "WHERE user_id = :id AND hash = :old"
                    ),
                    {"new": rehash, "id": account.id, "old": verified_hash},
                )
            issued = await self._sessions.create(
                session,
                account.id,
                remember_me=remember_me,
                device_label=device_label,
            )
            for reservation in reservations:
                await self._throttle.succeed_in(session, reservation)
            await self._audit.record_in(
                session,
                self._audit.event(
                    AuthAction.LOGIN,
                    AuthReason.AUTHENTICATED,
                    allowed=True,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=account.id,
                    actor_role=account.system_role,
                    resource_kind="login_source",
                    resource_id=tokens.source_audit_id(context.source),
                ),
            )
            record = issued.record
            return LoginResult(
                issued.token,
                AuthenticatedSession(
                    record=record,
                    login_name=login_name,
                    system_role=account.system_role,
                    checked_at=record.created_at,
                    token_hash=tokens.hash_session_token(issued.token),
                ),
            )

        return await run(self._database, work, self._timeout)

    async def _login_failed(
        self,
        account: _Account | None,
        reason: AuthReason,
        reservation: Reservation,
        context: RequestContext,
    ) -> None:
        """Record a failed login. Only a known account leaves a row (see ``audit``)."""
        if account is None:
            logger.info("Login refused (no such account)")
            return
        source_id = tokens.source_audit_id(context.source)
        events = [
            self._audit.event(
                AuthAction.LOGIN,
                reason,
                allowed=False,
                correlation_id=context.correlation_id,
                client_request_id=context.client_request_id,
                actor_id=account.id,
                actor_role=account.system_role,
                resource_kind="login_source",
                resource_id=source_id,
            )
        ]
        if reservation.locked_now:
            events.append(
                self._audit.event(
                    AuthAction.LOCKOUT,
                    AuthReason.BACKOFF_STARTED,
                    allowed=False,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=account.id,
                    actor_role=account.system_role,
                    resource_kind="login_source",
                    resource_id=source_id,
                )
            )
        for event in events:
            await self._audit.record_best_effort(event)

    # -- the session of the request ---------------------------------------------

    async def view(self, auth: AuthenticatedSession) -> SessionView:
        """The session, the user and the authentication state, for the client."""
        _require_auth(auth)
        policy = await self._policy.get()
        enrolled = await self._passkeys.is_enrolled(auth.record.user_id)
        return SessionView(
            session=auth,
            user_id=auth.record.user_id,
            auth=build_auth_state(
                policy,
                auth.system_role,
                auth.record,
                enrolled=enrolled,
                checked_at=auth.checked_at,
            ),
            policy=policy,
        )

    async def list_sessions(
        self, auth: AuthenticatedSession
    ) -> Sequence[SessionRecord]:
        """The user's valid sessions (their devices), most recently used first."""
        _require_auth(auth)

        async def work(session: AsyncSession) -> Sequence[SessionRecord]:
            return await self._sessions.list_active(session, auth.record.user_id)

        return await run(self._database, work, self._timeout)

    async def logout(self, auth: AuthenticatedSession, context: RequestContext) -> None:
        """End the session of the request."""
        _require_auth(auth)
        _require_context(context)

        async def work(session: AsyncSession) -> None:
            ended = await self._sessions.revoke(
                session, auth.record.id, auth.record.user_id, RevokeReason.LOGOUT
            )
            if ended:
                await self._audit.record_in(
                    session,
                    self._session_event(
                        AuthAction.LOGOUT,
                        AuthReason.LOGGED_OUT,
                        auth,
                        context,
                        auth.record.id,
                    ),
                )

        await run(self._database, work, self._timeout)

    async def revoke_session(
        self, auth: AuthenticatedSession, session_id: uuid.UUID, context: RequestContext
    ) -> None:
        """End one of the user's own sessions; ``SessionNotFoundError`` if none."""
        _require_auth(auth)
        _require_context(context)
        if not isinstance(session_id, uuid.UUID):
            raise InvalidAuthInputError("session_id")

        async def work(session: AsyncSession) -> None:
            ended = await self._sessions.revoke(
                session, session_id, auth.record.user_id, RevokeReason.REVOKED_BY_USER
            )
            if not ended:
                raise SessionNotFoundError
            await self._audit.record_in(
                session,
                self._session_event(
                    AuthAction.SESSION_REVOKE,
                    AuthReason.REVOKED,
                    auth,
                    context,
                    session_id,
                ),
            )

        await run(self._database, work, self._timeout)

    async def revoke_other_sessions(
        self, auth: AuthenticatedSession, context: RequestContext
    ) -> int:
        """End every other session of the user; how many ended."""
        _require_auth(auth)
        _require_context(context)

        async def work(session: AsyncSession) -> int:
            count = await self._sessions.revoke_all(
                session,
                auth.record.user_id,
                RevokeReason.LOGOUT_OTHERS,
                except_id=auth.record.id,
            )
            await self._audit.record_in(
                session,
                self._user_event(
                    AuthAction.SESSION_REVOKE_OTHERS,
                    AuthReason.REVOKED_OTHERS,
                    auth,
                    context,
                ),
            )
            return count

        return await run(self._database, work, self._timeout)

    async def revoke_all_sessions_of(
        self, user_id: uuid.UUID, reason: RevokeReason
    ) -> int:
        """End every session of a user (for the code that closes an account).

        A user who becomes ``pending_deletion`` or ``deleted`` no longer resolves
        to a principal at all (the lookup requires ``active``); this also ends
        the rows, so that they cannot come back to life if the user is restored.
        """
        if not isinstance(user_id, uuid.UUID):
            raise InvalidAuthInputError("user_id")
        reason = validate_reason(reason)

        async def work(session: AsyncSession) -> int:
            return await self._sessions.revoke_all(session, user_id, reason)

        return await run(self._database, work, self._timeout)

    # -- password change and Step-up ---------------------------------------------

    async def change_password(
        self,
        auth: AuthenticatedSession,
        current_password: str,
        new_password: str,
        context: RequestContext,
        *,
        revoke_other_sessions: bool = False,
    ) -> LoginResult:
        """Set a new password (needs the current one); the session's id is rotated.

        By default the user's other sessions stay (REQUIREMENTS.md); with
        ``revoke_other_sessions`` they all end. Returns the session with its NEW
        id (the client must replace its cookie).
        """
        _require_auth(auth)
        _require_context(context)
        if not isinstance(revoke_other_sessions, bool):
            raise InvalidAuthInputError("revoke_other_sessions")
        current = _password_or_invalid(current_password)
        candidate = validate_new_password(new_password, auth.login_name)
        if candidate == current:
            raise PasswordPolicyError(PasswordProblem.SAME_AS_CURRENT)
        reservations, stored = await self._check_current_password(
            auth, current, context, AuthAction.PASSWORD_CHANGE
        )
        new_hash = await self._hasher.hash(candidate)

        async def work(session: AsyncSession) -> LoginResult:
            await self._lock_credentials(session, auth.record.user_id, stored)
            await session.execute(
                text(
                    "UPDATE password_credentials SET hash = :hash, changed_at = :now "
                    "WHERE user_id = :id"
                ),
                {
                    "hash": new_hash,
                    "now": self._audit.now(),
                    "id": auth.record.user_id,
                },
            )
            revoked = 0
            if revoke_other_sessions:
                revoked = await self._sessions.revoke_all(
                    session,
                    auth.record.user_id,
                    RevokeReason.PASSWORD_CHANGED,
                    except_id=auth.record.id,
                )
            token = await self._sessions.rotate(
                session, auth.record.id, auth.token_hash
            )
            if token is None:
                raise SessionEndedError
            for reservation in reservations:
                await self._throttle.succeed_in(session, reservation)
            await self._audit.record_in(
                session,
                self._user_event(
                    AuthAction.PASSWORD_CHANGE, AuthReason.CHANGED, auth, context
                ),
            )
            if revoked:
                await self._audit.record_in(
                    session,
                    self._user_event(
                        AuthAction.SESSION_REVOKE_OTHERS,
                        AuthReason.REVOKED_OTHERS,
                        auth,
                        context,
                    ),
                )
            return LoginResult(token, replace_token_of(auth, token))

        return await run(self._database, work, self._timeout)

    async def step_up(
        self,
        auth: AuthenticatedSession,
        evidence: StepUpEvidence,
        context: RequestContext,
    ) -> LoginResult:
        """Prove again that the user is the account's owner; the id is rotated.

        The verifier of ``evidence.method`` decides (password now; PAW-023
        registers a Passkey one). A wrong proof counts against the account like a
        wrong password does.
        """
        _require_auth(auth)
        _require_context(context)
        if not isinstance(evidence, StepUpEvidence):
            raise InvalidAuthInputError("evidence")
        verifier = self._verifiers.get(evidence.method)
        if verifier is None:
            raise InvalidAuthInputError("method")
        if evidence.password is not None:
            evidence = StepUpEvidence(
                evidence.method, _password_or_invalid(evidence.password)
            )
        reservations = await self._reserve(context, auth.login_name)
        if not await verifier.verify(auth.record.user_id, auth.login_name, evidence):
            await self._record_refusal(
                auth,
                context,
                AuthAction.STEP_UP,
                AuthReason.INVALID_CREDENTIALS,
                reservations[1],
            )
            raise InvalidCredentialsError

        async def work(session: AsyncSession) -> LoginResult:
            issued = await self._sessions.record_step_up(
                session, auth.record.id, auth.token_hash, evidence.method
            )
            if issued is None:
                raise SessionEndedError
            for reservation in reservations:
                await self._throttle.succeed_in(session, reservation)
            await self._audit.record_in(
                session,
                self._session_event(
                    AuthAction.STEP_UP,
                    AuthReason.VERIFIED,
                    auth,
                    context,
                    auth.record.id,
                ),
            )
            return LoginResult(
                issued.token,
                replace(
                    auth,
                    record=issued.record,
                    checked_at=issued.record.stepup_at,
                    token_hash=tokens.hash_session_token(issued.token),
                ),
            )

        return await run(self._database, work, self._timeout)

    async def _check_current_password(
        self,
        auth: AuthenticatedSession,
        password: str,
        context: RequestContext,
        action: AuthAction,
    ) -> tuple[tuple[Reservation, Reservation], str]:
        reservations = await self._reserve(context, auth.login_name)
        stored = await _credential_hash(
            self._database, auth.record.user_id, self._timeout
        )
        if stored is None:
            await self._hasher.verify_unknown(password)
            matches = False
        else:
            matches = await self._hasher.verify(stored, password)
        if not matches or stored is None:
            await self._record_refusal(
                auth, context, action, AuthReason.INVALID_CREDENTIALS, reservations[1]
            )
            raise InvalidCredentialsError
        return reservations, stored

    async def _lock_credentials(
        self, session: AsyncSession, user_id: uuid.UUID, expected_hash: str
    ) -> None:
        """Lock the account's credentials and check they are still what was verified."""
        status = (
            await session.execute(
                text("SELECT status FROM users WHERE id = :id FOR UPDATE"),
                {"id": user_id},
            )
        ).scalar_one_or_none()
        stored = (
            await session.execute(
                text("SELECT hash FROM password_credentials WHERE user_id = :id"),
                {"id": user_id},
            )
        ).scalar_one_or_none()
        if status != "active" or stored != expected_hash:
            raise InvalidCredentialsError

    async def _record_refusal(
        self,
        auth: AuthenticatedSession,
        context: RequestContext,
        action: AuthAction,
        reason: AuthReason,
        reservation: Reservation,
    ) -> None:
        events = [self._user_event(action, reason, auth, context, allowed=False)]
        if reservation.locked_now:
            events.append(
                self._user_event(
                    AuthAction.LOCKOUT,
                    AuthReason.BACKOFF_STARTED,
                    auth,
                    context,
                    allowed=False,
                )
            )
        for event in events:
            await self._audit.record_best_effort(event)

    # -- the Owner's setup / recovery token ---------------------------------------

    async def redeem_owner_token(
        self, token: str, new_password: str, context: RequestContext
    ) -> RedeemResult:
        """Spend an Owner setup / recovery token and set the Owner's password.

        Public: rate limited per source and in total BEFORE anything else
        (Decision 0005), then the password is checked and hashed, then the token
        is redeemed and, in the same transaction, the password is set (replacing
        whatever was there: a recovery invalidates the old one), **every session
        of the user ends**, the login lock is cleared, credential invalidators
        run (PAW-023: Passkeys) and an invited user becomes active.

        ``TokenRejectedError`` is the one answer for every token that is
        not acceptable; ``PasswordPolicyError`` refuses the password (before the
        token is spent whenever it can be told from the password alone).
        """
        if self._redeemer is None:
            raise AuthUnavailableError
        _require_text("token", token, 512)
        _require_context(context)
        if not isinstance(new_password, str):
            raise InvalidAuthInputError("new_password")
        await self._throttle.reserve_many(
            [
                (ThrottleScope.REDEEM_SOURCE, tokens.source_key(context.source)),
                (ThrottleScope.REDEEM_GLOBAL, tokens.GLOBAL_KEY),
            ]
        )
        candidate = validate_new_password(new_password)
        new_hash = await self._hasher.hash(candidate)
        result: dict[str, RedeemResult] = {}

        async def apply(session: AsyncSession, redemption: Redemption) -> None:
            result["value"] = await self._set_password_of_redemption(
                session, redemption, candidate, new_hash, context
            )

        try:
            await self._redeemer.redeem(token, apply=apply)
        except SetupTokenRejectedError:
            raise TokenRejectedError from None
        except (AuthError, IdentityError):
            raise
        except Exception as error:
            logger.error("Token redemption failed (%s)", type(error).__name__)
            raise AuthUnavailableError from None
        return result["value"]

    async def _set_password_of_redemption(
        self,
        session: AsyncSession,
        redemption: Redemption,
        candidate: str,
        new_hash: str,
        context: RequestContext,
    ) -> RedeemResult:
        now = self._audit.now()
        user_id = redemption.user_id
        login_name = (
            await session.execute(
                text("SELECT login_name FROM users WHERE id = :id"), {"id": user_id}
            )
        ).scalar_one()
        # The login name is only known now: a password that is the name is
        # refused here (the token stays unspent: the transaction rolls back).
        validate_new_password(candidate, login_name)
        await session.execute(
            text(
                "INSERT INTO password_credentials "
                "(user_id, hash, created_at, changed_at) "
                "VALUES (:id, :hash, :now, :now) "
                "ON CONFLICT (user_id) DO UPDATE "
                "SET hash = EXCLUDED.hash, changed_at = EXCLUDED.changed_at"
            ),
            {"id": user_id, "hash": new_hash, "now": now},
        )
        recovery = redemption.purpose is TokenPurpose.RECOVERY
        revoked = await self._sessions.revoke_all(
            session,
            user_id,
            RevokeReason.RECOVERY if recovery else RevokeReason.PASSWORD_RESET,
        )
        await self._throttle.reset_in(
            session, ThrottleScope.LOGIN_ACCOUNT, tokens.account_key(login_name)
        )
        for invalidate in self._invalidators:
            await invalidate(session, user_id)
        await session.execute(
            text("SELECT paw_activate_invited_user(:id, :now)"),
            {"id": user_id, "now": now},
        )
        policy = await self._policy.get_in(session)
        reason = AuthReason.RECOVERY if recovery else AuthReason.SETUP
        await self._audit.record_in(
            session,
            self._audit.event(
                AuthAction.PASSWORD_SET,
                reason,
                allowed=True,
                correlation_id=context.correlation_id,
                client_request_id=context.client_request_id,
                actor_id=user_id,
                actor_role=SystemRole.OWNER,
                resource_kind="user",
                resource_id=user_id,
            ),
        )
        if revoked:
            await self._audit.record_in(
                session,
                self._audit.event(
                    AuthAction.SESSION_REVOKE_ALL,
                    AuthReason.REVOKED_ALL,
                    allowed=True,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=user_id,
                    actor_role=SystemRole.OWNER,
                    resource_kind="user",
                    resource_id=user_id,
                ),
            )
        return RedeemResult(
            purpose=redemption.purpose,
            user_id=user_id,
            passkey_required=(
                policy.requirement_for(SystemRole.OWNER).value == "required"
            ),
        )

    # -- an administrator's unlock --------------------------------------------------

    async def unlock_account(
        self, actor: Principal, target_user_id: uuid.UUID, context: RequestContext
    ) -> None:
        """Lift the login lock of an account (REQUIREMENTS.md: Owner / Admin).

        An Admin may unlock a User; unlocking an Admin or the Owner is the
        Owner's. Only the account's counter is cleared (a lock of a *source* is
        not the account's). A lock also ends by itself: it is never permanent.
        """
        if not isinstance(actor, Principal):
            raise InvalidAuthInputError("actor")
        if not isinstance(target_user_id, uuid.UUID):
            raise InvalidAuthInputError("target_user_id")
        _require_context(context)
        if actor.system_role not in (SystemRole.OWNER, SystemRole.ADMIN):
            await self._deny_unlock(actor, target_user_id, context)
            raise AuthPermissionError
        refused = False

        async def work(session: AsyncSession) -> None:
            nonlocal refused
            row = (
                await session.execute(
                    text(
                        "SELECT login_name, system_role FROM users "
                        "WHERE id = :id AND status IN ('invited', 'active')"
                    ),
                    {"id": target_user_id},
                )
            ).first()
            if row is None:
                raise AccountNotFoundError
            if row.system_role != SystemRole.USER.value and (
                actor.system_role is not SystemRole.OWNER
            ):
                refused = True
                raise AuthPermissionError
            await self._throttle.reset_in(
                session, ThrottleScope.LOGIN_ACCOUNT, tokens.account_key(row.login_name)
            )
            await self._audit.record_in(
                session,
                self._audit.event(
                    AuthAction.UNLOCK,
                    AuthReason.UNLOCKED,
                    allowed=True,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=actor.user_id,
                    actor_role=actor.system_role,
                    resource_kind="user",
                    resource_id=target_user_id,
                ),
            )

        try:
            await run(self._database, work, self._timeout)
        except AuthPermissionError:
            if refused:
                await self._deny_unlock(actor, target_user_id, context)
            raise

    async def _deny_unlock(
        self, actor: Principal, target_user_id: uuid.UUID, context: RequestContext
    ) -> None:
        await self._audit.record_best_effort(
            self._audit.event(
                AuthAction.UNLOCK,
                AuthReason.ROLE_NOT_ALLOWED,
                allowed=False,
                correlation_id=context.correlation_id,
                client_request_id=context.client_request_id,
                actor_id=actor.user_id,
                actor_role=actor.system_role,
                resource_kind="user",
                resource_id=target_user_id,
            )
        )

    # -- events -------------------------------------------------------------------

    def _user_event(
        self,
        action: AuthAction,
        reason: AuthReason,
        auth: AuthenticatedSession,
        context: RequestContext,
        *,
        allowed: bool = True,
    ):
        return self._audit.event(
            action,
            reason,
            allowed=allowed,
            correlation_id=context.correlation_id,
            client_request_id=context.client_request_id,
            actor_id=auth.record.user_id,
            actor_role=auth.system_role,
            resource_kind="user",
            resource_id=auth.record.user_id,
        )

    def _session_event(
        self,
        action: AuthAction,
        reason: AuthReason,
        auth: AuthenticatedSession,
        context: RequestContext,
        session_id: uuid.UUID,
    ):
        return self._audit.event(
            action,
            reason,
            allowed=True,
            correlation_id=context.correlation_id,
            client_request_id=context.client_request_id,
            actor_id=auth.record.user_id,
            actor_role=auth.system_role,
            resource_kind="session",
            resource_id=session_id,
        )


def replace_token_of(auth: AuthenticatedSession, token: str) -> AuthenticatedSession:
    """``auth`` after a rotation: the same session under its new id."""
    return replace(auth, token_hash=tokens.hash_session_token(token))


def _require_text(name: str, value: object, max_length: int) -> None:
    if not isinstance(value, str) or len(value) > max_length:
        raise InvalidAuthInputError(name)


def _require_auth(auth: object) -> AuthenticatedSession:
    if not isinstance(auth, AuthenticatedSession):
        raise InvalidAuthInputError("auth")
    return auth


def _require_context(context: object) -> RequestContext:
    if not isinstance(context, RequestContext):
        raise InvalidAuthInputError("context")
    return context


def _password_or_invalid(password: object) -> str:
    """The normalised password of a *proof*.

    Not a string: a caller error. A string that cannot be anybody's password
    (refused characters, far too long) is simply the wrong password.
    """
    if not isinstance(password, str):
        raise InvalidAuthInputError("password")
    try:
        return normalize_password(password)
    except PasswordPolicyError:
        raise InvalidCredentialsError from None
