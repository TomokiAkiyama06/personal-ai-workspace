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
   of the working set is denied (``repository_not_identified``).
   **Before** that decision, the Working Set's role ceiling (issue #85,
   Decision 0030, section 4): a call that touches a repository whose role is
   not resolved is denied (``repository_role_unresolved``); a call that writes
   or deletes (``write`` / ``destructive``) with a capability that is not a
   repository write is denied (``repository_write_capability_mismatch``), and
   so is one whose capability (``project.repo.write`` / ``project.pr.create``)
   the role does not allow, or that executes something (``execute``) in a
   ``referenced`` repository (``repository_role_insufficient``, #85 constraint
   2). An approval cannot lift any of these. A Working Set tool
   (``ToolSpec.working_set_operation``) is decided instead on the task's
   project (``project.task.working_set.manage``) AND on the repository it
   names, resolved from its registration, with the permission of the change
   (``tasks.working_set.required_permission``);
5. the task budget (:class:`~.budget.BudgetProvider`); unknown means denied;
6. a call that touches a repository is admitted on the roles the Working Set
   holds **now** (:class:`~.working_set.RepositoryUseGate`, i.e.
   ``TaskService.admit_repository_use``): the roles of step 4 are those of the
   caller's task scope, which can be older than a downgrade or a removal, and
   the stored table is the one truth (Decision 0030, 4.1 / 4.6). The same
   ceiling is applied again (``repository_role_unresolved`` /
   ``repository_role_insufficient``), and a write or something executed is
   recorded as a change of the repository in the attempt (section 5; a command
   can write, and the backend cannot tell). Such a use is also **reserved**
   (``BrokerDecision.reservation_id``) until :meth:`ToolBroker.record_execution`
   releases it after the executor returned or failed (or it expires): until
   then the repositories are not downgraded or removed, so the admission holds
   through the execution (Codex review of #85). A decision that ends up not
   allowed releases it at once. A use that cannot be admitted, or a write
   admitted without a reservation, is denied (``repository_write_unrecorded``,
   or ``repository_role_unresolved`` for a read). For a call that needs an
   approval this happens before the approval is consumed, so a refused use
   leaves it unused;
7. ``AUTO`` / ``SCOPED_AUTO`` are allowed; ``APPROVAL`` / ``STRONG_APPROVAL``
   need an approval bound to this exact call. **The task must still be able to
   act, in the worker's run** (:class:`~.task_state.TaskActivityProvider`: not
   completed / failed / cancelled, not unknown or unreadable, and the run in
   ``TaskContext.run`` is still the task's current one: a Retry / Restart
   starts a new run) before an approval is opened or used: an approval whose
   revocation failed when its task ended cannot be used, whatever the store
   still says. Then, without an approval a request is opened (``NEEDS_APPROVAL``,
   stamped with the run); with one it is consumed atomically (single use, and
   only by the run it was requested in) or the call is denied with the reason
   (expired, replayed, for another call, for an earlier run...). The check
   before the request or the use gives the early, precise reason; it can be
   overtaken by the end of the task or by a Retry / Restart, so the store checks
   the task **again in the same transaction that inserts the request or
   consumes the approval** (``require_active_task``: the task row is read
   locked), and a task that ended, or a run that was replaced, in between opens
   or consumes nothing (``task_not_active`` / ``task_superseded``).

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
    Capability,
    Decision,
    Reason,
    RepoAcl,
    RepoPermission,
    Resource,
    Scope,
)
from paw_backend.authz.capabilities import REPO_PERMISSION_OF
from paw_backend.tasks.domain import RepoRole, accepts_role
from paw_backend.tasks.errors import (
    RepositoryRoleInsufficientError,
    RepositoryRoleUnresolvedError,
    StaleRunError,
    TaskNotFoundError,
)
from paw_backend.tasks.working_set import (
    marks_changed,
    required_permission,
    role_allows,
)
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
from paw_backend.tools.capabilities import (
    ApprovalLevel,
    ToolCapability,
    most_restrictive,
)
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
from paw_backend.tools.working_set import (
    FailClosedRegistrations,
    FailClosedUseGate,
    RepositoryUseGate,
    WorkingSetRegistrations,
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
    # Requested in an earlier run of the task than the caller's.
    ConsumeOutcome.SUPERSEDED: BrokerReason.APPROVAL_SUPERSEDED,
    # The task ended (or was started again) between the broker's check and the
    # use (the store checks it again in the step that consumes).
    ConsumeOutcome.TASK_NOT_ACTIVE: BrokerReason.TASK_NOT_ACTIVE,
    ConsumeOutcome.TASK_UNKNOWN: BrokerReason.TASK_UNKNOWN,
    ConsumeOutcome.TASK_SUPERSEDED: BrokerReason.TASK_SUPERSEDED,
}
# The capability whose repository permission is this one: the proxy on which a
# Working Set change is decided on the repository it names (Decision 0030, 3.4).
_PROXY_CAPABILITY = {
    RepoPermission.READ: Capability.PROJECT_READ,
    RepoPermission.WRITE: Capability.PROJECT_REPO_WRITE,
}
_CHANGES = frozenset({ToolCapability.WRITE, ToolCapability.DESTRUCTIVE})
_OPEN_REFUSAL_REASON = {
    OpenOutcome.TOO_MANY_PENDING: BrokerReason.APPROVAL_LIMIT_REACHED,
    OpenOutcome.COOLING_DOWN: BrokerReason.APPROVAL_COOLDOWN,
    # The task ended (or was started again) between the broker's check and the
    # insert (the store checks it again in the transaction that inserts).
    OpenOutcome.TASK_NOT_ACTIVE: BrokerReason.TASK_NOT_ACTIVE,
    OpenOutcome.TASK_UNKNOWN: BrokerReason.TASK_UNKNOWN,
    OpenOutcome.TASK_SUPERSEDED: BrokerReason.TASK_SUPERSEDED,
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
        registrations: WorkingSetRegistrations | None = None,
        use_gate: RepositoryUseGate | None = None,
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
        require_async_method(self._task_activity, "check", 2)
        self._resolver: PathResolver = path_resolver or RealpathResolver()
        require_async_method(self._resolver, "resolve", 1)
        # Both fail closed when not wired: no Working Set change is decided, and
        # no call that touches a repository is allowed.
        self._registrations: WorkingSetRegistrations = (
            registrations or FailClosedRegistrations()
        )
        require_async_method(self._registrations, "working_set_acl", 1)
        self._use_gate: RepositoryUseGate = use_gate or FailClosedUseGate()
        require_async_method(self._use_gate, "admit_repository_use", 3)
        require_async_method(self._use_gate, "release_repository_use", 2)
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
        decision = await self._audited(decision, call.context)
        if not decision.allowed and decision.reservation_id is not None:
            # Admitted, but it does not run after all (its decision could not be
            # recorded): nothing is in flight on the repositories.
            await self._release(call.context, decision.reservation_id)
            decision = replace(decision, reservation_id=None)
        return decision

    async def record_execution(
        self, decision: BrokerDecision, *, succeeded: bool
    ) -> None:
        """Record that an allowed call ran: the audit row and the budget charge.

        Called by the runner after the executor returned or failed. It never
        raises for a store failure (the call already ran). It first releases the
        call's repository reservation (the write is no longer in flight; one that
        cannot be released expires, fail-closed).
        """
        invocation = decision.invocation
        if not decision.allowed or invocation is None:
            raise ValueError("only an allowed call can have been executed")
        context = invocation.context
        if decision.reservation_id is not None:
            await self._release(context, decision.reservation_id)
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

        if spec.working_set_operation is not None:
            denied = await self._authorize_working_set(
                spec, parsed, context, correlation_id
            )
        else:
            role_reason = self._role_denial(spec, context, classification)
            if role_reason is not None:
                return self._refuse(
                    role_reason,
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
            use_reason, reservation = await self._admit_use(
                spec, context, classification
            )
            if use_reason is not None:
                return self._refuse(
                    use_reason,
                    correlation_id,
                    tool=name,
                    level=level,
                    call_hash=call_hash,
                )
            reason = (
                BrokerReason.AUTO
                if level is ApprovalLevel.AUTO
                else BrokerReason.SCOPED_AUTO
            )
            return self._allow(
                spec,
                parsed,
                context,
                level,
                call_hash,
                correlation_id,
                reason,
                reservation_id=reservation,
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
        # Admitted BEFORE the approval is consumed: a use the stored Working Set
        # refuses (or that cannot be admitted) leaves the human's one-shot
        # approval unused. The other way round, a use admitted for an approval
        # that is then not consumed marks the repository changed although the
        # call did not run: that only adds an obligation (fail-closed), and its
        # reservation is released at once.
        use_reason, reservation = await self._admit_use(spec, context, classification)
        if use_reason is not None:
            return self._refuse(
                use_reason,
                correlation_id,
                tool=name,
                level=level,
                call_hash=call_hash,
                approval_id=approval_id,
            )
        decision = await self._consume_approval(
            spec,
            parsed,
            context,
            level,
            call_hash,
            correlation_id,
            approval_id,
            reservation_id=reservation,
        )
        if not decision.allowed and reservation is not None:
            await self._release(context, reservation)
        return decision

    async def _admit_use(
        self,
        spec: ToolSpec,
        context: TaskContext,
        classification: Classification,
    ) -> tuple[BrokerReason | None, uuid.UUID | None]:
        """Admit the call's use of the repositories it touches on the roles
        stored NOW (``RepositoryUseGate``; Decision 0030, 4.1 / 4.6): the task
        scope's roles, which :meth:`_role_denial` checked, may be older than a
        downgrade or a removal. A write or an execution is recorded as a change
        of the repository (section 5) and reserved until the call ended (Codex
        review of #85: the repositories are not downgraded or removed while its
        executor may still write). Returns the refusal (``None`` when admitted)
        and the reservation to release after the execution (``None``: none)."""
        if not classification.repositories:
            return None, None
        capability = spec.authz_capability
        executes = ToolCapability.EXECUTE in spec.capabilities
        changes = marks_changed(capability, executes=executes)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                reservation = await self._use_gate.admit_repository_use(
                    context.task_id,
                    context.run,
                    classification.repositories,
                    capability=capability,
                    executes=executes,
                )
        except RepositoryRoleUnresolvedError:
            return BrokerReason.REPOSITORY_ROLE_UNRESOLVED, None
        except RepositoryRoleInsufficientError:
            return BrokerReason.REPOSITORY_ROLE_INSUFFICIENT, None
        except StaleRunError:
            # A Retry / Restart started another run: this one acts no more.
            return BrokerReason.TASK_SUPERSEDED, None
        except TaskNotFoundError:
            return BrokerReason.TASK_UNKNOWN, None
        except Exception as error:
            logger.error("Repository use not admitted (%s)", type(error).__name__)
            # A change that cannot be recorded does not run; a read whose stored
            # role cannot be read is a role that is not resolved.
            return (
                BrokerReason.REPOSITORY_WRITE_UNRECORDED
                if changes
                else BrokerReason.REPOSITORY_ROLE_UNRESOLVED
            ), None
        if not isinstance(reservation, uuid.UUID):
            if changes:
                # A write that holds no reservation could outlive a downgrade.
                logger.error("Repository write admitted without a reservation")
                return BrokerReason.REPOSITORY_WRITE_UNRECORDED, None
            reservation = None
        return None, reservation

    async def _release(self, context: TaskContext, reservation_id: uuid.UUID) -> None:
        """Release a repository reservation; never raises (one that cannot be
        released expires: until then the repositories are not narrowed)."""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._use_gate.release_repository_use(
                    context.task_id, reservation_id
                )
        except Exception as error:
            logger.error(
                "Repository reservation not released (%s)", type(error).__name__
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
        *,
        reservation_id: uuid.UUID | None = None,
    ) -> BrokerDecision:
        return BrokerDecision(
            Verdict.ALLOW,
            reason,
            correlation_id,
            tool=spec.name,
            level=level,
            call_hash=call_hash,
            approval_id=approval_id,
            reservation_id=reservation_id,
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
        return await self._decide_all(
            context,
            [(spec.authz_capability, resource) for resource in resources],
            correlation_id,
        )

    async def _decide_all(
        self,
        context: TaskContext,
        requests: list[tuple[Capability, Resource]],
        correlation_id: uuid.UUID,
    ) -> tuple[BrokerReason, Reason | None] | None:
        """Every (capability, resource) must be allowed; the first denial wins."""
        for capability, resource in requests:
            try:
                decision = await self._authorizer.authorize_agent_action(
                    context.delegator_id,
                    context.grant,
                    capability,
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

    @staticmethod
    def _role_denial(
        spec: ToolSpec, context: TaskContext, classification: Classification
    ) -> BrokerReason | None:
        """The Working Set's role ceiling on the repositories a call touches
        (Decision 0030, section 4; #85 constraint 2), ``None`` when it allows.

        It only narrows: the repository's ACL is decided after it, and an
        approval never lifts it.
        """
        if not classification.repositories:
            return None
        capability = spec.authz_capability
        if (
            spec.capabilities & _CHANGES
            and REPO_PERMISSION_OF.get(capability) is not RepoPermission.WRITE
        ):
            return BrokerReason.REPOSITORY_WRITE_CAPABILITY_MISMATCH
        executes = ToolCapability.EXECUTE in spec.capabilities
        for repo_id in classification.repositories:
            repository = context.scope.repository(repo_id)
            role = repository.role if repository is not None else None
            if role is None:
                return BrokerReason.REPOSITORY_ROLE_UNRESOLVED
            if not role_allows(role, capability, executes=executes):
                return BrokerReason.REPOSITORY_ROLE_INSUFFICIENT
        return None

    async def _authorize_working_set(
        self,
        spec: ToolSpec,
        parsed: ParsedArguments,
        context: TaskContext,
        correlation_id: uuid.UUID,
    ) -> tuple[BrokerReason, Reason | None] | None:
        """A change of the Working Set (Decision 0030, 3.4): ``None`` when the
        task's project (``project.task.working_set.manage``) AND the repository
        it names (the permission of the change, on its registered ACL) allow it.

        The role the change is decided on is the one in the task scope, which the
        backend resolved from the stored Working Set; ``TaskService`` refuses the
        change if the stored role moved meanwhile.
        """
        operation = spec.working_set_operation
        assert operation is not None
        (target,) = [
            t for t in parsed.targets if t.kind is TargetKind.WORKING_SET_REPOSITORY
        ]
        repo_id = uuid.UUID(target.value)
        entry = context.scope.repository(repo_id)
        current: RepoRole | None = entry.role if entry is not None else None
        if entry is not None and current is None and not accepts_role(operation, None):
            # A downgrade or a removal cannot be decided on an unknown role (adding
            # one is decided as "not in the Working Set").
            return BrokerReason.REPOSITORY_ROLE_UNRESOLVED, None
        if not accepts_role(operation, current):
            return BrokerReason.WORKING_SET_CHANGE_INVALID, None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                acl = await self._registrations.working_set_acl(repo_id)
        except Exception as error:
            logger.error("Registration lookup failed (%s)", type(error).__name__)
            acl = None
        if not isinstance(acl, RepoAcl) or acl.repo_id != repo_id:
            return BrokerReason.WORKING_SET_REPOSITORY_UNRESOLVED, None
        state = context.scope.projects.get(acl.project_id)
        if state is None or (entry is not None and entry.project_id != acl.project_id):
            return BrokerReason.REPOSITORY_OUT_OF_SCOPE, None
        proxy = _PROXY_CAPABILITY[required_permission(operation, current)]
        primary = context.primary_project_id
        return await self._decide_all(
            context,
            [
                (
                    spec.authz_capability,
                    Resource.project(primary, context.scope.projects[primary]),
                ),
                (proxy, Resource.repository(acl.project_id, state, acl)),
            ],
            correlation_id,
        )

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
                activity = await self._task_activity.check(context.task_id, context.run)
        except Exception as error:
            logger.error("Task state check failed (%s)", type(error).__name__)
            return BrokerReason.TASK_STATE_UNAVAILABLE
        if activity is TaskActivity.ACTIVE:
            return None
        if activity is TaskActivity.ENDED:
            return BrokerReason.TASK_NOT_ACTIVE
        if activity is TaskActivity.SUPERSEDED:
            return BrokerReason.TASK_SUPERSEDED
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
                task_run=context.run,
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
                    new, now=now, limits=self._limits, require_active_task=True
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
        *,
        reservation_id: uuid.UUID | None = None,
    ) -> BrokerDecision:
        now = self._clock()
        binding = ApprovalBinding(
            task_id=context.task_id,
            task_run=context.run,
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
                reservation_id=reservation_id,
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
