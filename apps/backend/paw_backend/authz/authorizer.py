"""Authorize an action and record the decision.

Audit modes (``CAPABILITIES[...].audit``, see ``capabilities.py``):

* ``REQUIRED`` (the default for every capability): every decision is written
  to the sink. **Fail-closed**: if the write fails or times out, an *allow*
  becomes a denial (``Reason.AUDIT_UNAVAILABLE``; HTTP 503), because an action
  must not happen without a record of it. A denial stays a denial.
* ``DENIED_ONLY`` (an explicit allowlist of read-only capabilities): allowed
  decisions are not recorded at all; denials are recorded best effort and an
  audit failure never blocks the read.

Unauthenticated denials are never written to the database: anyone on the
network could create them at will, and the table cannot be pruned. They are
logged as structured, exception-free fields instead.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from paw_backend.authz.audit import AuditSink, build_event
from paw_backend.authz.capabilities import CAPABILITIES, AuditMode, Capability
from paw_backend.authz.policy import (
    DEFAULT_POLICY,
    Decision,
    Policy,
    Reason,
    decide,
    decide_agent,
    decide_role_change,
)
from paw_backend.authz.principals import PrincipalDirectory
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import AgentGrant, Principal, Resource

logger = logging.getLogger(__name__)


class Authorizer:
    """Decides in the backend, then audits. The only entry point for callers.

    Every method returns a :class:`Decision`; ``bool(decision)`` is
    ``decision.allowed``, so ``if await authorizer.authorize(...)`` is safe.
    """

    def __init__(
        self,
        sink: AuditSink,
        *,
        policy: Policy = DEFAULT_POLICY,
        directory: PrincipalDirectory | None = None,
        timeout_seconds: float = 3.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sink = sink
        self._policy = policy
        self._directory = directory
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    async def authorize(
        self,
        principal: Principal | None,
        capability: Capability,
        resource: Resource | None,
        *,
        correlation_id: uuid.UUID | None = None,
        client_request_id: str | None = None,
    ) -> Decision:
        """Decide for a human user and audit the decision."""
        decision = decide(principal, capability, resource, policy=self._policy)
        return await self._audited(
            decision,
            principal=principal,
            resource=resource,
            correlation_id=correlation_id,
            client_request_id=client_request_id,
        )

    async def authorize_agent_action(
        self,
        delegator_id: uuid.UUID,
        grant: AgentGrant,
        capability: Capability,
        resource: Resource | None,
        *,
        correlation_id: uuid.UUID | None = None,
        client_request_id: str | None = None,
    ) -> Decision:
        """Decide for an agent acting on behalf of the user ``delegator_id``.

        The delegating user's principal is looked up again through the
        directory on every call, so revoking a role or the user takes effect
        on the agent's very next action. Allowed only if both that user and
        the grant allow it (``policy.decide_agent``). The event names the user
        as ``actor_id`` and the agent as ``agent_id``.
        """
        principal = None
        if self._directory is not None:
            principal = await self._directory.get_principal_by_id(delegator_id)
        if principal is not None and principal.user_id != delegator_id:
            principal = None  # a directory answering for someone else is refused
        if principal is None:
            decision = Decision.deny(
                Reason.DELEGATOR_NOT_ACTIVE,
                capability if isinstance(capability, Capability) else None,
            )
        else:
            decision = decide_agent(
                principal, grant, capability, resource, policy=self._policy
            )
        return await self._audited(
            decision,
            principal=principal,
            actor_id=delegator_id,
            resource=resource,
            agent=grant,
            correlation_id=correlation_id,
            client_request_id=client_request_id,
        )

    async def authorize_role_change(
        self,
        actor: Principal | None,
        target_role: SystemRole,
        new_role: SystemRole | None,
        *,
        target_user_id: uuid.UUID | None = None,
        correlation_id: uuid.UUID | None = None,
        client_request_id: str | None = None,
    ) -> Decision:
        """Decide a change of a user's system role (or their removal) and audit it.

        See ``policy.decide_role_change``: an Admin cannot manage the Owner or
        other Admins, and nobody changes their own role.
        """
        decision = decide_role_change(
            actor,
            target_role,
            new_role,
            target_user_id=target_user_id,
            policy=self._policy,
        )
        return await self._audited(
            decision,
            principal=actor,
            resource=Resource.system(),
            correlation_id=correlation_id,
            client_request_id=client_request_id,
        )

    async def _audited(
        self,
        decision: Decision,
        *,
        principal: Principal | None,
        resource: Resource | None,
        actor_id: uuid.UUID | None = None,
        agent: AgentGrant | None = None,
        correlation_id: uuid.UUID | None,
        client_request_id: str | None,
    ) -> Decision:
        event = build_event(
            decision,
            principal=principal,
            actor_id=actor_id,
            resource=resource,
            agent=agent,
            correlation_id=correlation_id,
            client_request_id=client_request_id,
            occurred_at=self._clock(),
        )
        if decision.reason is Reason.UNAUTHENTICATED:
            # Not persisted (see the module docstring). Only validated,
            # fixed-alphabet fields; never a message from an exception.
            logger.info(
                "authz.denied reason=%s action=%s resource_kind=%s "
                "correlation_id=%s client_request_id=%s",
                event.reason,
                event.action,
                event.resource_kind,
                event.correlation_id,
                event.client_request_id,
            )
            return decision
        if decision.allowed and _audit_mode(decision) is AuditMode.DENIED_ONLY:
            return decision
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._sink.record(event)
        except Exception as error:
            # Type name only: driver messages can name the host or the user.
            logger.log(
                logging.ERROR if decision.allowed else logging.WARNING,
                "Audit write failed (%s) for action %s",
                type(error).__name__,
                event.action,
            )
            if decision.allowed:
                return replace(decision, allowed=False, reason=Reason.AUDIT_UNAVAILABLE)
        return decision


def _audit_mode(decision: Decision) -> AuditMode:
    if decision.capability is None:
        return AuditMode.REQUIRED
    return CAPABILITIES[decision.capability].audit
