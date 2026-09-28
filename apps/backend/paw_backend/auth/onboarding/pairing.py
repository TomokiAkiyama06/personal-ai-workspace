"""Adding a device from a trusted one: QR code / link pairing (PAW-024).

REQUIREMENTS.md "User Invitation / Multi-device Login"; the state machine and the
values are Decision 0033 section 2 (Approved 2026-09-28)::

    issued --(a User's new device hands the token in)------------------> completed
    issued --(an Owner's / Admin's new device)--> claimed --(approve)--> approved
    approved --(the new device completes with its claim)---------------> completed
    claimed --(reject)--> rejected
    issued / claimed / approved --(revoked, superseded, account closed)--> revoked

* **Issuing** (a trusted device: a valid session whose Passkey gate is open) ends
  every live pairing of the user first: at most one valid token at a time (a
  partial unique index also says so). The token (``onetime.PAIRING``) lives
  ``pairing_token_ttl_seconds`` (10 minutes by default) and is shown once, with the
  link path ``/pair#<token>`` (a URL fragment never reaches the server's logs).
* **Claiming** (public, the new device): rate limited per source and in total
  (``pairing_*``: a correct token or claim gives its attempt back) and per token
  (``setup_token_max_attempts``, counted only while the token is ``issued``). A
  User's new device gets its session at once. An Owner's or Admin's (judged by
  the role at that moment) gets a one-time claim (``onetime.CLAIM``, with its own
  random lookup key ``claim_id``: whoever saw the QR code knows the pairing's id,
  never the claim's) and waits, at most the same lifetime, for a trusted device's
  explicit approval, which needs a recent **Passkey Step-up** of the approving
  session (a rejection does not). Either way the token is spent: the same QR code
  cannot be used twice.
* **The confirmation code** (Decision 0033, point 12, approved 2026-09-28): with
  its claim, an Owner's / Admin's new device is given a short code
  (``CONFIRMATION_LENGTH`` characters of ``CONFIRMATION_ALPHABET``, from the
  operating system's CSPRNG, independent of the token and the claim). The new
  device shows it; the approver types it in on the trusted device, which is never
  told it (so whoever handed a seen QR code in first cannot be approved: the code
  is on their screen, not on the approver's). Only a salted HMAC-SHA256 is
  stored, compared in constant time; a missing or wrong code refuses the approval
  and counts (after the Step-up check, in the same transaction as its audit
  event), and the ``CONFIRMATION_MAX_ATTEMPTS``-th wrong one ends the pairing
  (``confirmation_failed``). The code lives as long as its claim. A User's pairing
  has no approval and no code (point 5).
* **Completing** (public, the new device): with an approved claim, the session;
  with a claim still waiting, ``Pending`` and nothing changes; anything else is the
  one ``TokenRejectedError``.
* **The new session** has ``auth_method = pairing`` and no Step-up. Its Passkey gate
  is the sign-in's for a User (the policy and the Passkeys registered) and ``open``
  for an approved Owner / Admin (the trusted device's Passkey Step-up stood in for
  the new device's Passkey assertion).
* **Locks**: the user's row ``FOR UPDATE`` first, then the pairing's: issuing,
  revoking, claiming, deciding and completing never wait for each other in the
  opposite order. Two claims of one token: exactly one succeeds.
* **Audit**: ids and enum values (``auth.pairing.*``); a pairing is named by its
  ``audit_ref``, never by its id, token, claim or device name.
"""

import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth import tokens
from paw_backend.auth.audit import AuthAction, AuthAudit, AuthReason
from paw_backend.auth.auth_policy import AuthPolicyService
from paw_backend.auth.context import RequestContext
from paw_backend.auth.db import run
from paw_backend.auth.errors import (
    ConfirmationCodeError,
    InvalidAuthInputError,
    PairingNotFoundError,
    PasskeyRequiredError,
    SessionEndedError,
    StepUpRequiredError,
    TokenRejectedError,
)
from paw_backend.auth.models import (
    AuthMethod,
    PasskeyGate,
    PasskeyRequirement,
    RevokeReason,
    ThrottleScope,
)
from paw_backend.auth.onboarding import onetime
from paw_backend.auth.onboarding.common import (
    APPROVAL_ROLES,
    StepUpGuard,
    TokenRefusal,
    require_context,
    require_token_text,
    require_uuid,
)
from paw_backend.auth.onboarding.models import (
    HASH_BYTES,
    SALT_BYTES,
    PairingEnd,
    PairingState,
)
from paw_backend.auth.service import LoginResult
from paw_backend.auth.sessions import (
    AuthenticatedSession,
    SessionStore,
    validate_device_label,
)
from paw_backend.auth.throttle import Reservation, Throttle
from paw_backend.authz.roles import SystemRole
from paw_backend.db import Database

