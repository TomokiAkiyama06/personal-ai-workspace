"""The user lifecycle: Invited / Active / Pending deletion / Deleted (PAW-024).

REQUIREMENTS.md "User Lifecycle" and "User Deletion Retention"; the transitions and
their rules are Decision 0033 section 3 (Approved 2026-09-28):

* (none) to ``invited``: an invitation (``invitations``);
* ``invited`` to ``active``: the invitation token is redeemed;
* ``invited`` to ``deleted``: the invitation is cancelled;
* ``active`` to ``pending_deletion``: the user is deleted;
* ``pending_deletion`` to ``active``: the Owner restores (within 30 days);
* ``pending_deletion`` to ``deleted``: NOT here. The scheduled erasure
  (``paw_backend.auth.onboarding.erasure``, Issue #127) erases the personal data
  first, as the table owner, and only then records that edge.

``TRANSITIONS`` is that list; the database function ``paw_change_user_status``
(migration ``0124``) allows exactly the same edges and is the only way the web role
changes ``users.status``. The Owner is never a target (the CLI owns that account).

Deleting (``active`` to ``pending_deletion``), in one transaction under the user's
row lock (``FOR NO KEY UPDATE``) and then the rows of the projects the user manages
(``FOR UPDATE``, the project module's lock): the status, every session of the user
ended (``account_closed``), every live pairing ended, the user's open Passkey
challenges deleted, the audit rows (the user's queued and running tasks are stopped
by ``paw_backend.orchestrator.user_sweep``, Issue #127). A user who is
the only live Manager (accepted, and whose account is ``active``) of an active or
archived project is refused (``OwnershipTransferRequiredError``). Deleting
an ``invited`` user cancels the invitation: straight to ``deleted`` (there is no
personal data to erase) and the outstanding token ends. Deleting and restoring are
sensitive operations: a recent Passkey Step-up of the administrator's own session.
"""

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.audit import AuthAction, AuthAudit, AuthReason
from paw_backend.auth.auth_policy import AuthPolicyService
from paw_backend.auth.context import RequestContext
from paw_backend.auth.db import run
from paw_backend.auth.errors import (
    AccountNotFoundError,
    AccountStateError,
    AuthPermissionError,
    OwnershipTransferRequiredError,
    RetentionExpiredError,
    StepUpRequiredError,
)
from paw_backend.auth.models import RevokeReason
from paw_backend.auth.onboarding.common import (
    StepUpGuard,
    may_administer,
    require_actor,
    require_context,
    require_uuid,
)
from paw_backend.auth.onboarding.models import InvitationEnd, PairingEnd
from paw_backend.auth.onboarding.pairing import end_live_pairings_in
from paw_backend.auth.sessions import SessionStore
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal
from paw_backend.db import Database
from paw_backend.identity import UserStatus

# REQUIREMENTS.md: a deleted user is kept 30 days; only the Owner restores meanwhile.
# Counted as 720 hours, like a project's retention: ``interval '30 days'`` on a
# timestamptz adds calendar days in the session's time zone and can be an hour off
# across a daylight saving change.
RETENTION_HOURS = 30 * 24

TRANSITIONS = frozenset(
    {
        (UserStatus.INVITED, UserStatus.ACTIVE),
        (UserStatus.INVITED, UserStatus.DELETED),
        (UserStatus.ACTIVE, UserStatus.PENDING_DELETION),
        (UserStatus.PENDING_DELETION, UserStatus.ACTIVE),
    }
)


def transition_allowed(old: UserStatus | str, new: UserStatus | str) -> bool:
    """Whether the web application may move a user from ``old`` to ``new``."""
    try:
        return (UserStatus(old), UserStatus(new)) in TRANSITIONS
    except ValueError:
        return False


