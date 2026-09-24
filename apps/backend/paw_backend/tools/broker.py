"""The Tool Broker: decides whether a tool call may run. It never runs anything.

``ToolBroker.request(call)`` returns ``ALLOW``, ``NEEDS_APPROVAL`` or ``DENY``
with a stable reason code. Running an allowed call is another object's job
(:class:`~.runner.ToolRunner` and the injected ``ToolExecutor``); the broker
performs no tool side effect. The order of the checks, each of which can only
narrow what the previous one allowed:

1. the call is well formed and names a registered tool (exact name);
2. the tool is not one that returns credential plaintext, and no argument holds
   credential plaintext; arguments match the tool's declared schema and every
   target (path / host / URL / project / credential handle) is normalised;
3. the targets are compared with the task scope (symlinks resolved), and the
   tool's classes, environment and that result give a level through the
   :class:`~.policy.ToolPolicy`; ``DENY`` ends the call;
4. the PAW-025 authorization decision for the tool's capability: the delegating
   user's rights intersected with the agent grant. The broker adds to this and
   never replaces it: an approval cannot make an operation the agent may not do.
   A call that touches a repository of the task's working set (it names the
   repository, a path lies in its worktree, or a URL lies below a remote the
   backend registered for it) is decided on that **repository** with its
   resolved ACL, so a read-only override or a "no agents" override applies; an
   ACL the backend could not resolve denies. A URL of such a call that lies
   below no remote of the working set is refused at step 3
   (``remote_not_in_repository``): the executor is never given an endpoint whose
   repository was not authorized. A repository write that touches no repository
   of the working set is denied (``repository_not_identified``);
5. the task budget (:class:`~.budget.BudgetProvider`); unknown means denied;
6. ``AUTO`` / ``SCOPED_AUTO`` are allowed; ``APPROVAL`` / ``STRONG_APPROVAL``
   need an approval bound to this exact call. **The task must still be able to
   act** (:class:`~.task_state.TaskActivityProvider`: not completed / failed /
   cancelled, and not unknown or unreadable) before an approval is opened or
   used: an approval whose revocation failed when its task ended cannot be
   used, whatever the store still says. Then, without an approval a request is
   opened (``NEEDS_APPROVAL``); with one it is consumed atomically (single
   use) or the call is denied with the reason (expired, replayed, for another
   call...). The check before the use gives the early, precise reason; it can be
   overtaken by the end of the task, so the store checks the task **again in the
   same transaction that consumes** (``require_active_task``: the task row is
   read locked), and a task that ended in between consumes nothing
   (``task_not_active``).

The decision is audited (ids and enums only). An ``ALLOW`` that cannot be
recorded becomes a ``DENY`` (``audit_unavailable``): a tool never runs without
a record of the decision to run it.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from paw_backend.authz import (
    CAPABILITIES,
    AuditSink,
    Authorizer,
    Decision,
    Reason,
    RepoPermission,
    Resource,
    Scope,
)
from paw_backend.authz.capabilities import REPO_PERMISSION_OF
from paw_backend.tools.approval_types import (
    ApprovalBinding,
    ApprovalEvent,
    ApprovalEventKind,
    ApprovalStore,
    ConsumeOutcome,
    NewApproval,
    OpenLimits,
    OpenOutcome,
    OpenResult,
)
from paw_backend.tools.approvals import ApprovalListeners, Listener
from paw_backend.tools.audit import build_tool_event, record_event, tool_action
from paw_backend.tools.budget import (
    BudgetProvider,
    BudgetStatus,
    FailClosedBudgetProvider,
)
from paw_backend.tools.calls import (
    ArgumentError,
    ParsedArguments,
    TaskContext,
    ToolCall,
    ToolInvocation,
    compute_call_hash,
    parse_arguments,
)
from paw_backend.tools.capabilities import ApprovalLevel, most_restrictive
from paw_backend.tools.decisions import BrokerDecision, BrokerReason, Verdict
from paw_backend.tools.interfaces import require_async_method
from paw_backend.tools.policy import DEFAULT_TOOL_POLICY, ToolPolicy
from paw_backend.tools.registry import ToolRegistry, ToolSpec
from paw_backend.tools.scope import (
    Classification,
    PathResolutionError,
    PathResolver,
    RealpathResolver,
    TargetKind,
    classify_targets,
)
from paw_backend.tools.task_state import (
    FailClosedTaskActivity,
    TaskActivity,
    TaskActivityProvider,
)

logger = logging.getLogger(__name__)

MIN_APPROVAL_TTL = timedelta(minutes=1)
MAX_APPROVAL_TTL = timedelta(hours=24)
DEFAULT_APPROVAL_TTL = timedelta(hours=1)

_OUT_OF_SCOPE_REASON = {
    TargetKind.PATH: BrokerReason.PATH_OUT_OF_SCOPE,
    TargetKind.HOST: BrokerReason.HOST_OUT_OF_SCOPE,
    TargetKind.PROJECT: BrokerReason.PROJECT_OUT_OF_SCOPE,
    TargetKind.CREDENTIAL: BrokerReason.CREDENTIAL_OUT_OF_SCOPE,
    TargetKind.REPOSITORY: BrokerReason.REPOSITORY_OUT_OF_SCOPE,
    TargetKind.URL: BrokerReason.REMOTE_NOT_IN_REPOSITORY,
}
_CONSUME_REASON = {
    ConsumeOutcome.NOT_FOUND: BrokerReason.APPROVAL_NOT_FOUND,
    ConsumeOutcome.MISMATCH: BrokerReason.APPROVAL_MISMATCH,
    ConsumeOutcome.EXPIRED: BrokerReason.APPROVAL_EXPIRED,
    ConsumeOutcome.ALREADY_USED: BrokerReason.APPROVAL_ALREADY_USED,
    ConsumeOutcome.REJECTED: BrokerReason.APPROVAL_REJECTED,
    ConsumeOutcome.REVOKED: BrokerReason.APPROVAL_REVOKED,
    # The task ended between the broker's check and the use (the store checks it
    # again in the step that consumes).
    ConsumeOutcome.TASK_NOT_ACTIVE: BrokerReason.TASK_NOT_ACTIVE,
    ConsumeOutcome.TASK_UNKNOWN: BrokerReason.TASK_UNKNOWN,
}
_OPEN_REFUSAL_REASON = {
    OpenOutcome.TOO_MANY_PENDING: BrokerReason.APPROVAL_LIMIT_REACHED,
    OpenOutcome.COOLING_DOWN: BrokerReason.APPROVAL_COOLDOWN,
}


class ToolBroker:
    """Decides tool calls. See the module docstring for the order of the checks."""

    def __init__(
        self,
        registry: ToolRegistry,
        authorizer: Authorizer,
        approvals: ApprovalStore,
        audit: AuditSink,
        *,
        policy: ToolPolicy = DEFAULT_TOOL_POLICY,
        budget: BudgetProvider | None = None,
        task_activity: TaskActivityProvider | None = None,
        path_resolver: PathResolver | None = None,
        approval_ttl: timedelta = DEFAULT_APPROVAL_TTL,
        max_pending_approvals: int = 10,
        rejection_cooldown: timedelta = timedelta(minutes=5),
        listeners: Sequence[Listener] = (),
        timeout_seconds: float = 3.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be a ToolRegistry")
        if not isinstance(policy, ToolPolicy):
            raise TypeError("policy must be a ToolPolicy")
        require_async_method(authorizer, "authorize_agent_action", 4)
        for name, count in (("open_request", 1), ("consume", 2)):
            require_async_method(approvals, name, count)
        require_async_method(audit, "record", 1)
        self._budget: BudgetProvider = budget or FailClosedBudgetProvider()
        require_async_method(self._budget, "check", 2)
        require_async_method(self._budget, "charge", 2)
        self._task_activity: TaskActivityProvider = (
            FailClosedTaskActivity() if task_activity is None else task_activity
        )
        require_async_method(self._task_activity, "check", 1)
        self._resolver: PathResolver = path_resolver or RealpathResolver()
        require_async_method(self._resolver, "resolve", 1)
        if not isinstance(approval_ttl, timedelta) or not (
            MIN_APPROVAL_TTL <= approval_ttl <= MAX_APPROVAL_TTL
        ):
            raise ValueError("approval_ttl must be between 1 minute and 24 hours")
        if not timeout_seconds > 0:
            raise ValueError("timeout_seconds must be positive")
        # At most this many open approvals per (task, user), and a rejected call
        # is not asked again for the cooldown (both bounded, see OpenLimits).
        self._limits = OpenLimits(max_pending_approvals, rejection_cooldown)
        self._registry = registry
        self._policy = policy
        self._authorizer = authorizer
        self._approvals = approvals
        self._audit = audit
        self._approval_ttl = approval_ttl
        self._listeners = ApprovalListeners(listeners, timeout_seconds=timeout_seconds)
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    # ------------------------------------------------------------------ API

    async def request(
        self, call: ToolCall, *, approval_id: uuid.UUID | None = None
    ) -> BrokerDecision:
        """Decide ``call``. Pass the ``approval_id`` of an approved request to
        use it (each approval is good for one call, once).

        Never raises for a bad ``call``; never executes anything.
        """
        if not isinstance(call, ToolCall) or not isinstance(call.context, TaskContext):
            # No task, user or agent to attribute a row to: log only.
            logger.warning("Tool call refused: not a ToolCall")
            return BrokerDecision(Verdict.DENY, BrokerReason.INVALID_CALL, uuid.uuid4())
        correlation_id = (
            call.correlation_id
            if isinstance(call.correlation_id, uuid.UUID)
            else uuid.uuid4()
        )
        decision = await self._evaluate(call, correlation_id, approval_id)
        return await self._audited(decision, call.context)

    async def record_execution(
        self, decision: BrokerDecision, *, succeeded: bool
    ) -> None:
        """Record that an allowed call ran: the audit row and the budget charge.

        Called by the runner after the executor returned or failed. It never
        raises for a store failure (the call already ran).
        """
        invocation = decision.invocation
        if not decision.allowed or invocation is None:
            raise ValueError("only an allowed call can have been executed")
        context = invocation.context
        reason = BrokerReason.EXECUTED if succeeded else BrokerReason.EXECUTION_FAILED
        await record_event(
            self._audit,
            build_tool_event(
                action=tool_action(invocation.tool),
                allowed=True,
                reason=reason.value,
                correlation_id=invocation.correlation_id,
                occurred_at=self._clock(),
                resource_kind="task",
                resource_id=context.task_id,
                project_id=context.primary_project_id,
                actor_id=context.delegator_id,
                agent_id=context.grant.agent_id,
            ),
            self._timeout_seconds,
        )
        spec = self._registry.get(invocation.tool)
        if spec is not None and spec.requires_budget:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    await self._budget.charge(context.task_id, invocation.tool)
            except Exception as error:
                logger.error("Budget charge failed (%s)", type(error).__name__)

    # ------------------------------------------------------------- decision

    def _refuse(
        self,
        reason: BrokerReason,
        correlation_id: uuid.UUID,
        *,
        tool: str | None = None,
        level: ApprovalLevel | None = None,
        call_hash: str | None = None,
        approval_id: uuid.UUID | None = None,
        authz_reason: Reason | None = None,
    ) -> BrokerDecision:
        return BrokerDecision(
            Verdict.DENY,
            reason,
            correlation_id,
            tool=tool,
            level=level,
            call_hash=call_hash,
            approval_id=approval_id,
            authz_reason=authz_reason,
        )

    async def _evaluate(
        self,
        call: ToolCall,
        correlation_id: uuid.UUID,
        approval_id: uuid.UUID | None,
    ) -> BrokerDecision:
        context = call.context
        if approval_id is not None and not isinstance(approval_id, uuid.UUID):
            return self._refuse(BrokerReason.INVALID_CALL, correlation_id)
        spec = self._registry.get(call.tool)
        if spec is None:
            return self._refuse(BrokerReason.UNKNOWN_TOOL, correlation_id)
        name = spec.name
        if spec.returns_credential_plaintext:
            return self._refuse(
                BrokerReason.CREDENTIAL_PLAINTEXT_DENIED,
                correlation_id,
                tool=name,
                level=ApprovalLevel.DENY,
            )
        try:
            parsed = parse_arguments(spec, call.arguments, context.scope)
        except ArgumentError as error:
            return self._refuse(error.reason, correlation_id, tool=name)
        call_hash = compute_call_hash(name, parsed.values, context)

        try:
            classification = await classify_targets(
                parsed.targets,
                context.scope,
                self._resolver,
                timeout_seconds=self._timeout_seconds,
                urls=parsed.urls,
            )
        except PathResolutionError as error:
            logger.warning("Path resolution failed (%s)", error)
            return self._refuse(
                BrokerReason.PATH_RESOLUTION_UNAVAILABLE,
                correlation_id,
                tool=name,
                call_hash=call_hash,
            )
        level = most_restrictive(
            (
                self._policy.level_for(
                    spec.capabilities, spec.environment, classification.status
                ),
                spec.min_level,
            )
        )
        if level is ApprovalLevel.DENY:
            reason = (
                _OUT_OF_SCOPE_REASON[classification.offending]
                if classification.offending is not None
                else BrokerReason.POLICY_DENIED
            )
            return self._refuse(
                reason,
                correlation_id,
                tool=name,
                level=level,
                call_hash=call_hash,
            )

        denied = await self._authorize(
            spec, parsed, context, classification, correlation_id
        )
        if denied is not None:
            reason, authz_reason = denied
            return self._refuse(
                reason,
                correlation_id,
                tool=name,
                level=level,
                call_hash=call_hash,
                authz_reason=authz_reason,
            )
        if spec.requires_budget:
            budget_reason = await self._budget_denial(context, name)
            if budget_reason is not None:
                return self._refuse(
                    budget_reason,
                    correlation_id,
                    tool=name,
                    level=level,
                    call_hash=call_hash,
                )

        if level in (ApprovalLevel.AUTO, ApprovalLevel.SCOPED_AUTO):
            reason = (
                BrokerReason.AUTO
                if level is ApprovalLevel.AUTO
                else BrokerReason.SCOPED_AUTO
            )
            return self._allow(
                spec, parsed, context, level, call_hash, correlation_id, reason
            )
        task_reason = await self._task_denial(context)
        if task_reason is not None:
            return self._refuse(
                task_reason,
                correlation_id,
                tool=name,
                level=level,
                call_hash=call_hash,
                approval_id=approval_id,
            )
        if approval_id is None:
            return await self._open_approval(
                spec, parsed, context, level, call_hash, correlation_id
            )
        return await self._consume_approval(
            spec, parsed, context, level, call_hash, correlation_id, approval_id
        )

    def _allow(
        self,
        spec: ToolSpec,
        parsed: ParsedArguments,
        context: TaskContext,
        level: ApprovalLevel,
        call_hash: str,
        correlation_id: uuid.UUID,
        reason: BrokerReason,
        approval_id: uuid.UUID | None = None,
    ) -> BrokerDecision:
        return BrokerDecision(
            Verdict.ALLOW,
            reason,
            correlation_id,
            tool=spec.name,
            level=level,
            call_hash=call_hash,
            approval_id=approval_id,
            invocation=ToolInvocation(
                tool=spec.name,
                arguments=parsed.values,
                context=context,
                call_hash=call_hash,
                level=level,
                capabilities=spec.capabilities,
                correlation_id=correlation_id,
            ),
        )

    # --------------------------------------------------------- authorization

    def _resources(
        self,
        spec: ToolSpec,
        parsed: ParsedArguments,
        context: TaskContext,
        classification: Classification,
    ) -> list[Resource] | BrokerReason:
        """What the call is about, for the authorization decision (or the reason
        it cannot be decided)."""
        capability = spec.authz_capability
        match CAPABILITIES[capability].scope:
            case Scope.PROJECT:
                resources: list[Resource] = []
                if capability in REPO_PERMISSION_OF:
                    # A repository the call touches is decided on its own ACL
                    # (a project resource never carries one).
                    for repo_id in classification.repositories:
                        repository = context.scope.repository(repo_id)
                        assert repository is not None  # classified from this scope
                        state = context.scope.projects[repository.project_id]
                        resources.append(
                            Resource.repository(
                                repository.project_id, state, repository.acl
                            )
                            if repository.acl is not None
                            # The ACL is unknown: the policy denies it, and it is
                            # never read as "inherit".
                            else Resource(
                                kind="repository",
                                id=repository.repo_id,
                                project_id=repository.project_id,
                                repo_id=repository.repo_id,
                                project_state=state,
                            )
                        )
                    if (
                        not resources
                        and REPO_PERMISSION_OF[capability] is RepoPermission.WRITE
                    ):
                        return BrokerReason.REPOSITORY_NOT_IDENTIFIED
                projects: list[uuid.UUID] = []
                for target in parsed.targets:
                    if target.kind is TargetKind.PROJECT:
                        project_id = uuid.UUID(target.value)
                        if project_id not in projects:
                            projects.append(project_id)
                if not projects and not resources:
                    projects = [context.primary_project_id]
                resources.extend(
                    Resource.project(project_id, context.scope.projects[project_id])
                    for project_id in projects
                )
                return resources
            case Scope.SELF:
                return [Resource.owned_by(context.delegator_id, "tool_target")]
            case _:
                return [Resource.system()]

    async def _authorize(
        self,
        spec: ToolSpec,
        parsed: ParsedArguments,
        context: TaskContext,
        classification: Classification,
        correlation_id: uuid.UUID,
    ) -> tuple[BrokerReason, Reason | None] | None:
        """``None`` when allowed, else the reason (and the PAW-025 reason)."""
        resources = self._resources(spec, parsed, context, classification)
        if isinstance(resources, BrokerReason):
            return resources, None
        for resource in resources:
            try:
                decision = await self._authorizer.authorize_agent_action(
                    context.delegator_id,
                    context.grant,
                    spec.authz_capability,
                    resource,
                    correlation_id=correlation_id,
                )
            except Exception as error:
                logger.error("Authorization failed (%s)", type(error).__name__)
                return BrokerReason.AUTHZ_UNAVAILABLE, None
            if not isinstance(decision, Decision):
                return BrokerReason.AUTHZ_UNAVAILABLE, None
            if not decision.allowed:
                return BrokerReason.AUTHZ_DENIED, decision.reason
        return None

    async def _budget_denial(
        self, context: TaskContext, tool: str
    ) -> BrokerReason | None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                status = await self._budget.check(context.task_id, tool)
        except Exception as error:
            logger.error("Budget check failed (%s)", type(error).__name__)
            return BrokerReason.BUDGET_UNAVAILABLE
        if status is BudgetStatus.WITHIN_BUDGET:
            return None
        if status is BudgetStatus.EXCEEDED:
            return BrokerReason.BUDGET_EXCEEDED
        if status is BudgetStatus.UNKNOWN:
            return BrokerReason.BUDGET_UNKNOWN
        return BrokerReason.BUDGET_UNAVAILABLE  # not a status: an adapter bug

    async def _task_denial(self, context: TaskContext) -> BrokerReason | None:
        """Why the task cannot ask for or use an approval now (``None``: it can)."""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                activity = await self._task_activity.check(context.task_id)
        except Exception as error:
            logger.error("Task state check failed (%s)", type(error).__name__)
            return BrokerReason.TASK_STATE_UNAVAILABLE
        if activity is TaskActivity.ACTIVE:
            return None
        if activity is TaskActivity.ENDED:
            return BrokerReason.TASK_NOT_ACTIVE
        if activity is TaskActivity.UNKNOWN:
            return BrokerReason.TASK_UNKNOWN
        return BrokerReason.TASK_STATE_UNAVAILABLE  # not an answer: an adapter bug

    # -------------------------------------------------------------- approvals

    async def _open_approval(
        self,
        spec: ToolSpec,
        parsed: ParsedArguments,
        context: TaskContext,
        level: ApprovalLevel,
        call_hash: str,
        correlation_id: uuid.UUID,
    ) -> BrokerDecision:
        now = self._clock()
        if not parsed.summary:
            # A human cannot judge a call they are shown nothing of.
            return self._refuse(
                BrokerReason.APPROVAL_NOT_DISPLAYABLE,
                correlation_id,
                tool=spec.name,
                level=level,
                call_hash=call_hash,
            )
        try:
            new = NewApproval(
                approval_id=uuid.uuid4(),
                task_id=context.task_id,
                project_id=context.primary_project_id,
                agent_id=context.grant.agent_id,
                requester_user_id=context.delegator_id,
                tool=spec.name,
                level=level,
                call_hash=call_hash,
                targets=parsed.targets,
                summary=parsed.summary,
                expires_at=now + self._approval_ttl,
            )
        except ValueError:
            return self._refuse(
                BrokerReason.APPROVAL_NOT_DISPLAYABLE,
                correlation_id,
                tool=spec.name,
                level=level,
                call_hash=call_hash,
            )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                opened = await self._approvals.open_request(
                    new, now=now, limits=self._limits
                )
        except Exception as error:
            logger.error("Approval request failed (%s)", type(error).__name__)
            return self._refuse(
                BrokerReason.APPROVAL_UNAVAILABLE,
                correlation_id,
                tool=spec.name,
                level=level,
                call_hash=call_hash,
            )
        if isinstance(opened, OpenResult) and opened.outcome in _OPEN_REFUSAL_REASON:
            return self._refuse(
                _OPEN_REFUSAL_REASON[opened.outcome],
                correlation_id,
                tool=spec.name,
                level=level,
                call_hash=call_hash,
            )
        if (
            not isinstance(opened, OpenResult)
            or opened.record is None
            or opened.record.call_hash != call_hash
        ):
            logger.error("Approval store returned an unexpected record")
            return self._refuse(
                BrokerReason.APPROVAL_UNAVAILABLE,
                correlation_id,
                tool=spec.name,
                level=level,
                call_hash=call_hash,
            )
        if opened.created:
            await self._listeners.emit(
                ApprovalEvent(
                    kind=ApprovalEventKind.REQUESTED,
                    approval_id=opened.record.approval_id,
                    task_id=context.task_id,
                    tool=spec.name,
                    level=level,
                    occurred_at=now,
                )
            )
            reason = (
                BrokerReason.STRONG_APPROVAL_REQUIRED
                if level is ApprovalLevel.STRONG_APPROVAL
                else BrokerReason.APPROVAL_REQUIRED
            )
        else:
            reason = BrokerReason.APPROVAL_PENDING
        return BrokerDecision(
            Verdict.NEEDS_APPROVAL,
            reason,
            correlation_id,
            tool=spec.name,
            level=level,
            call_hash=call_hash,
            approval_id=opened.record.approval_id,
        )

    async def _consume_approval(
        self,
        spec: ToolSpec,
        parsed: ParsedArguments,
        context: TaskContext,
        level: ApprovalLevel,
        call_hash: str,
        correlation_id: uuid.UUID,
        approval_id: uuid.UUID,
    ) -> BrokerDecision:
        now = self._clock()
        binding = ApprovalBinding(
            task_id=context.task_id,
            agent_id=context.grant.agent_id,
            requester_user_id=context.delegator_id,
            tool=spec.name,
            level=level,
            call_hash=call_hash,
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                outcome = await self._approvals.consume(
                    approval_id, binding, now=now, require_active_task=True
                )
        except Exception as error:
            logger.error("Approval use failed (%s)", type(error).__name__)
            outcome = None
        if outcome is ConsumeOutcome.CONSUMED:
            await self._listeners.emit(
                ApprovalEvent(
                    kind=ApprovalEventKind.CONSUMED,
                    approval_id=approval_id,
                    task_id=context.task_id,
                    tool=spec.name,
                    level=level,
                    occurred_at=now,
                )
            )
            return self._allow(
                spec,
                parsed,
                context,
                level,
                call_hash,
                correlation_id,
                BrokerReason.APPROVAL_CONSUMED,
                approval_id,
            )
        if outcome is ConsumeOutcome.PENDING:
            return BrokerDecision(
                Verdict.NEEDS_APPROVAL,
                BrokerReason.APPROVAL_PENDING,
                correlation_id,
                tool=spec.name,
                level=level,
                call_hash=call_hash,
                approval_id=approval_id,
            )
        reason = (
            _CONSUME_REASON.get(outcome, BrokerReason.APPROVAL_UNAVAILABLE)
            if isinstance(outcome, ConsumeOutcome)
            else BrokerReason.APPROVAL_UNAVAILABLE
        )
        return self._refuse(
            reason,
            correlation_id,
            tool=spec.name,
            level=level,
            call_hash=call_hash,
            approval_id=approval_id,
        )

    # ------------------------------------------------------------------ audit

    async def _audited(
        self, decision: BrokerDecision, context: TaskContext
    ) -> BrokerDecision:
        recorded = await record_event(
            self._audit,
            build_tool_event(
                action=tool_action(decision.tool),
                allowed=decision.allowed,
                reason=decision.reason.value,
                correlation_id=decision.correlation_id,
                occurred_at=self._clock(),
                resource_kind="task",
                resource_id=context.task_id,
                project_id=context.primary_project_id,
                actor_id=context.delegator_id,
                agent_id=context.grant.agent_id,
            ),
            self._timeout_seconds,
        )
        if recorded or not decision.allowed:
            return decision
        # An allowed call whose decision cannot be recorded does not run.
        return replace(
            decision,
            verdict=Verdict.DENY,
            reason=BrokerReason.AUDIT_UNAVAILABLE,
            invocation=None,
        )