logger = logging.getLogger(__name__)

LINK_PATH = "/pair#{token}"
_CLOCK = (
    "WITH clock AS (SELECT greatest(CAST(:now AS timestamptz), "
    "clock_timestamp()) AS ts)"
)
_LIVE = "('issued', 'claimed', 'approved')"

# The confirmation code of an Owner's / Admin's claim (Decision 0033, point 12).
# No 0 / O, 1 / I / L or U: easy to read out and type. 30 ** 8 is about 2 ** 39.
CONFIRMATION_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CONFIRMATION_LENGTH = 8
CONFIRMATION_MAX_ATTEMPTS = 3
_CODE_INPUT_MAX = 32
_DUMMY_SALT = bytes(SALT_BYTES)
_DUMMY_CODE = "!" * CONFIRMATION_LENGTH
_DUMMY_HASH = onetime.hash_secret(_DUMMY_SALT, "?" * CONFIRMATION_LENGTH)


def new_confirmation_code() -> str:
    """A fresh confirmation code from the operating system's CSPRNG."""
    return "".join(
        secrets.choice(CONFIRMATION_ALPHABET) for _ in range(CONFIRMATION_LENGTH)
    )


def _normalised_code(value: object) -> str | None:
    """What a person typed, without case, spaces and hyphens; ``None`` if malformed."""
    if not isinstance(value, str) or len(value) > _CODE_INPUT_MAX:
        return None
    code = value.replace("-", "").replace(" ", "").upper()
    if len(code) != CONFIRMATION_LENGTH or not all(
        char in CONFIRMATION_ALPHABET for char in code
    ):
        return None
    return code


def confirmation_matches(
    value: object, salt: bytes | None, digest: bytes | None
) -> bool:
    """Whether ``value`` is the code stored as ``salt`` / ``digest``.

    The same work (one HMAC, one constant-time comparison) whatever ``value`` is.
    """
    code = _normalised_code(value)
    stored = (
        isinstance(salt, bytes)
        and isinstance(digest, bytes)
        and len(digest) == HASH_BYTES
    )
    candidate = onetime.hash_secret(
        salt if stored else _DUMMY_SALT, code if code is not None else _DUMMY_CODE
    )
    same = hmac.compare_digest(candidate, digest if stored else _DUMMY_HASH)
    return same and stored and code is not None


@dataclass(frozen=True, slots=True)
class IssuedPairing:
    """A pairing token, shown once on the trusted device (as a QR code / a link)."""

    # The pairing's public name (its ``audit_ref``): what approve / reject take.
    pairing_id: uuid.UUID
    token: str = field(repr=False)
    expires_at: datetime
    # Whether the new device will need this user's explicit approval.
    approval_required: bool

    @property
    def link_path(self) -> str:
        return LINK_PATH.format(token=self.token)


@dataclass(frozen=True, slots=True)
class PairingOutcome:
    """What the new device gets: its session, or a claim to wait with.

    ``login`` is set when the device has its session (``completed``); otherwise
    ``claim`` is set the first time (the claim to complete with, shown once) and
    ``expires_at`` says until when the approval may come.
    """

    login: LoginResult | None = None
    claim: str | None = field(default=None, repr=False)
    # With the claim: the code the new device shows for the approver to enter.
    confirmation_code: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None

    @property
    def completed(self) -> bool:
        return self.login is not None


@dataclass(frozen=True, slots=True)
class PendingPairing:
    """A new device of the user that waits for an approval."""

    pairing_id: uuid.UUID
    device_label: str | None
    claimed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _User:
    login_name: str
    system_role: str
    status: str


