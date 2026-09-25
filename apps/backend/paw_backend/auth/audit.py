"""Audit events of the authentication code.

Every event goes into the existing ``audit_events`` trail (PAW-025) as an
``AuditEvent``: ids and enum values only. Never a password, a hash, a session id,
a token or a login name. A failed login records the account's id when the
account exists (an unknown name is only logged, without the name: anyone could
otherwise write rows into a table that cannot be pruned) and a pseudonymous id
of the source bucket (``tokens.source_audit_id``).

Two ways to write, chosen by what the event is:

* ``record_in(session, event)``: an insert in the caller's transaction. The
  event of a change (a login that creates a session, a password change, a
  revocation) commits with the change or not at all: **fail-closed, and no
  event for something that did not happen**. A failure raises and the caller's
  transaction rolls back.
* ``record(event)`` / ``record_best_effort(event)``: the ``AuditSink`` of the
  application, in its own short transaction, for events about something that
  was refused (a denial stays a denial when it cannot be recorded).
"""

import asyncio
import inspect
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.errors import AuthUnavailableError
from paw_backend.authz.audit import AuditEvent, AuditSink
from paw_backend.authz.models import AuditEventRecord
from paw_backend.authz.roles import SystemRole

logger = logging.getLogger(__name__)


class AuthAction(StrEnum):
    """``AuditEvent.action`` values written by the authentication code."""

    LOGIN = "auth.login"
    LOCKOUT = "auth.lockout"
    UNLOCK = "auth.unlock"
    LOGOUT = "auth.logout"
    SESSION_REVOKE = "auth.session.revoke"
    SESSION_REVOKE_OTHERS = "auth.session.revoke_others"
    SESSION_REVOKE_ALL = "auth.session.revoke_all"
    PASSWORD_CHANGE = "auth.password.change"
    PASSWORD_SET = "auth.password.set"
    STEP_UP = "auth.step_up"
    POLICY_UPDATE = "auth.policy.update"


class AuthReason(StrEnum):
    # allow
    AUTHENTICATED = "authenticated"
    LOGGED_OUT = "logged_out"
    REVOKED = "revoked"
    REVOKED_OTHERS = "revoked_others"
    REVOKED_ALL = "revoked_all"
    CHANGED = "changed"
    SETUP = "setup"
    RECOVERY = "recovery"
    VERIFIED = "verified"
    UNLOCKED = "unlocked"
    UPDATED = "updated"
    # deny
    INVALID_CREDENTIALS = "invalid_credentials"
    ACCOUNT_NOT_ACTIVE = "account_not_active"
    NO_PASSWORD = "no_password"
    CREDENTIALS_CHANGED = "credentials_changed"
    BACKOFF_STARTED = "backoff_started"
    ROLE_NOT_ALLOWED = "role_not_allowed"
    STEP_UP_REQUIRED = "step_up_required"
    VERSION_CONFLICT = "version_conflict"


class AuthAudit:
    """Builds and stores the events of the authentication service."""

    def __init__(self, sink: AuditSink, *, timeout_seconds: float, clock=None) -> None:
        record = getattr(sink, "record", None)
        if not callable(record) or not inspect.iscoroutinefunction(record):
            raise TypeError("sink must have an async record(event) method")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be in (0, 60]")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._sink = sink
        self._timeout = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    def event(
        self,
        action: AuthAction,
        reason: AuthReason,
        *,
        allowed: bool,
        correlation_id: uuid.UUID,
        client_request_id: str | None = None,
        actor_id: uuid.UUID | None = None,
        actor_role: SystemRole | str | None = None,
        resource_kind: str,
        resource_id: uuid.UUID | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            event_id=uuid.uuid4(),
            correlation_id=correlation_id,
            occurred_at=self.now(),
            actor_id=actor_id,
            actor_role=SystemRole(actor_role).value if actor_role is not None else None,
            action=action.value,
            resource_kind=resource_kind,
            resource_id=resource_id,
            decision="allow" if allowed else "deny",
            reason=reason.value,
            client_request_id=client_request_id,
        )

    async def record_in(self, session: AsyncSession, event: AuditEvent) -> None:
        """Insert ``event`` in ``session``'s transaction (committed or lost with it)."""
        await session.execute(
            insert(AuditEventRecord).values(
                id=event.event_id,
                correlation_id=event.correlation_id,
                occurred_at=event.occurred_at,
                actor_id=event.actor_id,
                actor_role=event.actor_role,
                agent_id=event.agent_id,
                action=event.action,
                resource_kind=event.resource_kind,
                resource_id=event.resource_id,
                project_id=event.project_id,
                repo_id=event.repo_id,
                repo_acl=event.repo_acl,
                decision=event.decision,
                reason=event.reason,
                old_role=event.old_role,
                new_role=event.new_role,
                client_request_id=event.client_request_id,
            )
        )

    async def record(self, event: AuditEvent) -> None:
        """Store ``event`` through the sink; ``AuthUnavailableError`` if that fails."""
        try:
            async with asyncio.timeout(self._timeout):
                await self._sink.record(event)
        except Exception as error:
            # Type name only: a driver message can name the host or the user.
            logger.error(
                "Audit write failed (%s) for action %s",
                type(error).__name__,
                event.action,
            )
            raise AuthUnavailableError from None

    async def record_best_effort(self, event: AuditEvent) -> None:
        """For events about something that was refused anyway."""
        try:
            await self.record(event)
        except AuthUnavailableError:
            pass  # already logged; the refusal stands
