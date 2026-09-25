"""Registering, listing and revoking Passkeys; the Passkey Step-up verifier.

The rules (Decision 0025), and where each lives:

* **Who may register.** A session that is not waiting for its own Passkey
  authentication (``ASSERTION_REQUIRED`` with a Passkey registered: that session may
  only authenticate). A user who already has a Passkey needs a recent Passkey Step-up
  to add another (a stolen password or cookie must not be able to add a credential
  of the thief's); a user with none needs a recent authentication of any kind (the
  sign-in itself counts: an enrolment-only session that has just been created).
  Checked again under the user's row lock when the credential is stored.
* **What a registration opens.** The first Passkey registered by a restricted
  session lifts its gate and rotates its id, but is NOT recorded as a Step-up: a
  credential that a password just vouched for proves nothing yet.
* **Revoking.** Needs a recent Passkey Step-up when the role's requirement is
  ``required`` (any Step-up otherwise). The last Passkey of a role that requires
  one cannot be revoked (it would turn a Passkey-protected account into a
  password-only one; register the replacement first). A revoked Passkey ends the
  sessions it opened (``revoked_reason = passkey_revoked``), forgets every Passkey
  Step-up of the user's sessions and their open challenges. A session that was
  signed in with the Passkey being revoked therefore ends, the caller's own included.
* **Races.** The user's row is locked ``FOR UPDATE`` for a registration and a
  revocation (they cannot interleave, and the limit and the last-Passkey rule see
  one consistent count), a sign-in holds it ``FOR SHARE``. A Passkey used for a
  Step-up is confirmed ``FOR SHARE`` in the recording transaction.
* **Audit.** Every registration, revocation and refusal is a row
  (``auth.passkey.register``, ``auth.passkey.revoke``, ``auth.passkey.authenticate``)
  with an enum reason; a change commits with its row, a refusal is written on its
  own, best effort. No credential id, key, challenge or name is ever recorded.
* **Throttle.** A registration attempt counts against the account and the source like
  a wrong password (``Throttle``); a success resets the account's count.
"""