async def end_live_pairings_in(
    session: AsyncSession, user_id: uuid.UUID, reason: PairingEnd, now: datetime
) -> list[uuid.UUID]:
    """End every live pairing of the user; their ``audit_ref`` values.

    The caller holds the user's row lock (``FOR UPDATE``).
    """
    result = await session.execute(
        text(
            f"UPDATE device_pairings SET state = 'revoked', ended_at = :now, "
            f"ended_reason = :reason WHERE user_id = :id AND state IN {_LIVE} "
            f"RETURNING audit_ref"
        ),
        {"now": now, "reason": reason.value, "id": user_id},
    )
    return [row.audit_ref for row in result.all()]


class PairingService:
    """Issue, revoke, list, approve, reject; claim and complete (public)."""

    def __init__(
        self,
        database: Database,
        *,
        sessions: SessionStore,
        throttle: Throttle,
        audit: AuthAudit,
        policy: AuthPolicyService,
        passkeys,
        ttl_seconds: int,
        max_attempts: int,
        timeout_seconds: float = 3.0,
    ) -> None:
        for name, value, kind in (
            ("database", database, Database),
            ("sessions", sessions, SessionStore),
            ("throttle", throttle, Throttle),
            ("audit", audit, AuthAudit),
            ("policy", policy, AuthPolicyService),
        ):
            if not isinstance(value, kind):
                raise TypeError(f"{name} must be a {kind.__name__}")
        if not callable(getattr(passkeys, "count_active_in", None)):
            raise TypeError("passkeys must have a count_active_in(session, user_id)")
        for name, value, low, high in (
            ("ttl_seconds", ttl_seconds, 60, 3_600),
            ("max_attempts", max_attempts, 1, 20),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not low <= value <= high
            ):
                raise ValueError(f"{name} must be an int in [{low}, {high}]")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be in (0, 60]")
        self._database = database
        self._sessions = sessions
        self._throttle = throttle
        self._audit = audit
        self._policy = policy
        self._passkeys = passkeys
        self._passkeys_on = getattr(passkeys, "available", False) is True
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_attempts = max_attempts
        self._timeout = float(timeout_seconds)

    # -- the trusted device ------------------------------------------------------------

    async def issue(
        self, auth: AuthenticatedSession, context: RequestContext
    ) -> IssuedPairing:
        """A new pairing token; every live pairing of the user ends first."""
        auth = _require_auth(auth)
        require_context(context)
        new = onetime.PAIRING.generate()
        audit_ref = uuid.uuid4()

        async def work(session: AsyncSession) -> IssuedPairing:
            user = await self._lock_trusted_in(session, auth)
            now = self._audit.now()
            for ref in await end_live_pairings_in(
                session, auth.record.user_id, PairingEnd.SUPERSEDED, now
            ):
                await self._record_in(
                    session,
                    AuthAction.PAIRING_REVOKE,
                    AuthReason.SUPERSEDED,
                    auth,
                    context,
                    ref,
                )
            expires_at = now + self._ttl
            approval = SystemRole(user.system_role) in APPROVAL_ROLES
            await session.execute(
                text(
                    "INSERT INTO device_pairings (id, audit_ref, user_id, "
                    "issued_by_session, state, approval_required, salt, "
                    "secret_hash, created_at, expires_at, attempts, "
                    "confirm_attempts) VALUES (:id, :ref, :user_id, :session_id, "
                    "'issued', :approval, :salt, :hash, :now, :expires, 0, 0)"
                ),
                {
                    "id": new.token_id,
                    "ref": audit_ref,
                    "user_id": auth.record.user_id,
                    "session_id": auth.record.id,
                    "approval": approval,
                    "salt": new.salt,
                    "hash": new.secret_hash,
                    "now": now,
                    "expires": expires_at,
                },
            )
            await self._record_in(
                session,
                AuthAction.PAIRING_ISSUE,
                AuthReason.ISSUED,
                auth,
                context,
                audit_ref,
            )
            return IssuedPairing(audit_ref, new.token, expires_at, approval)

        return await run(self._database, work, self._timeout)

    async def revoke(self, auth: AuthenticatedSession, context: RequestContext) -> int:
        """End every live pairing of the user (from any of their trusted devices)."""
        auth = _require_auth(auth)
        require_context(context)

        async def work(session: AsyncSession) -> int:
            await self._lock_trusted_in(session, auth)
            now = self._audit.now()
            ended = await end_live_pairings_in(
                session, auth.record.user_id, PairingEnd.REVOKED_BY_USER, now
            )
            for ref in ended:
                await self._record_in(
                    session,
                    AuthAction.PAIRING_REVOKE,
                    AuthReason.REVOKED,
                    auth,
                    context,
                    ref,
                )
            return len(ended)

        return await run(self._database, work, self._timeout)

    async def pending(self, auth: AuthenticatedSession) -> tuple[PendingPairing, ...]:
        """The user's new devices that wait for an approval (and may still get it)."""
        auth = _require_auth(auth)

        async def work(session: AsyncSession) -> tuple[PendingPairing, ...]:
            rows = (
                await session.execute(
                    text(
                        f"""{_CLOCK}
                        SELECT p.audit_ref, p.device_label, p.claimed_at,
                               p.expires_at
                          FROM clock, device_pairings p
                         WHERE p.user_id = :id AND p.state = 'claimed'
                           AND p.expires_at > clock.ts
                         ORDER BY p.claimed_at"""
                    ),
                    {"id": auth.record.user_id, "now": self._audit.now()},
                )
            ).all()
            return tuple(
                PendingPairing(
                    row.audit_ref, row.device_label, row.claimed_at, row.expires_at
                )
                for row in rows
            )

        return await run(self._database, work, self._timeout)

    async def approve(
        self,
        auth: AuthenticatedSession,
        pairing_id: uuid.UUID,
        context: RequestContext,
        *,
        confirmation_code: str | None,
    ) -> None:
        """Approve a waiting new device with the code it shows.

        Needs a recent Passkey Step-up of this session. ``ConfirmationCodeError``
        when the code is missing or wrong (counted; the last allowed wrong one
        ends the pairing).
        """
        await self._decide(
            auth, pairing_id, context, approve=True, code=confirmation_code
        )

    async def reject(
        self,
        auth: AuthenticatedSession,
        pairing_id: uuid.UUID,
        context: RequestContext,
    ) -> None:
        """Refuse a waiting new device (no Step-up: refusing is the safe side)."""
        await self._decide(auth, pairing_id, context, approve=False)

    async def _decide(
        self,
        auth: AuthenticatedSession,
        pairing_id: uuid.UUID,
        context: RequestContext,
        *,
        approve: bool,
        code: str | None = None,
    ) -> None:
        auth = _require_auth(auth)
        require_uuid("pairing_id", pairing_id)
        require_context(context)
        action = AuthAction.PAIRING_APPROVE if approve else AuthAction.PAIRING_REJECT
        guard = StepUpGuard(self._policy)

        async def work(session: AsyncSession) -> bool:
            await self._lock_trusted_in(session, auth)
            now = self._audit.now()
            if approve:
                await guard.require_in(
                    session,
                    session_id=auth.record.id,
                    user_id=auth.record.user_id,
                    now=now,
                )
            row = (
                await session.execute(
                    text(
                        f"""{_CLOCK}
                        SELECT p.id, p.confirm_salt, p.confirm_hash,
                               p.confirm_attempts
                          FROM clock, device_pairings p
                         WHERE p.audit_ref = :ref AND p.user_id = :user_id
                           AND p.state = 'claimed' AND p.expires_at > clock.ts
                           FOR UPDATE OF p"""
                    ),
                    {"ref": pairing_id, "user_id": auth.record.user_id, "now": now},
                )
            ).first()
            if row is None:
                raise PairingNotFoundError
            if approve and not confirmation_matches(
                code, row.confirm_salt, row.confirm_hash
            ):
                await self._count_code_in(session, row, now, auth, context, pairing_id)
                return False
            await session.execute(
                text(
                    "UPDATE device_pairings SET state = :state, decided_at = :now, "
                    "decided_by_session = :session_id WHERE id = :id"
                ),
                {
                    "state": (
                        PairingState.APPROVED if approve else PairingState.REJECTED
                    ).value,
                    "now": now,
                    "session_id": auth.record.id,
                    "id": row.id,
                },
            )
            await self._record_in(
                session,
                action,
                AuthReason.APPROVED if approve else AuthReason.REJECTED,
                auth,
                context,
                pairing_id,
            )
            return True

        try:
            decided = await run(self._database, work, self._timeout)
        except StepUpRequiredError:
            if guard.refused is not None:
                await self._audit.record_best_effort(
                    self._event(action, guard.refused, auth, context, pairing_id)
                )
            raise
        if not decided:
            raise ConfirmationCodeError

    async def _count_code_in(
        self, session: AsyncSession, row, now: datetime, auth, context, pairing_id
    ) -> None:
        """Count one missing / wrong confirmation code; the last one ends the pairing.

        Committed with its audit event (the caller returns, never raises, so that
        the count is not rolled back).
        """
        attempts = row.confirm_attempts + 1
        exhausted = attempts >= CONFIRMATION_MAX_ATTEMPTS
        await session.execute(
            text(
                "UPDATE device_pairings SET confirm_attempts = :attempts, "
                "state = CASE WHEN :exhausted THEN 'revoked' ELSE state END, "
                "ended_at = CASE WHEN :exhausted THEN CAST(:now AS timestamptz) "
                "ELSE ended_at END, "
                "ended_reason = CASE WHEN :exhausted THEN :reason "
                "ELSE ended_reason END WHERE id = :id"
            ),
            {
                "attempts": attempts,
                "exhausted": exhausted,
                "now": now,
                "reason": PairingEnd.CONFIRMATION_FAILED.value,
                "id": row.id,
            },
        )
        await self._audit.record_in(
            session,
            self._event(
                AuthAction.PAIRING_APPROVE,
                AuthReason.CONFIRMATION_ATTEMPTS_EXHAUSTED
                if exhausted
                else AuthReason.CONFIRMATION_CODE_MISMATCH,
                auth,
                context,
                pairing_id,
            ),
        )
        logger.info("Pairing approval refused (confirmation code)")

    # -- the new device (public) -------------------------------------------------------

    async def claim(
        self,
        token: str,
        device_label: str,
        context: RequestContext,
        *,
        remember_me: bool = False,
        replace_token: str | None = None,
    ) -> PairingOutcome:
        """Hand a pairing token in: a session (a User) or a claim to wait with.

        ``ThrottledError``, ``TokenRejectedError`` (the one answer for every token
        that is not acceptable).
        """
        require_token_text("token", token)
        require_context(context)
        label = validate_device_label(device_label)
        if label is None:
            raise InvalidAuthInputError("device_label")
        if not isinstance(remember_me, bool):
            raise InvalidAuthInputError("remember_me")
        if replace_token is not None and not isinstance(replace_token, str):
            raise InvalidAuthInputError("replace_token")
        reservations = await self._reserve(context)
        parsed = onetime.PAIRING.parse(token)

        async def work(session: AsyncSession) -> PairingOutcome | TokenRefusal:
            found = await self._lock_pairing_in(session, parsed)
            if isinstance(found, TokenRefusal):
                return found
            user, row, now = found
            matches = onetime.verify(parsed, row.salt, row.secret_hash)
            if row.locked_at is not None:
                return _locked()
            if row.state != PairingState.ISSUED.value:
                # The token is spent already: attempts no longer count, so that
                # whoever saw the QR code cannot lock a claimed pairing out (its
                # locked_at would refuse the waiting device's claim too).
                refusal = (
                    AuthReason.TOKEN_MISMATCH
                    if not matches
                    else AuthReason.TOKEN_REVOKED
                    if row.state
                    in (PairingState.REVOKED.value, PairingState.REJECTED.value)
                    else AuthReason.TOKEN_USED
                )
            else:
                refusal = await self._count_attempt_in(session, row, now, matches)
            if refusal is None:
                if row.expired:
                    refusal = AuthReason.TOKEN_EXPIRED
                elif user.status != "active":
                    refusal = AuthReason.USER_NOT_ELIGIBLE
            if refusal is not None:
                return self._refusal(refusal, row, user)
            for reservation in reservations:
                await self._throttle.succeed_in(session, reservation)
            if SystemRole(user.system_role) in APPROVAL_ROLES:
                # A fresh lookup key, not the pairing's id (which the QR code
                # shows): see ``DevicePairingRow.claim_id``.
                claim = onetime.CLAIM.generate()
                code = new_confirmation_code()
                code_salt = secrets.token_bytes(SALT_BYTES)
                expires_at = now + self._ttl
                await session.execute(
                    text(
                        "UPDATE device_pairings SET state = 'claimed', "
                        "approval_required = true, claim_id = :claim_id, "
                        "claim_salt = :salt, "
                        "claim_hash = :hash, confirm_salt = :code_salt, "
                        "confirm_hash = :code_hash, device_label = :label, "
                        "remember_me = :remember, claimed_at = :now, "
                        "expires_at = :expires WHERE id = :id"
                    ),
                    {
                        "claim_id": claim.token_id,
                        "salt": claim.salt,
                        "hash": claim.secret_hash,
                        "code_salt": code_salt,
                        "code_hash": onetime.hash_secret(code_salt, code),
                        "label": label,
                        "remember": remember_me,
                        "now": now,
                        "expires": expires_at,
                        "id": row.id,
                    },
                )
                await self._audit.record_in(
                    session,
                    self._user_event(
                        AuthAction.PAIRING_CLAIM,
                        AuthReason.PENDING_APPROVAL,
                        row.user_id,
                        user.system_role,
                        context,
                        "device_pairing",
                        row.audit_ref,
                    ),
                )
                return PairingOutcome(
                    claim=claim.token, confirmation_code=code, expires_at=expires_at
                )
            login = await self._complete_in(
                session,
                row,
                user,
                now,
                context,
                label=label,
                remember_me=remember_me,
                approved=False,
                replace_token=replace_token,
            )
            return PairingOutcome(login=login)

        return await self._finish(work, AuthAction.PAIRING_CLAIM, context)

    async def complete(
        self,
        claim: str,
        context: RequestContext,
        *,
        replace_token: str | None = None,
    ) -> PairingOutcome:
        """Hand the claim in: the session once approved, ``Pending`` until then."""
        require_token_text("claim", claim)
        require_context(context)
        if replace_token is not None and not isinstance(replace_token, str):
            raise InvalidAuthInputError("replace_token")
        reservations = await self._reserve(context)
        parsed = onetime.CLAIM.parse(claim)

        async def work(session: AsyncSession) -> PairingOutcome | TokenRefusal:
            found = await self._lock_pairing_in(session, parsed, by_claim=True)
            if isinstance(found, TokenRefusal):
                return found
            user, row, now = found
            matches = onetime.verify(parsed, row.claim_salt, row.claim_hash)
            if row.locked_at is not None:
                return _locked()
            if not matches:
                # Only a wrong claim counts: a new device polling with the right
                # one may ask for as long as its wait lasts.
                refusal = await self._count_attempt_in(session, row, now, False)
                return self._refusal(refusal, row, user)
            if row.expired or row.state not in (
                PairingState.CLAIMED.value,
                PairingState.APPROVED.value,
            ):
                reason = (
                    AuthReason.TOKEN_USED
                    if row.state == PairingState.COMPLETED.value
                    else AuthReason.TOKEN_REVOKED
                    if row.state
                    in (PairingState.REVOKED.value, PairingState.REJECTED.value)
                    else AuthReason.TOKEN_EXPIRED
                )
                return self._refusal(reason, row, user)
            if user.status != "active":
                return self._refusal(AuthReason.USER_NOT_ELIGIBLE, row, user)
            for reservation in reservations:
                await self._throttle.succeed_in(session, reservation)
            if row.state == PairingState.CLAIMED.value:
                return PairingOutcome(expires_at=row.expires_at)
            login = await self._complete_in(
                session,
                row,
                user,
                now,
                context,
                label=row.device_label,
                remember_me=bool(row.remember_me),
                approved=True,
                replace_token=replace_token,
            )
            return PairingOutcome(login=login)

        return await self._finish(work, AuthAction.PAIRING_COMPLETE, context)

    # -- internals ---------------------------------------------------------------------

    async def _reserve(self, context: RequestContext) -> tuple[Reservation, ...]:
        return await self._throttle.reserve_many(
            [
                (ThrottleScope.PAIRING_SOURCE, tokens.source_key(context.source)),
                (ThrottleScope.PAIRING_GLOBAL, tokens.PAIRING_GLOBAL_KEY),
            ]
        )

    async def _finish(self, work, action: AuthAction, context) -> PairingOutcome:
        outcome = await run(self._database, work, self._timeout)
        if isinstance(outcome, PairingOutcome):
            return outcome
        if outcome.reason is not None:
            await self._audit.record_best_effort(
                self._user_event(
                    action,
                    outcome.reason,
                    outcome.user_id,
                    outcome.role,
                    context,
                    "device_pairing",
                    outcome.audit_ref,
                    allowed=False,
                )
            )
        raise TokenRejectedError

    async def _lock_pairing_in(
        self, session: AsyncSession, parsed, *, by_claim: bool = False
    ):
        """The user's row and the pairing's, locked in that order; or a refusal.

        ``by_claim``: ``parsed`` is a claim, looked up by ``claim_id``.
        """
        lookup = parsed.token_id if parsed is not None else uuid.uuid4()
        key = "claim_id" if by_claim else "id"
        user_id = (
            await session.execute(
                text(f"SELECT user_id FROM device_pairings WHERE {key} = :id"),
                {"id": lookup},
            )
        ).scalar_one_or_none()
        if user_id is None or parsed is None:
            onetime.verify(parsed, None, None)
            logger.info("Pairing refused (no such token)")
            return TokenRefusal(None)
        row_user = (
            await session.execute(
                text(
                    "SELECT login_name, system_role, status FROM users "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": user_id},
            )
        ).first()
        now = self._audit.now()
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    SELECT p.id, p.audit_ref, p.user_id, p.state, p.salt,
                           p.secret_hash, p.claim_salt, p.claim_hash,
                           p.device_label, p.remember_me, p.attempts, p.locked_at,
                           p.expires_at, p.expires_at <= clock.ts AS expired
                      FROM clock, device_pairings p WHERE p.{key} = :id
                       FOR UPDATE OF p"""
                ),
                {"id": lookup, "now": now},
            )
        ).first()
        if row is None or row_user is None:
            onetime.verify(parsed, None, None)
            return TokenRefusal(None)
        user = _User(row_user.login_name, row_user.system_role, row_user.status)
        return user, row, now

    async def _count_attempt_in(
        self, session: AsyncSession, row, now: datetime, matches: bool
    ) -> AuthReason | None:
        """Count one attempt of the token; the refusal it leads to, if any."""
        attempts = row.attempts + 1
        exhausted = attempts >= self._max_attempts and not matches
        await session.execute(
            text(
                "UPDATE device_pairings SET attempts = :attempts, locked_at = "
                "CASE WHEN :exhausted THEN CAST(:now AS timestamptz) END "
                "WHERE id = :id"
            ),
            {"attempts": attempts, "exhausted": exhausted, "now": now, "id": row.id},
        )
        if matches:
            return None
        return AuthReason.ATTEMPTS_EXHAUSTED if exhausted else AuthReason.TOKEN_MISMATCH

    @staticmethod
    def _refusal(reason: AuthReason, row, user: _User) -> TokenRefusal:
        return TokenRefusal(reason, row.user_id, user.system_role, row.audit_ref)

    async def _complete_in(
        self,
        session: AsyncSession,
        row,
        user: _User,
        now: datetime,
        context: RequestContext,
        *,
        label: str | None,
        remember_me: bool,
        approved: bool,
        replace_token: str | None,
    ) -> LoginResult:
        gate = (
            PasskeyGate.OPEN
            if approved
            else await self._gate_for_in(session, row.user_id, user.system_role)
        )
        if replace_token is not None:
            await self._sessions.revoke_by_token(
                session, replace_token, RevokeReason.REPLACED
            )
        issued = await self._sessions.create(
            session,
            row.user_id,
            remember_me=remember_me,
            device_label=label,
            auth_method=AuthMethod.PAIRING,
            passkey_gate=gate,
        )
        await session.execute(
            text(
                "UPDATE device_pairings SET state = 'completed', completed_at = :now, "
                "created_session = :session_id, device_label = :label, "
                "remember_me = :remember, approval_required = :approval "
                "WHERE id = :id"
            ),
            {
                "now": now,
                "session_id": issued.record.id,
                "label": label,
                "remember": remember_me,
                "approval": approved,
                "id": row.id,
            },
        )
        await self._audit.record_in(
            session,
            self._user_event(
                AuthAction.PAIRING_COMPLETE,
                AuthReason.COMPLETED,
                row.user_id,
                user.system_role,
                context,
                "session",
                issued.record.id,
            ),
        )
        record = issued.record
        return LoginResult(
            issued.token,
            AuthenticatedSession(
                record=record,
                login_name=user.login_name,
                system_role=SystemRole(user.system_role),
                checked_at=record.created_at,
                token_hash=tokens.hash_session_token(issued.token),
            ),
        )

    async def _gate_for_in(
        self, session: AsyncSession, user_id: uuid.UUID, role: str
    ) -> PasskeyGate:
        """The gate of a User's paired session: the password sign-in's rule."""
        if not self._passkeys_on:
            return PasskeyGate.OPEN
        policy = await self._policy.get_in(session)
        if policy.requirement_for(SystemRole(role)) is not PasskeyRequirement.REQUIRED:
            return PasskeyGate.OPEN
        count = await self._passkeys.count_active_in(session, user_id)
        return (
            PasskeyGate.ENROLLMENT_REQUIRED
            if count == 0
            else PasskeyGate.ASSERTION_REQUIRED
        )

    async def _lock_trusted_in(
        self, session: AsyncSession, auth: AuthenticatedSession
    ) -> _User:
        """Lock the user's row; the request's session must still be a trusted one."""
        row = (
            await session.execute(
                text(
                    "SELECT login_name, system_role, status FROM users "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": auth.record.user_id},
            )
        ).first()
        if row is None or row.status != "active":
            raise SessionEndedError
        gate = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    SELECT s.passkey_gate FROM clock, auth_sessions s
                     WHERE s.id = :id AND s.user_id = :user_id
                       AND s.revoked_at IS NULL AND s.idle_expires_at > clock.ts
                       AND s.absolute_expires_at > clock.ts"""
                ),
                {
                    "id": auth.record.id,
                    "user_id": auth.record.user_id,
                    "now": self._audit.now(),
                },
            )
        ).scalar_one_or_none()
        if gate is None:
            raise SessionEndedError
        if gate != PasskeyGate.OPEN.value:
            raise PasskeyRequiredError
        return _User(row.login_name, row.system_role, row.status)

    def _event(self, action, reason, auth, context, audit_ref):
        return self._user_event(
            action,
            reason,
            auth.record.user_id,
            auth.system_role,
            context,
            "device_pairing",
            audit_ref,
            allowed=False,
        )

    async def _record_in(self, session, action, reason, auth, context, audit_ref):
        await self._audit.record_in(
            session,
            self._user_event(
                action,
                reason,
                auth.record.user_id,
                auth.system_role,
                context,
                "device_pairing",
                audit_ref,
            ),
        )

    def _user_event(
        self,
        action,
        reason,
        user_id,
        role,
        context,
        kind,
        resource_id,
        *,
        allowed: bool = True,
    ):
        return self._audit.event(
            action,
            reason,
            allowed=allowed,
            correlation_id=context.correlation_id,
            client_request_id=context.client_request_id,
            actor_id=user_id,
            actor_role=role,
            resource_kind=kind,
            resource_id=resource_id,
        )


def _locked() -> TokenRefusal:
    """A token already locked out: refused, logged, not audited again (the row that
    locked it was), like the Owner's token."""
    logger.info("Pairing refused (locked token)")
    return TokenRefusal(None)


def _require_auth(value: object) -> AuthenticatedSession:
    if not isinstance(value, AuthenticatedSession):
        raise InvalidAuthInputError("auth")
    return value
