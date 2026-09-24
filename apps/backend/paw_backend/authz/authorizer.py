"""Authorize an action and record the decision.

Every decision is turned into an :class:`AuditEvent` and handed to the sink.

Fail-closed rule: when the audit write fails (or times out) and the requested
capability is *privileged* (``CAPABILITIES[...].privileged``), an allow is
turned into a denial with ``Reason.AUDIT_UNAVAILABLE``: an administrative or
permission-changing action must not happen without a record of it. For every
other capability an unwritable audit trail does not stop the action (so an
audit outage cannot take the whole workspace down); the failure is logged with
the exception type only. A denial stays a denial either way.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from paw_backend.authz.audit import AuditSink, build_event
from paw_backend.authz.capabilities import CAPABILITIES, Capability
from paw_backend.authz.policy import (
    DEFAULT_POLICY,
    Decision,
    Policy,
    Reason,
    decide,
    decide_agent,
)
from paw_backend.authz.subjects import AgentGrant, Principal, Resource

logger = logging.getLogger(__name__)


class Authorizer:
    """Decides in the backend, then audits. The only entry point for callers."""

    def __init__(
        self,
        sink: AuditSink,
        *,
        policy: Policy = DEFAULT_POLICY,
        timeout_seconds: float = 3.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sink = sink
        self._policy = policy
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    async def authorize(
        self,
        principal: Principal | None,
        capability: Capability | str,
        resource: Resource | None,
        *,
        request_id: str | None = None,
    ) -> Decision:
        """Decide for a human user and audit the decision."""
        decision = decide(principal, capability, resource, policy=self._policy)
        return await self._audited(
            decision,
            principal=principal,
            resource=resource,
            agent=None,
            request_id=request_id,
        )

    async def authorize_agent_action(
        self,
        delegator: Principal | None,
        grant: AgentGrant,
        capability: Capability | str,
        resource: Resource | None,
        *,
        request_id: str | None = None,
    ) -> Decision:
        """Decide for an agent acting on behalf of ``delegator`` and audit it.

        Allowed only if both the delegating user and the grant allow it; see
        ``policy.decide_agent``. The event names the user as ``actor_id`` and
        the agent as ``agent_id``.
        """
        decision = decide_agent(
            delegator, grant, capability, resource, policy=self._policy
        )
        return await self._audited(
            decision,
            principal=delegator,
            resource=resource,
            agent=grant,
            request_id=request_id,
        )

    async def _audited(
        self,
        decision: Decision,
        *,
        principal: Principal | None,
        resource: Resource | None,
        agent: AgentGrant | None,
        request_id: str | None,
    ) -> Decision:
        event = build_event(
            decision,
            principal=principal,
            resource=resource,
            agent=agent,
            request_id=request_id,
            occurred_at=self._clock(),
        )
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
            if decision.allowed and _is_privileged(decision.capability):
                return replace(decision, allowed=False, reason=Reason.AUDIT_UNAVAILABLE)
        return decision


def _is_privileged(capability: Capability | None) -> bool:
    # An unknown capability is never allowed in the first place; if one ever
    # got here anyway, treating it as privileged keeps the failure closed.
    return capability is None or CAPABILITIES[capability].privileged