import logging
import secrets
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth import tokens
from paw_backend.auth.audit import AuthAction, AuthAudit, AuthReason
from paw_backend.auth.auth_policy import AuthPolicyService
from paw_backend.auth.context import RequestContext
from paw_backend.auth.db import run
from paw_backend.auth.errors import (
    InvalidAuthInputError,
    LastPasskeyError,
    NoPasskeyError,
    PasskeyChallengeError,
    PasskeyExistsError,
    PasskeyLimitError,
    PasskeyNotFoundError,
    PasskeyRequiredError,
    PasskeyUnavailableError,
    PasskeyVerificationError,
    SessionEndedError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.auth.models import (
    AuthMethod,
    PasskeyGate,
    PasskeyRequirement,
    ThrottleScope,
)
from paw_backend.auth.passkeys import ceremony
from paw_backend.auth.passkeys.config import PasskeyConfig
from paw_backend.auth.passkeys.models import (
    CHALLENGE_BYTES,
    MAX_PASSKEYS_PER_USER,
    PasskeyPurpose,
    PasskeyRevokeReason,
)
from paw_backend.auth.passkeys.store import PasskeyRecord, PasskeyRegistry, UseOutcome
from paw_backend.auth.passkeys.types import parse_registration_credential
from paw_backend.auth.service import LoginResult
from paw_backend.auth.sessions import (
    AuthenticatedSession,
    SessionStore,
    validate_device_label,
)
from paw_backend.auth.state import StepUpEvidence, StepUpRefused
from paw_backend.auth.stepup import Freshness, read_freshness_in
from paw_backend.auth.throttle import Reservation, Throttle
from paw_backend.db import Database

logger = logging.getLogger(__name__)

DEFAULT_PASSKEY_NAME = "Passkey"


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """A Passkey was registered. ``login`` is set when the session's id was rotated
    (the registration lifted the session's gate): the client must replace its cookie."""

    passkey: PasskeyRecord
    login: LoginResult | None


@dataclass(frozen=True, slots=True)
class RevocationResult:
    sessions_ended: int
    # The session of this request ended too (it was signed in with this Passkey).
    current_session_ended: bool


def _require_auth(value: object) -> AuthenticatedSession:
    if not isinstance(value, AuthenticatedSession):
        raise InvalidAuthInputError("auth")
    return value


def _require_context(value: object) -> RequestContext:
    if not isinstance(value, RequestContext):
        raise InvalidAuthInputError("context")
    return value


class PasskeyService:
    """Passkey registration, listing and revocation for the signed-in user."""

    def __init__(
        self,
        database: Database,
        registry: PasskeyRegistry,
        sessions: SessionStore,
        throttle: Throttle,
        audit: AuthAudit,
        policy: AuthPolicyService,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        for name, value, kind in (
            ("database", database, Database),
            ("registry", registry, PasskeyRegistry),
            ("sessions", sessions, SessionStore),
            ("throttle", throttle, Throttle),
            ("audit", audit, AuthAudit),
            ("policy", policy, AuthPolicyService),
        ):
            if not isinstance(value, kind):
                raise TypeError(f"{name} must be a {kind.__name__}")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be in (0, 60]")
        self._database = database
        self._registry = registry
        self._sessions = sessions
        self._throttle = throttle
        self._audit = audit
        self._policy = policy
        self._timeout = float(timeout_seconds)

    @property
    def available(self) -> bool:
        return self._registry.available

    def _config(self) -> PasskeyConfig:
        config = self._registry.config
        if config is None:
            raise PasskeyUnavailableError
        return config

    # -- registration -----------------------------------------------------------------

    async def register_begin(
        self, auth: AuthenticatedSession, context: RequestContext
    ) -> dict:
        """The options for ``navigator.credentials.create()``; stores the challenge."""
        _require_auth(auth)
        _require_context(context)
        config = self._config()
        user_id = auth.record.user_id

        async def work(session: AsyncSession) -> tuple[bytes, list[bytes]]:
            await self._require_may_register_in(session, auth)
            challenge = secrets.token_bytes(CHALLENGE_BYTES)
            await self._registry.issue_challenge_in(
                session,
                user_id=user_id,
                session_id=auth.record.id,
                purpose=PasskeyPurpose.REGISTER,
                challenge=challenge,
            )
            exclude = await self._registry.active_credential_ids_in(session, user_id)
            return challenge, exclude

        try:
            challenge, exclude = await run(self._database, work, self._timeout)
        except (
            PasskeyRequiredError,
            PasskeyLimitError,
            StepUpRequiredError,
        ) as error:
            await self._deny(auth, context, AuthAction.PASSKEY_REGISTER, _reason(error))
            raise
        return ceremony.registration_options(
            config,
            user_id=user_id,
            login_name=auth.login_name,
            challenge=challenge,
            exclude=exclude,
        )

    async def register_finish(
        self,
        auth: AuthenticatedSession,
        credential: object,
        name: str | None,
        context: RequestContext,
    ) -> RegistrationResult:
        """Verify the browser's answer and store the credential.

        Raises ``PasskeyChallengeError`` (no live challenge: begin again),
        ``PasskeyVerificationError`` (the answer did not verify),
        ``PasskeyExistsError`` (that credential is registered), the gate / Step-up /
        limit errors of ``register_begin``, ``ThrottledError`` and
        ``AuthUnavailableError``.
        """
        _require_auth(auth)
        _require_context(context)
        config = self._config()
        checked = parse_registration_credential(credential)
        label = validate_device_label(name) or DEFAULT_PASSKEY_NAME
        user_id = auth.record.user_id
        reservations = await self._reserve(context, auth.login_name)

        async def take(session: AsyncSession) -> bytes | None:
            return await self._registry.consume_challenge_in(
                session,
                user_id=user_id,
                session_id=auth.record.id,
                purpose=PasskeyPurpose.REGISTER,
            )

        challenge = await run(self._database, take, self._timeout)
        if challenge is None:
            await self._deny(
                auth,
                context,
                AuthAction.PASSKEY_REGISTER,
                AuthReason.CHALLENGE_INVALID,
                reservations[1],
            )
            raise PasskeyChallengeError
        try:
            verified = ceremony.verify_registration(config, checked, challenge)
        except ceremony.CeremonyRejected:
            await self._deny(
                auth,
                context,
                AuthAction.PASSKEY_REGISTER,
                AuthReason.VERIFICATION_FAILED,
                reservations[1],
            )
            raise PasskeyVerificationError from None

        async def store(session: AsyncSession) -> RegistrationResult:
            await self._lock_user_in(session, user_id)
            freshness = await self._require_may_register_in(session, auth)
            record = await self._registry.insert_in(
                session, user_id=user_id, name=label, registration=verified
            )
            if record is None:
                raise PasskeyExistsError
            login: LoginResult | None = None
            if freshness.gate is not PasskeyGate.OPEN:
                issued = await self._sessions.open_gate(
                    session, auth.record.id, auth.token_hash, record.id
                )
                if issued is None:
                    raise SessionEndedError
                token_hash = tokens.hash_session_token(issued.token)
                login = LoginResult(
                    issued.token,
                    replace(auth, record=issued.record, token_hash=token_hash),
                )
            for reservation in reservations:
                await self._throttle.succeed_in(session, reservation)
            await self._audit.record_in(
                session,
                self._audit.event(
                    AuthAction.PASSKEY_REGISTER,
                    AuthReason.REGISTERED,
                    allowed=True,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=user_id,
                    actor_role=auth.system_role,
                    resource_kind="passkey",
                    resource_id=record.id,
                ),
            )
            return RegistrationResult(record, login)

        try:
            return await run(self._database, store, self._timeout)
        except (
            PasskeyRequiredError,
            PasskeyLimitError,
            StepUpRequiredError,
            PasskeyExistsError,
        ) as error:
            await self._deny(
                auth,
                context,
                AuthAction.PASSKEY_REGISTER,
                _reason(error),
                reservations[1],
            )
            raise

    # -- authentication (Step-up / the second half of a restricted sign-in) ----

    async def authenticate_begin(
        self, auth: AuthenticatedSession, context: RequestContext
    ) -> dict:
        """The options for ``navigator.credentials.get()`` over the user's Passkeys."""
        _require_auth(auth)
        _require_context(context)
        config = self._config()
        user_id = auth.record.user_id

        async def work(session: AsyncSession) -> tuple[bytes, list[bytes]]:
            allow = await self._registry.active_credential_ids_in(session, user_id)
            if not allow:
                raise NoPasskeyError
            challenge = secrets.token_bytes(CHALLENGE_BYTES)
            await self._registry.issue_challenge_in(
                session,
                user_id=user_id,
                session_id=auth.record.id,
                purpose=PasskeyPurpose.AUTHENTICATE,
                challenge=challenge,
            )
            return challenge, allow

        challenge, allow = await run(self._database, work, self._timeout)
        return ceremony.authentication_options(config, challenge=challenge, allow=allow)

    # -- the list and revocation -------------------------------------------------

    async def list_passkeys(self, auth: AuthenticatedSession) -> list[PasskeyRecord]:
        """The user's active Passkeys (their devices), oldest first."""
        _require_auth(auth)

        async def work(session: AsyncSession) -> list[PasskeyRecord]:
            return await self._registry.list_active_in(session, auth.record.user_id)

        return await run(self._database, work, self._timeout)

    async def revoke(
        self,
        auth: AuthenticatedSession,
        passkey_id: uuid.UUID,
        context: RequestContext,
    ) -> RevocationResult:
        """Revoke one of the user's Passkeys (see the module docstring)."""
        _require_auth(auth)
        _require_context(context)
        if not isinstance(passkey_id, uuid.UUID):
            raise InvalidAuthInputError("passkey_id")
        user_id = auth.record.user_id

        async def work(session: AsyncSession) -> RevocationResult:
            await self._lock_user_in(session, user_id)
            # The credential before any session (see ``lock_active_in``); whether it
            # exists is only told after the caller's right to ask has been judged.
            found = await self._registry.lock_active_in(
                session, passkey_id=passkey_id, user_id=user_id
            )
            policy = await self._policy.get_in(session)
            freshness = await read_freshness_in(
                session,
                session_id=auth.record.id,
                user_id=user_id,
                window_minutes=policy.stepup_window_minutes,
                now=self._audit.now(),
            )
            if freshness is None:
                raise SessionEndedError
            if freshness.gate is not PasskeyGate.OPEN:
                raise PasskeyRequiredError
            required = (
                policy.requirement_for(auth.system_role) is PasskeyRequirement.REQUIRED
            )
            if required:
                _require_passkey_step_up(freshness)
            elif freshness.step_up is None:
                raise StepUpRequiredError
            if not found:
                raise PasskeyNotFoundError
            revoked = await self._registry.revoke_in(
                session,
                passkey_id=passkey_id,
                user_id=user_id,
                reason=PasskeyRevokeReason.REVOKED_BY_USER,
            )
            if not revoked:  # (cannot happen: the row is locked)
                raise PasskeyNotFoundError
            if required and await self._registry.count_active_in(session, user_id) == 0:
                # Rolled back with everything above: the credential stays active.
                raise LastPasskeyError
            ended = await self._sessions.revoke_bound_to_passkey(session, passkey_id)
            await self._sessions.clear_passkey_step_ups(session, user_id)
            await self._registry.delete_challenges_of_in(session, user_id)
            await self._audit.record_in(
                session,
                self._audit.event(
                    AuthAction.PASSKEY_REVOKE,
                    AuthReason.REVOKED,
                    allowed=True,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=user_id,
                    actor_role=auth.system_role,
                    resource_kind="passkey",
                    resource_id=passkey_id,
                ),
            )
            return RevocationResult(len(ended), auth.record.id in ended)

        try:
            return await run(self._database, work, self._timeout)
        except (
            PasskeyRequiredError,
            PasskeyNotFoundError,
            LastPasskeyError,
            StepUpRequiredError,
        ) as error:
            await self._deny(
                auth,
                context,
                AuthAction.PASSKEY_REVOKE,
                _reason(error),
                resource_id=passkey_id,
            )
            raise

    # -- shared ------------------------------------------------------------------

    async def _lock_user_in(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        """Lock the user's row for the rest of the transaction; it must be active."""
        status = (
            await session.execute(
                text("SELECT status FROM users WHERE id = :id FOR UPDATE"),
                {"id": user_id},
            )
        ).scalar_one_or_none()
        if status != "active":
            raise SessionEndedError

    async def _require_may_register_in(
        self, session: AsyncSession, auth: AuthenticatedSession
    ) -> Freshness:
        """The registration rules (see the module docstring); the session's state."""
        user_id = auth.record.user_id
        policy = await self._policy.get_in(session)
        freshness = await read_freshness_in(
            session,
            session_id=auth.record.id,
            user_id=user_id,
            window_minutes=policy.stepup_window_minutes,
            now=self._audit.now(),
        )
        if freshness is None:
            raise SessionEndedError
        count = await self._registry.count_active_in(session, user_id)
        gate = freshness.gate
        if gate is PasskeyGate.ASSERTION_REQUIRED and count == 0:
            # The Passkeys it was waiting for are gone (revoked since sign-in): the
            # session may enrol again, it is not stuck.
            gate = PasskeyGate.ENROLLMENT_REQUIRED
            freshness = replace(freshness, gate=gate)
        if gate is PasskeyGate.ASSERTION_REQUIRED:
            raise PasskeyRequiredError  # authenticate first
        if count >= MAX_PASSKEYS_PER_USER:
            raise PasskeyLimitError
        if count > 0:
            _require_passkey_step_up(freshness)
        elif not freshness.recently_authenticated:
            raise StepUpRequiredError
        return freshness

    async def _reserve(
        self, context: RequestContext, login_name: str
    ) -> tuple[Reservation, Reservation]:
        source, account = await self._throttle.reserve_many(
            [
                (ThrottleScope.LOGIN_SOURCE, tokens.source_key(context.source)),
                (ThrottleScope.LOGIN_ACCOUNT, tokens.account_key(login_name)),
            ]
        )
        return source, account

    async def _deny(
        self,
        auth: AuthenticatedSession,
        context: RequestContext,
        action: AuthAction,
        reason: AuthReason,
        reservation: Reservation | None = None,
        *,
        resource_id: uuid.UUID | None = None,
    ) -> None:
        events = [
            self._audit.event(
                action,
                reason,
                allowed=False,
                correlation_id=context.correlation_id,
                client_request_id=context.client_request_id,
                actor_id=auth.record.user_id,
                actor_role=auth.system_role,
                resource_kind="passkey" if resource_id else "user",
                resource_id=resource_id or auth.record.user_id,
            )
        ]
        if reservation is not None and reservation.locked_now:
            events.append(
                self._audit.event(
                    AuthAction.LOCKOUT,
                    AuthReason.BACKOFF_STARTED,
                    allowed=False,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=auth.record.user_id,
                    actor_role=auth.system_role,
                    resource_kind="user",
                    resource_id=auth.record.user_id,
                )
            )
        for event in events:
            await self._audit.record_best_effort(event)


def _require_passkey_step_up(freshness: Freshness) -> None:
    if freshness.step_up is None:
        raise StepUpRequiredError
    if freshness.step_up is not AuthMethod.PASSKEY:
        raise StepUpMethodInsufficientError


def _reason(error: Exception) -> AuthReason:
    """The audit reason of a refusal (an enum; the error carries nothing else)."""
    if isinstance(error, StepUpMethodInsufficientError):
        return AuthReason.STEP_UP_METHOD_INSUFFICIENT
    if isinstance(error, StepUpRequiredError):
        return AuthReason.STEP_UP_REQUIRED
    if isinstance(error, PasskeyRequiredError):
        return AuthReason.GATE_NOT_ALLOWED
    if isinstance(error, PasskeyLimitError):
        return AuthReason.LIMIT_REACHED
    if isinstance(error, PasskeyExistsError):
        return AuthReason.ALREADY_REGISTERED
    if isinstance(error, LastPasskeyError):
        return AuthReason.LAST_PASSKEY
    if isinstance(error, PasskeyNotFoundError):
        return AuthReason.NOT_FOUND
    return AuthReason.VERIFICATION_FAILED


class PasskeyStepUpVerifier:
    """The ``StepUpVerifier`` of the Passkey method (``AuthMethod.PASSKEY``).

    ``verify`` checks a WebAuthn assertion: the session's authentication challenge
    (single use, consumed FIRST, at the database's clock), the credential (an active
    one of the user), the signature, origin, RP ID and user verification
    (``ceremony``), and the signature counter (stored atomically with the check). It
    answers ``True`` only for an assertion that passed all of it; it raises
    ``StepUpRefused`` with the reason otherwise, so the audit row says why (a counter
    that did not go up is the clone-detection signal, logged as a warning too).

    ``AuthService.step_up`` then records the Step-up in the session and confirms the
    credential is still active in THAT transaction (a Passkey revoked in between
    cannot step anything up).
    """

    method = AuthMethod.PASSKEY

    def __init__(
        self, database: Database, registry: PasskeyRegistry, *, timeout_seconds: float
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(registry, PasskeyRegistry) or not registry.available:
            raise TypeError("registry must be a configured PasskeyRegistry")
        self._database = database
        self._registry = registry
        self._timeout = float(timeout_seconds)

    async def verify(
        self, user_id: uuid.UUID, login_name: str, evidence: StepUpEvidence
    ) -> bool:
        assertion = evidence.assertion
        session_id = evidence.session_id
        config = self._registry.config
        if assertion is None or session_id is None or config is None:
            return False

        async def prepare(session: AsyncSession):
            challenge = await self._registry.consume_challenge_in(
                session,
                user_id=user_id,
                session_id=session_id,
                purpose=PasskeyPurpose.AUTHENTICATE,
            )
            if challenge is None:
                raise StepUpRefused(AuthReason.CHALLENGE_INVALID)
            stored = await self._registry.stored_in(
                session, user_id, assertion.credential_id
            )
            if stored is None:
                raise StepUpRefused(AuthReason.UNKNOWN_CREDENTIAL)
            return challenge, stored

        # The challenge is spent here and stays spent whatever happens next.
        challenge, stored = await run(self._database, prepare, self._timeout)
        try:
            verified = ceremony.verify_assertion(
                config,
                assertion,
                challenge,
                public_key=stored.public_key,
                user_id=user_id,
            )
        except ceremony.CeremonyRejected:
            raise StepUpRefused(AuthReason.VERIFICATION_FAILED) from None

        async def use(session: AsyncSession) -> UseOutcome:
            return await self._registry.record_use_in(
                session, passkey_id=stored.id, user_id=user_id, assertion=verified
            )

        outcome = await run(self._database, use, self._timeout)
        if outcome is UseOutcome.USED:
            return True
        if outcome is UseOutcome.REGRESSION:
            # A fixed line: no credential id, no counter, no name.
            logger.warning(
                "A passkey assertion did not advance the signature counter "
                "(replay or cloned authenticator)"
            )
            raise StepUpRefused(AuthReason.SIGN_COUNT_REGRESSION)
        if outcome is UseOutcome.GONE:
            raise StepUpRefused(AuthReason.UNKNOWN_CREDENTIAL)
        raise StepUpRefused(AuthReason.VERIFICATION_FAILED)
