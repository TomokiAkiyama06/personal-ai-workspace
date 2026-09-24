"""Audit events of the Owner setup, recovery and token redemption.

The events go through the existing ``AuditSink`` (PAW-025) and carry ids and
enum values only. A token appears as its ``audit_ref``, never as its lookup id
(the lookup id is part of the token string: whoever knows it can spend the
token's attempts).
"""

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from paw_backend.authz.audit import AuditEvent, AuditSink
from paw_backend.authz.roles import SystemRole
from paw_backend.identity.errors import AuditUnavailableError

logger = logging.getLogger(__name__)

TOKEN_KIND = "setup_token"


class AuditAction(StrEnum):
    """``AuditEvent.action`` values written by the identity services."""

    OWNER_CREATE = "owner.create"
    OWNER_REPLACE = "owner.replace"
    SETUP_TOKEN_ISSUE = "owner.setup_token.issue"
    RECOVERY_TOKEN_ISSUE = "owner.recovery_token.issue"
    TOKEN_REVOKE = "owner.token.revoke"
    TOKEN_REDEEM = "owner.token.redeem"


class AuditReason(StrEnum):
    """``AuditEvent.reason`` values. Only the audit trail sees the fine reasons."""

    # allow
    CREATED = "created"
    REPLACED = "replaced"
    ISSUED = "issued"
    SUPERSEDED = "superseded"
    REDEEMED = "redeemed"
    # deny
    OWNER_EXISTS = "owner_exists"
    OWNER_NOT_LIVE = "owner_not_live"
    LOGIN_NAME_TAKEN = "login_name_taken"
    OWNER_MISSING = "owner_missing"
    TOKEN_MISMATCH = "token_mismatch"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_USED = "token_used"
    TOKEN_REVOKED = "token_revoked"
    TOKEN_UNAVAILABLE = "token_unavailable"  # consumed / revoked while redeeming
    USER_NOT_ELIGIBLE = "user_not_eligible"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class IdentityAudit:
    """Builds and stores the events of one service (clock, timeout, sink)."""

    def __init__(
        self,
        sink: AuditSink,
        *,
        timeout_seconds: float,
        clock: Callable[[], datetime] | None,
    ) -> None:
        _require_sink(sink)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if not isinstance(timeout_seconds, int | float) or not (
            0 < timeout_seconds <= 60
        ):
            raise ValueError("audit_timeout_seconds must be in (0, 60]")
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
        action: AuditAction,
        reason: AuditReason,
        *,
        correlation_id: uuid.UUID,
        allowed: bool = True,
        actor_id: uuid.UUID | None = None,
        actor_role: SystemRole | None = SystemRole.SYSTEM,
        resource_kind: str = "user",
        resource_id: uuid.UUID | None = None,
        old_role: SystemRole | None = None,
        new_role: SystemRole | None = None,
    ) -> AuditEvent:
        """An event. By default the actor is the backend itself (the local CLI)."""
        return AuditEvent(
            event_id=uuid.uuid4(),
            correlation_id=correlation_id,
            occurred_at=self.now(),
            actor_id=actor_id,
            actor_role=actor_role.value if actor_role is not None else None,
            action=action.value,
            resource_kind=resource_kind,
            resource_id=resource_id,
            decision="allow" if allowed else "deny",
            reason=reason.value,
            old_role=old_role.value if old_role is not None else None,
            new_role=new_role.value if new_role is not None else None,
        )

    def token_event(
        self,
        action: AuditAction,
        reason: AuditReason,
        correlation_id: uuid.UUID,
        audit_ref: uuid.UUID,
        *,
        allowed: bool = True,
        actor_role: SystemRole | None = SystemRole.SYSTEM,
    ) -> AuditEvent:
        """An event about a token, which it names by ``audit_ref`` only."""
        return self.event(
            action,
            reason,
            correlation_id=correlation_id,
            allowed=allowed,
            actor_role=actor_role,
            resource_kind=TOKEN_KIND,
            resource_id=audit_ref,
        )

    async def record(self, event: AuditEvent) -> None:
        """Store ``event``; ``AuditUnavailableError`` if that fails."""
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
            raise AuditUnavailableError from None

    async def record_best_effort(self, event: AuditEvent) -> None:
        """For events about something that was refused anyway."""
        try:
            await self.record(event)
        except AuditUnavailableError:
            pass  # already logged; the refusal stands


def require_int(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an integer from {low} to {high}")


def _require_sink(audit: object) -> None:
    record = getattr(audit, "record", None)
    if not callable(record) or not inspect.iscoroutinefunction(record):
        raise TypeError("audit must have an async record(event) method")
    try:
        inspect.signature(record).bind(object())
    except TypeError:
        raise TypeError("audit.record must accept one event argument") from None