class UserLifecycleService:
    """Delete (or cancel an invitation) and restore. See the module docstring."""

    def __init__(
        self,
        database: Database,
        *,
        sessions: SessionStore,
        audit: AuthAudit,
        policy: AuthPolicyService,
        timeout_seconds: float = 3.0,
    ) -> None:
        for name, value, kind in (
            ("database", database, Database),
            ("sessions", sessions, SessionStore),
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
        self._sessions = sessions
        self._audit = audit
        self._policy = policy
        self._timeout = float(timeout_seconds)

    async def delete_user(
        self,
        actor: Principal,
        user_id: uuid.UUID,
        context: RequestContext,
        *,
        session_id: uuid.UUID,
    ) -> UserStatus:
        """Delete a user (``pending_deletion``) or cancel an invitation (``deleted``).

        Returns the new status. ``AccountNotFoundError`` (no such user, or the
        Owner), ``AuthPermissionError`` (a role the actor may not delete, or the
        actor themself), ``AccountStateError`` (already pending deletion or
        deleted), ``OwnershipTransferRequiredError``, ``StepUpRequiredError``.
        """
        actor = require_actor(actor)
        require_uuid("user_id", user_id)
        require_context(context)
        require_uuid("session_id", session_id)
        action = AuthAction.USER_DELETE
        if actor.system_role not in (SystemRole.OWNER, SystemRole.ADMIN) or (
            user_id == actor.user_id
        ):
            await self._deny(
                action, AuthReason.ROLE_NOT_ALLOWED, actor, context, user_id
            )
            raise AuthPermissionError

        async def change(session: AsyncSession, row, now: datetime) -> UserStatus:
            if row.status == UserStatus.INVITED.value:
                await self._set_status_in(
                    session, user_id, UserStatus.INVITED, UserStatus.DELETED, now, actor
                )
                ended = await session.execute(
                    text(
                        "UPDATE user_invitations SET revoked_at = :now, "
                        "revoked_reason = :reason WHERE user_id = :id "
                        "AND used_at IS NULL AND revoked_at IS NULL "
                        "RETURNING audit_ref"
                    ),
                    {
                        "now": now,
                        "reason": InvitationEnd.CANCELLED.value,
                        "id": user_id,
                    },
                )
                for ref in [r.audit_ref for r in ended.all()]:
                    await self._record_in(
                        session,
                        AuthAction.INVITATION_REVOKE,
                        AuthReason.CANCELLED,
                        actor,
                        context,
                        "invitation",
                        ref,
                    )
                await self._record_in(
                    session,
                    action,
                    AuthReason.INVITATION_CANCELLED,
                    actor,
                    context,
                    "user",
                    user_id,
                )
                return UserStatus.DELETED
            if row.status != UserStatus.ACTIVE.value:
                raise AccountStateError
            if await _is_last_manager_in(session, user_id):
                raise OwnershipTransferRequiredError
            await self._set_status_in(
                session,
                user_id,
                UserStatus.ACTIVE,
                UserStatus.PENDING_DELETION,
                now,
                actor,
            )
            revoked = await self._sessions.revoke_all(
                session, user_id, RevokeReason.ACCOUNT_CLOSED
            )
            # A Passkey ceremony the user began cannot finish any more (Issue
            # #127): its challenge goes with the sessions.
            await session.execute(
                text("DELETE FROM passkey_challenges WHERE user_id = :id"),
                {"id": user_id},
            )
            for ref in await end_live_pairings_in(
                session, user_id, PairingEnd.ACCOUNT_CLOSED, now
            ):
                await self._record_in(
                    session,
                    AuthAction.PAIRING_REVOKE,
                    AuthReason.ACCOUNT_CLOSED,
                    actor,
                    context,
                    "device_pairing",
                    ref,
                )
            await self._record_in(
                session,
                action,
                AuthReason.DELETION_PENDING,
                actor,
                context,
                "user",
                user_id,
            )
            if revoked:
                await self._record_in(
                    session,
                    AuthAction.SESSION_REVOKE_ALL,
                    AuthReason.REVOKED_ALL,
                    actor,
                    context,
                    "user",
                    user_id,
                )
            return UserStatus.PENDING_DELETION

        return await self._administer(
            action, actor, user_id, context, session_id, change, owner_only=False
        )

    async def restore_user(
        self,
        actor: Principal,
        user_id: uuid.UUID,
        context: RequestContext,
        *,
        session_id: uuid.UUID,
    ) -> UserStatus:
        """The Owner brings a user who is pending deletion back (within 30 days).

        The user's sessions stay ended (they sign in again). ``RetentionExpiredError``
        once the 30 days have passed (judged at the database's clock).
        """
        actor = require_actor(actor)
        require_uuid("user_id", user_id)
        require_context(context)
        require_uuid("session_id", session_id)
        action = AuthAction.USER_RESTORE
        if actor.system_role is not SystemRole.OWNER:
            await self._deny(
                action, AuthReason.ROLE_NOT_ALLOWED, actor, context, user_id
            )
            raise AuthPermissionError

        async def change(session: AsyncSession, row, now: datetime) -> UserStatus:
            if row.status != UserStatus.PENDING_DELETION.value:
                raise AccountStateError
            within = (
                await session.execute(
                    text(
                        "SELECT max(changed_at) + make_interval(hours => :hours) "
                        "> greatest(CAST(:now AS timestamptz), clock_timestamp()) "
                        "FROM user_status_changes WHERE user_id = :id "
                        "AND new_status = 'pending_deletion'"
                    ),
                    {"hours": RETENTION_HOURS, "now": now, "id": user_id},
                )
            ).scalar_one()
            if within is not True:
                raise RetentionExpiredError
            await self._set_status_in(
                session,
                user_id,
                UserStatus.PENDING_DELETION,
                UserStatus.ACTIVE,
                now,
                actor,
            )
            await self._record_in(
                session, action, AuthReason.RESTORED, actor, context, "user", user_id
            )
            return UserStatus.ACTIVE

        return await self._administer(
            action, actor, user_id, context, session_id, change, owner_only=True
        )

    async def _administer(
        self, action, actor, user_id, context, session_id, change, *, owner_only
    ) -> UserStatus:
        guard = StepUpGuard(self._policy)
        refused: AuthReason | None = None

        async def work(session: AsyncSession) -> UserStatus:
            nonlocal refused
            now = self._audit.now()
            await guard.require_in(
                session, session_id=session_id, user_id=actor.user_id, now=now
            )
            row = (
                await session.execute(
                    # NO KEY UPDATE: the status is not a key, and the lock must
                    # not exclude the KEY SHARE that a foreign key to the user
                    # takes (the project module inserts a membership while it
                    # holds the project row, which this then waits for).
                    text(
                        "SELECT system_role, status FROM users WHERE id = :id "
                        "FOR NO KEY UPDATE"
                    ),
                    {"id": user_id},
                )
            ).first()
            if row is None or row.system_role == SystemRole.OWNER.value:
                raise AccountNotFoundError
            if not owner_only and not may_administer(actor, row.system_role):
                refused = AuthReason.ROLE_NOT_ALLOWED
                raise AuthPermissionError
            try:
                return await change(session, row, now)
            except AccountStateError:
                refused = AuthReason.INVALID_STATE
                raise
            except OwnershipTransferRequiredError:
                refused = AuthReason.OWNERSHIP_TRANSFER_REQUIRED
                raise
            except RetentionExpiredError:
                refused = AuthReason.RETENTION_EXPIRED
                raise

        try:
            return await run(self._database, work, self._timeout)
        except (
            StepUpRequiredError,
            AuthPermissionError,
            AccountStateError,
            OwnershipTransferRequiredError,
            RetentionExpiredError,
        ):
            reason = guard.refused or refused
            if reason is not None:
                await self._deny(action, reason, actor, context, user_id)
            raise

    async def _set_status_in(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        old: UserStatus,
        new: UserStatus,
        now: datetime,
        actor: Principal,
    ) -> None:
        if not transition_allowed(old, new):  # a programming error
            raise AccountStateError
        changed = (
            await session.execute(
                text("SELECT paw_change_user_status(:id, :old, :new, :now, :actor)"),
                {
                    "id": user_id,
                    "old": old.value,
                    "new": new.value,
                    "now": now,
                    "actor": actor.user_id,
                },
            )
        ).scalar_one()
        if not changed:  # cannot happen under the row lock
            raise AccountStateError

    async def _record_in(
        self, session, action, reason, actor, context, kind, resource_id
    ) -> None:
        await self._audit.record_in(
            session,
            self._audit.event(
                action,
                reason,
                allowed=True,
                correlation_id=context.correlation_id,
                client_request_id=context.client_request_id,
                actor_id=actor.user_id,
                actor_role=actor.system_role,
                resource_kind=kind,
                resource_id=resource_id,
            ),
        )

    async def _deny(self, action, reason, actor, context, user_id) -> None:
        await self._audit.record_best_effort(
            self._audit.event(
                action,
                reason,
                allowed=False,
                correlation_id=context.correlation_id,
                client_request_id=context.client_request_id,
                actor_id=actor.user_id,
                actor_role=actor.system_role,
                resource_kind="user",
                resource_id=user_id,
            )
        )


async def _is_last_manager_in(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether the user is the only live Manager of an active / archived project.

    "Live": an accepted Manager whose own account is ``active`` (a co-Manager who
    is pending deletion hands nothing over). The projects the user manages are
    locked ``FOR UPDATE`` first, in the order of their ids, the lock the project
    module takes for every membership change (``projects.service``): a co-Manager
    leaving, being demoted or being deleted at the same time commits first and
    the check below (a new statement, so a new snapshot) sees it. Becoming a
    Manager of a project the user does not manage yet (creating one, accepting
    an invitation, a promotion) locks the user's row ``FOR SHARE``, which waits
    for the caller's ``FOR NO KEY UPDATE`` of it: such a project is either seen
    here or refused there (``AccountNotActiveError``).
    """
    await session.execute(
        text(
            """
            SELECT p.id FROM projects p
             WHERE p.id IN (SELECT m.project_id FROM project_members m
                             WHERE m.user_id = :id AND m.status = 'active'
                               AND m.role = 'manager')
             ORDER BY p.id FOR UPDATE OF p
            """
        ),
        {"id": user_id},
    )
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM project_members m JOIN projects p ON p.id = m.project_id
                 WHERE m.user_id = :id AND m.status = 'active' AND m.role = 'manager'
                   AND p.status IN ('active', 'archived')
                   AND NOT EXISTS (
                       SELECT 1 FROM project_members o
                         JOIN users u ON u.id = o.user_id AND u.status = 'active'
                        WHERE o.project_id = m.project_id AND o.user_id <> :id
                          AND o.status = 'active' AND o.role = 'manager')
                 LIMIT 1
                """
            ),
            {"id": user_id},
        )
    ).first()
    return row is not None
