"""Authorize an action and record the decision.

Audit modes (``CAPABILITIES[...].audit``, see ``capabilities.py``):

* ``REQUIRED`` (the default for every capability): every decision is written
  to the sink. **Fail-closed**: if the write fails or times out, an *allow*
  becomes a denial (``Reason.AUDIT_UNAVAILABLE``; HTTP 503), because an action
  must not happen without a record of it. A denial stays a denial.
* ``DENIED_ONLY`` (an explicit allowlist of read-only capabilities): allowed
  decisions are not recorded at all; denials are recorded best effort and an
  audit failure never blocks the read. This never applies to an agent: agent
  decisions are always ``REQUIRED``.

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
    decide_ownership_transfer,
    decide_role_change,
)
from paw_backend.authz.principals import PrincipalDirectory
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import AgentGrant, Principal, Resource, to_uuid

logger = logging.getLogger(__name__)

# Lookups that were cancelled at their deadline but have not ended yet (see
# `Authorizer._lookup`). At this many, a new lookup is refused (an audited
# denial) instead of piling one more stuck task onto a directory that is not
# answering.
_MAX_LIVE_LOOKUPS = 32


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
        # Every directory lookup that has not ended yet, in flight or abandoned
        # after its deadline (strong references), and the bound on how many may
        # exist at once (`_MAX_LIVE_LOOKUPS`): a slot is taken before a lookup
        # starts and returned only when its task has really ended.
        self._live: set[asyncio.Task[Principal | None]] = set()
        self._slots = asyncio.Semaphore(_MAX_LIVE_LOOKUPS)

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
        delegator_id: uuid.UUID | str,
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
        on the agent's very next action. A directory that fails or is slow
        (bounded by the same timeout as the audit write, however it reacts to
        cancellation; see ``_lookup``) counts as "user not active": an audited
        denial, never an error. Allowed only if both that
        user and the grant allow it (``policy.decide_agent``). The event names
        the user as ``actor_id`` and the agent as ``agent_id``.

        Agent decisions are always ``REQUIRED``, whatever the capability's own
        audit mode: an agent never acts without a recorded decision.
        """
        try:
            user_id: uuid.UUID | None = to_uuid(delegator_id, "delegator_id")
        except ValueError:
            user_id = None
        principal = None if user_id is None else await self._lookup(user_id)
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
            actor_id=user_id,
            resource=resource,
            agent=grant,
            correlation_id=correlation_id,
            client_request_id=client_request_id,
        )

    async def _lookup(self, user_id: uuid.UUID) -> Principal | None:
        """The current principal of ``user_id``; ``None`` on any failure.

        The directory is a database-backed component that can stall, and
        cancelling a stalled driver call waits for the driver's cleanup (about
        ten seconds, or for good against a server that never answers). So the
        deadline must not depend on how the directory reacts to cancellation:
        the lookup runs as its own task and is waited for with a deadline
        (``asyncio.wait``, not ``asyncio.timeout``). At the deadline the task
        is asked to stop and *abandoned*: the caller gets the fail-closed
        answer at once, and whatever the task eventually returns or raises is
        discarded, never used and never logged. A directory should still stop
        its own work on cancellation (``Database.fetch_abortable``), so that an
        abandoned lookup does not keep a connection; at most
        ``_MAX_LIVE_LOOKUPS`` lookups (in flight or abandoned) exist at a time:
        a slot is taken before a lookup starts, within the same deadline, and
        returned when its task has really ended, so a stalled directory cannot
        make concurrent requests start more lookups (or connections) than that.
        """
        if self._directory is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_seconds
        try:
            await asyncio.wait_for(self._slots.acquire(), self._timeout_seconds)
        except TimeoutError:
            logger.warning("Principal lookup refused: earlier lookups have not ended")
            return None
        lookup = asyncio.create_task(self._ask_directory(user_id))
        self._live.add(lookup)

        def ended(task: asyncio.Task[Principal | None]) -> None:
            self._live.discard(task)
            self._slots.release()
            # Retrieve the outcome so that asyncio never logs it (with the
            # message) when an abandoned lookup fails after its deadline.
            task.cancelled() or task.exception()

        lookup.add_done_callback(ended)
        try:
            await asyncio.wait({lookup}, timeout=max(0.0, deadline - loop.time()))
        finally:
            if not lookup.done():  # timed out, or this caller was cancelled
                lookup.cancel()
        if not lookup.done():
            logger.warning("Principal lookup failed (TimeoutError)")
            return None
        if lookup.cancelled():  # the directory cancelled itself
            logger.warning("Principal lookup failed (CancelledError)")
            return None
        try:
            principal = lookup.result()
        except Exception as error:
            # Type name only; a directory error can carry connection details.
            logger.warning("Principal lookup failed (%s)", type(error).__name__)
            return None
        if not isinstance(principal, Principal) or principal.user_id != user_id:
            return None  # a directory answering for someone else is refused
        return principal

    async def _ask_directory(self, user_id: uuid.UUID) -> Principal | None:
        # A coroutine of its own, so that a directory whose method is not
        # awaitable fails inside the lookup task (a denial), not in the caller.
        return await self._directory.get_principal_by_id(user_id)

    async def authorize_role_change(
        self,
        actor: Principal | None,
        target_user_id: uuid.UUID,
        target_role: SystemRole,
        new_role: SystemRole | None,
        *,
        correlation_id: uuid.UUID | None = None,
        client_request_id: str | None = None,
    ) -> Decision:
        """Decide a change of a user's system role (or their removal) and audit it.

        See ``policy.decide_role_change``. The event is about the target user
        (``resource_kind="user"``, ``resource_id``) and carries the role before
        and after. The Owner role never moves here; use
        :meth:`authorize_ownership_transfer`.
        """
        decision = decide_role_change(
            actor, target_user_id, target_role, new_role, policy=self._policy
        )
        return await self._audited(
            decision,
            principal=actor,
            resource=_user_resource(target_user_id),
            old_role=target_role,
            new_role=new_role,
            correlation_id=correlation_id,
            client_request_id=client_request_id,
        )

    async def authorize_ownership_transfer(
        self,
        actor: Principal | None,
        new_owner_id: uuid.UUID,
        new_owner_role: SystemRole,
        *,
        correlation_id: uuid.UUID | None = None,
        client_request_id: str | None = None,
    ) -> Decision:
        """Decide handing the Owner role to ``new_owner_id`` and audit it once.

        See ``policy.decide_ownership_transfer``: the only way the Owner role
        moves. One decision, one event (``old_role`` is the new Owner's role
        before, ``new_role`` is ``owner``); the actor becomes an Admin.
        """
        decision = decide_ownership_transfer(
            actor, new_owner_id, new_owner_role, policy=self._policy
        )
        return await self._audited(
            decision,
            principal=actor,
            resource=_user_resource(new_owner_id),
            old_role=new_owner_role,
            new_role=SystemRole.OWNER,
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
        old_role: SystemRole | None = None,
        new_role: SystemRole | None = None,
        correlation_id: uuid.UUID | None,
        client_request_id: str | None,
    ) -> Decision:
        event = build_event(
            decision,
            principal=principal,
            actor_id=actor_id,
            resource=resource,
            agent=agent,
            old_role=old_role,
            new_role=new_role,
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
        if (
            decision.allowed
            and agent is None  # an agent's actions are always recorded
            and _audit_mode(decision) is AuditMode.DENIED_ONLY
        ):
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


def _user_resource(user_id: object) -> Resource | None:
    """The audit resource of a user, or ``None`` if the id is malformed."""
    try:
        return Resource(kind="user", id=user_id)
    except ValueError:
        return None
