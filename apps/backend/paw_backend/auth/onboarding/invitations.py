"""Invite-only registration: invitation tokens (PAW-024, Decision 0033 section 1).

* **Nobody registers on their own.** An Owner or an Admin invites a user: an
  ``invited`` row in ``users`` (made by the ``SECURITY DEFINER`` function
  ``paw_invite_user``: the web role cannot INSERT into ``users``) and a one-time
  invitation token, shown once. An Admin invites Users; only the Owner invites
  Admins; nobody invites an Owner. Inviting, reissuing and revoking are sensitive
  operations of an administrator: they need a recent Passkey Step-up of the
  administrator's own session (like unlocking an account).
* **The token** (``onetime.INVITATION``) is single use, expires
  (``invitation_ttl_seconds``), can be revoked, and has an attempt limit
  (``setup_token_max_attempts``; a token that reached it is locked for good). A
  user has at most one outstanding token (a partial unique index); a reissue ends
  the old one first.
* **Redeeming** (public): rate limited per source and in total (the Owner token's
  scopes) before anything else; the password is checked and hashed before the
  token is looked at; then ONE transaction locks the user's row, then the token's,
  counts the attempt, and, for a right token of a user who is still ``invited``,
  sets the password, marks the token used, makes the user ``active``
  (``paw_change_user_status``) and writes the audit row. A refused token's attempt
  is committed (the transaction does not roll back for it). No session is made: the
  user signs in normally afterwards (the Passkey policy's gate applies there).
* **Every refusal is the same** ``TokenRejectedError``; the reason is in the audit
  trail only (an unknown token id is logged, never written: anyone could make one).
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth import tokens
from paw_backend.auth.audit import AuthAction, AuthAudit, AuthReason
from paw_backend.auth.auth_policy import AuthPolicyService
from paw_backend.auth.context import RequestContext
from paw_backend.auth.db import run, sqlstate_of
from paw_backend.auth.errors import (
    AccountNotFoundError,
    AccountStateError,
    AuthPermissionError,
    InvalidAuthInputError,
    InvitationNotFoundError,
    LoginNameTakenError,
    StepUpRequiredError,
    TokenRejectedError,
)
from paw_backend.auth.models import ThrottleScope
from paw_backend.auth.onboarding import onetime
from paw_backend.auth.onboarding.common import (
    INVITABLE_ROLES,
    StepUpGuard,
    TokenRefusal,
    may_administer,
    require_actor,
    require_context,
    require_token_text,
    require_uuid,
)
from paw_backend.auth.onboarding.models import InvitationEnd
from paw_backend.auth.passwords import PasswordHasher, validate_new_password
from paw_backend.auth.throttle import Throttle
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal
from paw_backend.db import Database
from paw_backend.identity import InvalidLoginNameError, normalize_login_name

logger = logging.getLogger(__name__)

_UNIQUE_VIOLATION = "23505"
_CLOCK = (
    "WITH clock AS (SELECT greatest(CAST(:now AS timestamptz), "
    "clock_timestamp()) AS ts)"
)


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    """An invitation. ``token`` is shown once (to the administrator), never stored."""

    user_id: uuid.UUID
    login_name: str
    system_role: SystemRole
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RedeemedInvitation:
    """The invited user set a password and is ``active`` (nobody is signed in)."""

    user_id: uuid.UUID
    system_role: SystemRole


@dataclass(frozen=True, slots=True)
class _Target:
    login_name: str
    system_role: str
    status: str


class InvitationService:
    """Invite, reissue, revoke, redeem. See the module docstring."""

    def __init__(
        self,
        database: Database,
        *,
        hasher: PasswordHasher,
        throttle: Throttle,
        audit: AuthAudit,
        policy: AuthPolicyService,
        ttl_seconds: int,
        max_attempts: int,
        timeout_seconds: float = 3.0,
    ) -> None:
        for name, value, kind in (
            ("database", database, Database),
            ("hasher", hasher, PasswordHasher),
            ("throttle", throttle, Throttle),
            ("audit", audit, AuthAudit),
            ("policy", policy, AuthPolicyService),
        ):
            if not isinstance(value, kind):
                raise TypeError(f"{name} must be a {kind.__name__}")
        for name, value, low, high in (
            ("ttl_seconds", ttl_seconds, 600, 1_209_600),
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
        self._hasher = hasher
        self._throttle = throttle
        self._audit = audit
        self._policy = policy
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_attempts = max_attempts
        self._timeout = float(timeout_seconds)

    # -- administrators ----------------------------------------------------------------

    async def invite(
        self,
        actor: Principal,
        login_name: str,
        system_role: SystemRole,
        context: RequestContext,
        *,
        session_id: uuid.UUID,
    ) -> IssuedInvitation:
        """Create an ``invited`` user and its first invitation token.

        ``LoginNameTakenError`` if the name is somebody's; ``AuthPermissionError``
        for a role the actor may not create; ``StepUpRequiredError`` (or its
        subclass) without a recent Passkey Step-up of ``session_id``.
        """
        actor = require_actor(actor)
        require_context(context)
        require_uuid("session_id", session_id)
        if not isinstance(system_role, SystemRole) or system_role not in (
            INVITABLE_ROLES
        ):
            raise InvalidAuthInputError("system_role")
        if not isinstance(login_name, str) or len(login_name) > 256:
            raise InvalidAuthInputError("login_name")
        try:
            name = normalize_login_name(login_name)
        except InvalidLoginNameError:
            raise InvalidAuthInputError("login_name") from None
        if not may_administer(actor, system_role):
            await self._deny(
                AuthAction.INVITATION_ISSUE,
                AuthReason.ROLE_NOT_ALLOWED,
                actor,
                context,
                None,
            )
            raise AuthPermissionError
        user_id = uuid.uuid4()
        new = onetime.INVITATION.generate()
        guard = StepUpGuard(self._policy)
        refused: AuthReason | None = None

        async def work(session: AsyncSession) -> datetime:
            nonlocal refused
            now = self._audit.now()
            await guard.require_in(
                session, session_id=session_id, user_id=actor.user_id, now=now
            )
            try:
                await session.execute(
                    text("SELECT paw_invite_user(:id, :name, :role, :now, :actor)"),
                    {
                        "id": user_id,
                        "name": name,
                        "role": system_role.value,
                        "now": now,
                        "actor": actor.user_id,
                    },
                )
            except IntegrityError as error:
                if sqlstate_of(error) == _UNIQUE_VIOLATION:
                    refused = AuthReason.LOGIN_NAME_TAKEN
                    raise LoginNameTakenError from None
                raise
            expires_at = await self._insert_token_in(session, user_id, new, actor, now)
            await self._audit.record_in(
                session,
                self._event(
                    AuthAction.INVITATION_ISSUE,
                    AuthReason.ISSUED,
                    actor,
                    context,
                    user_id,
                    allowed=True,
                ),
            )
            return expires_at

        try:
            expires_at = await run(self._database, work, self._timeout)
        except (StepUpRequiredError, LoginNameTakenError):
            reason = guard.refused or refused
            if reason is not None:
                await self._deny(
                    AuthAction.INVITATION_ISSUE, reason, actor, context, None
                )
            raise
        return IssuedInvitation(user_id, name, system_role, new.token, expires_at)

    async def reissue(
        self,
        actor: Principal,
        user_id: uuid.UUID,
        context: RequestContext,
        *,
        session_id: uuid.UUID,
    ) -> IssuedInvitation:
        """End the invited user's outstanding token (if any) and issue a new one."""
        actor = require_actor(actor)
        require_uuid("user_id", user_id)
        require_context(context)
        require_uuid("session_id", session_id)
        new = onetime.INVITATION.generate()

        async def change(session: AsyncSession, target: _Target, now: datetime):
            for audit_ref in await self._end_outstanding_in(
                session, user_id, InvitationEnd.SUPERSEDED, now
            ):
                await self._audit.record_in(
                    session,
                    self._event(
                        AuthAction.INVITATION_REVOKE,
                        AuthReason.SUPERSEDED,
                        actor,
                        context,
                        user_id,
                        allowed=True,
                        audit_ref=audit_ref,
                    ),
                )
            expires_at = await self._insert_token_in(session, user_id, new, actor, now)
            await self._audit.record_in(
                session,
                self._event(
                    AuthAction.INVITATION_ISSUE,
                    AuthReason.REISSUED,
                    actor,
                    context,
                    user_id,
                    allowed=True,
                ),
            )
            return IssuedInvitation(
                user_id,
                target.login_name,
                SystemRole(target.system_role),
                new.token,
                expires_at,
            )

        return await self._administer(
            AuthAction.INVITATION_ISSUE, actor, user_id, context, session_id, change
        )

    async def revoke(
        self,
        actor: Principal,
        user_id: uuid.UUID,
        context: RequestContext,
        *,
        session_id: uuid.UUID,
    ) -> None:
        """Revoke the invited user's outstanding token (the user stays ``invited``)."""
        actor = require_actor(actor)
        require_uuid("user_id", user_id)
        require_context(context)
        require_uuid("session_id", session_id)

        async def change(session: AsyncSession, target: _Target, now: datetime):
            ended = await self._end_outstanding_in(
                session, user_id, InvitationEnd.REVOKED, now
            )
            if not ended:
                raise InvitationNotFoundError
            for audit_ref in ended:
                await self._audit.record_in(
                    session,
                    self._event(
                        AuthAction.INVITATION_REVOKE,
                        AuthReason.REVOKED,
                        actor,
                        context,
                        user_id,
                        allowed=True,
                        audit_ref=audit_ref,
                    ),
                )

        await self._administer(
            AuthAction.INVITATION_REVOKE, actor, user_id, context, session_id, change
        )

    async def _administer(self, action, actor, user_id, context, session_id, change):
        """The frame of reissue / revoke: Step-up, lock, rules, then ``change``."""
        if actor.system_role not in (SystemRole.OWNER, SystemRole.ADMIN):
            await self._deny(action, AuthReason.ROLE_NOT_ALLOWED, actor, context, None)
            raise AuthPermissionError
        guard = StepUpGuard(self._policy)
        refused: AuthReason | None = None

        async def work(session: AsyncSession):
            nonlocal refused
            now = self._audit.now()
            await guard.require_in(
                session, session_id=session_id, user_id=actor.user_id, now=now
            )
            target = await _lock_target_in(session, user_id)
            if target is None or target.system_role == SystemRole.OWNER.value:
                raise AccountNotFoundError
            if not may_administer(actor, target.system_role):
                refused = AuthReason.ROLE_NOT_ALLOWED
                raise AuthPermissionError
            if target.status != "invited":
                refused = AuthReason.INVALID_STATE
                raise AccountStateError
            return await change(session, target, now)

        try:
            return await run(self._database, work, self._timeout)
        except (StepUpRequiredError, AuthPermissionError, AccountStateError):
            reason = guard.refused or refused
            if reason is not None:
                await self._deny(action, reason, actor, context, user_id)
            raise

    async def _insert_token_in(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        new: onetime.NewToken,
        actor: Principal,
        now: datetime,
    ) -> datetime:
        expires_at = now + self._ttl
        await session.execute(
            text(
                "INSERT INTO user_invitations (id, audit_ref, user_id, invited_by, "
                "salt, secret_hash, created_at, expires_at, attempts) VALUES (:id, "
                ":ref, :user_id, :actor, :salt, :hash, :now, :expires, 0)"
            ),
            {
                "id": new.token_id,
                "ref": uuid.uuid4(),
                "user_id": user_id,
                "actor": actor.user_id,
                "salt": new.salt,
                "hash": new.secret_hash,
                "now": now,
                "expires": expires_at,
            },
        )
        return expires_at

    @staticmethod
    async def _end_outstanding_in(
        session: AsyncSession,
        user_id: uuid.UUID,
        reason: InvitationEnd,
        now: datetime,
    ) -> list[uuid.UUID]:
        result = await session.execute(
            text(
                "UPDATE user_invitations SET revoked_at = :now, revoked_reason = "
                ":reason WHERE user_id = :user_id AND used_at IS NULL "
                "AND revoked_at IS NULL RETURNING audit_ref"
            ),
            {"now": now, "reason": reason.value, "user_id": user_id},
        )
        return [row.audit_ref for row in result.all()]

    # -- the invited person ------------------------------------------------------------

    async def redeem(
        self, token: str, new_password: str, context: RequestContext
    ) -> RedeemedInvitation:
        """Spend an invitation token and set the invited user's password.

        ``ThrottledError`` (the source or the total is locked: nothing was read),
        ``PasswordPolicyError`` (the token is not spent), ``TokenRejectedError``
        (the one answer for every token that is not acceptable).
        """
        require_token_text("token", token)
        require_context(context)
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
        parsed = onetime.INVITATION.parse(token)
        lookup = parsed.token_id if parsed is not None else uuid.uuid4()

        async def work(session: AsyncSession) -> RedeemedInvitation | TokenRefusal:
            user_id = (
                await session.execute(
                    text("SELECT user_id FROM user_invitations WHERE id = :id"),
                    {"id": lookup},
                )
            ).scalar_one_or_none()
            if user_id is None or parsed is None:
                onetime.verify(parsed, None, None)
                return TokenRefusal(None)
            # The user's row first, then the token's: the order every writer of
            # either keeps (an administrator's reissue locks the user first too).
            target = await _lock_target_in(session, user_id)
            now = self._audit.now()
            row = (
                await session.execute(
                    text(
                        f"""{_CLOCK}
                        SELECT i.salt, i.secret_hash, i.audit_ref, i.used_at,
                               i.revoked_at, i.locked_at, i.attempts,
                               i.expires_at <= clock.ts AS expired
                          FROM clock, user_invitations i
                         WHERE i.id = :id FOR UPDATE OF i"""
                    ),
                    {"id": lookup, "now": now},
                )
            ).one()
            role = target.system_role if target is not None else None
            if row.locked_at is not None:
                onetime.verify(parsed, None, None)
                logger.info("Invitation refused (locked token)")
                return TokenRefusal(None)
            attempts = row.attempts + 1
            exhausted = attempts >= self._max_attempts
            await session.execute(
                text(
                    "UPDATE user_invitations SET attempts = :attempts, locked_at = "
                    "CASE WHEN :exhausted THEN CAST(:now AS timestamptz) END "
                    "WHERE id = :id"
                ),
                {
                    "attempts": attempts,
                    "exhausted": exhausted,
                    "now": now,
                    "id": lookup,
                },
            )
            refusal = None
            if not onetime.verify(parsed, row.salt, row.secret_hash):
                refusal = (
                    AuthReason.ATTEMPTS_EXHAUSTED
                    if exhausted
                    else AuthReason.TOKEN_MISMATCH
                )
            elif row.used_at is not None:
                refusal = AuthReason.TOKEN_USED
            elif row.revoked_at is not None:
                refusal = AuthReason.TOKEN_REVOKED
            elif row.expired:
                refusal = AuthReason.TOKEN_EXPIRED
            elif (
                target is None
                or target.status != "invited"
                or target.system_role == SystemRole.OWNER.value
            ):
                refusal = AuthReason.USER_NOT_ELIGIBLE
            if refusal is not None:
                return TokenRefusal(refusal, user_id, role, row.audit_ref)
            # The login name is only known now: a password that is the name is
            # refused here, and the whole transaction (the attempt too) rolls back.
            validate_new_password(candidate, target.login_name)
            await session.execute(
                text(
                    "UPDATE user_invitations SET used_at = :now, locked_at = NULL "
                    "WHERE id = :id AND used_at IS NULL AND revoked_at IS NULL"
                ),
                {"now": now, "id": lookup},
            )
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
            activated = (
                await session.execute(
                    text(
                        "SELECT paw_change_user_status(:id, 'invited', 'active', "
                        ":now, NULL)"
                    ),
                    {"id": user_id, "now": now},
                )
            ).scalar_one()
            if not activated:  # cannot happen under the row lock
                raise AccountStateError
            await self._throttle.reset_in(
                session,
                ThrottleScope.LOGIN_ACCOUNT,
                tokens.account_key(target.login_name),
            )
            await self._audit.record_in(
                session,
                self._audit.event(
                    AuthAction.INVITATION_REDEEM,
                    AuthReason.REDEEMED,
                    allowed=True,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=user_id,
                    actor_role=target.system_role,
                    resource_kind="invitation",
                    resource_id=row.audit_ref,
                ),
            )
            return RedeemedInvitation(user_id, SystemRole(target.system_role))

        outcome = await run(self._database, work, self._timeout)
        if isinstance(outcome, RedeemedInvitation):
            return outcome
        if outcome.reason is None:
            logger.info("Invitation refused (no such token)")
        else:
            await self._audit.record_best_effort(
                self._audit.event(
                    AuthAction.INVITATION_REDEEM,
                    outcome.reason,
                    allowed=False,
                    correlation_id=context.correlation_id,
                    client_request_id=context.client_request_id,
                    actor_id=outcome.user_id,
                    actor_role=outcome.role,
                    resource_kind="invitation",
                    resource_id=outcome.audit_ref,
                )
            )
        raise TokenRejectedError

    # -- events ------------------------------------------------------------------------

    def _event(
        self,
        action: AuthAction,
        reason: AuthReason,
        actor: Principal,
        context: RequestContext,
        user_id: uuid.UUID | None,
        *,
        allowed: bool,
        audit_ref: uuid.UUID | None = None,
    ):
        return self._audit.event(
            action,
            reason,
            allowed=allowed,
            correlation_id=context.correlation_id,
            client_request_id=context.client_request_id,
            actor_id=actor.user_id,
            actor_role=actor.system_role,
            resource_kind="invitation" if audit_ref is not None else "user",
            resource_id=audit_ref if audit_ref is not None else user_id,
        )

    async def _deny(
        self,
        action: AuthAction,
        reason: AuthReason,
        actor: Principal,
        context: RequestContext,
        user_id: uuid.UUID | None,
    ) -> None:
        await self._audit.record_best_effort(
            self._event(action, reason, actor, context, user_id, allowed=False)
        )


async def _lock_target_in(session: AsyncSession, user_id: uuid.UUID) -> _Target | None:
    row = (
        await session.execute(
            text(
                "SELECT login_name, system_role, status FROM users "
                "WHERE id = :id FOR UPDATE"
            ),
            {"id": user_id},
        )
    ).first()
    if row is None:
        return None
    return _Target(row.login_name, row.system_role, row.status)
