"""The shared connection service: Codex / Claude connections, quotas, attributed use.

What it is (and is not)
-----------------------
A backend library on PostgreSQL. It has no HTTP endpoint (sessions come with PAW-022)
and makes no network call and starts no process: the provider is reached only through
a registered :class:`~paw_backend.connections.adapter.ConnectionAdapter` (none ships
here) with a credential that only the secret store's resolver can produce
(:mod:`~paw_backend.connections.secret`). The Orchestrator (PAW-034) calls
:meth:`ConnectionService.execute`; an agent never does. Decision 0016 (Approved,
2026-09-26) holds the product choices this code follows (quota semantics, periods,
running tasks).

Authorization (the existing capabilities of Decision 0004; none is added)
-------------------------------------------------------------------------
* ``connect``, ``replace_credential``, ``enable``, ``disable``, ``disconnect``,
  ``get_connection``, ``list_connections``: ``admin.config.manage`` (Owner / Admin);
* ``set_quota``, ``remove_quota``: ``admin.quota.manage`` (Owner / Admin), and the
  quota of an Owner only by the Owner (Decision 0016, section 7);
* ``quota_status``, ``list_usage`` of ANOTHER user: ``admin.usage.view``;
* ``quota_status``, ``list_usage`` of one's OWN, ``availability`` and ``execute``:
  ``agent.use``, a ``Scope.SELF`` capability: the resource is owned by the user the
  call is for, so an Owner does not read or spend another user's;
* ``check_health``: backend-internal (a scheduler), no actor, like
  ``ProjectService.purge_expired``.

Every method takes the acting :class:`~paw_backend.authz.Principal` first (only
``user_id`` and ``system_role`` are taken from it; the authentication layer resolved
them from stored users) and asks the :class:`~paw_backend.authz.Authorizer` (default
deny; the Authorizer audits the decision, ``REQUIRED``: no audit, no action).

Order of checks (every method)
------------------------------
1. the actor: not a ``Principal`` -> ``ConnectionPermissionDeniedError``
   (``UNAUTHENTICATED``);
2. the arguments, in signature order, all before the database is touched
   (``InvalidConnectionInputError``; the store is never asked);
3. the Authorizer (``ConnectionPermissionDeniedError``, ``reason`` attached; an audit
   that could not be written is ``Reason.AUDIT_UNAVAILABLE``, for the API layer's 503);
4. the rules of the method.

The audit
---------
Besides the Authorizer's decision rows (which name the capability), the service writes
its own events with ids and enums only (never a prompt, an answer, a model name, a
credential or a handle): the OUTCOME of every change of a connection or a quota
(``connection.connect`` / ``.replace`` / ``.enable`` / ``.disable`` / ``.disconnect`` /
``.quota.set`` / ``.quota.remove``, allowed, reason ``succeeded``), a change of a
connection's status (``connection.status``, reason = the new status; no actor when a
health check found it), and every REFUSAL to start a call (``connection.use``, denied,
reason = a ``RefusalReason``: ``quota_exceeded``, ``connection_unavailable``,
``task_*``, ``task_budget_*``). An allowed call is recorded as its usage row
(user, task, project, kind, model, purpose, tokens, duration) and by the Authorizer's
``agent.use`` row; not a third time. These events are written AFTER the change or the
refusal and are best effort (a failure is logged by exception type and does not undo
the change; the refusal stands): the decision rows are the fail-closed ones.
Each refusal writes one row per attempt, so a caller that retries in a loop after a
quota refusal fills the audit table: queue the task until ``resets_at`` instead.

``execute``
-----------
1. the arguments; 2. ``agent.use`` on the user's own resource (the user is
``context.delegator_id``: a principal that is somebody else is denied); 3. an adapter
must be registered for the kind; 4. the task's own token budget (PAW-033
``BudgetTracker``, if the service was given one): exhausted or not configured refuses
the call; 5. the ADMISSION (``ConnectionStore.admit``): one transaction that checks
the task, the connection and the quotas and inserts the ``in_flight`` usage row; 6. the
call: the handle is resolved into a ``Secret`` and the adapter runs, under ONE deadline
(``request.timeout_seconds``); 7. the SETTLEMENT, always, and before any cancellation
of the caller gets through (it runs as a task the caller keeps and waits for; the
cancel is raised afterwards): the outcome, the tokens and the database-clock duration
are written to the usage row and the tokens are charged to the task's budget; 8. the
answer, scrubbed of the credential (scrub, redact the credential formats, scrub again,
then check: returned whole or refused; never shortened, never longer than the limit,
never with the credential still readable in any normalised form; each valid token
count the adapter reported is kept even when the answer is refused).

A quota that is reached never interrupts a call that has started: the check is at the
admission only, and a running TASK is not refused at its next call either (Decision
0016, section 3); only a call that starts a new task is (``QuotaExceededError``,
audited, with ``resets_at`` for a calendar window). A quota that is not set is not
enforced: a user, metric or period without a row is unlimited (Decision 0016,
section 2); the call is still attributed and audited.

Failures of the call (``ConnectionCallError`` with a closed ``FailureCode``) are
recorded and counted like any call. Cancelling the caller records the call as
``cancelled`` (and the cancellation propagates). Whatever the adapter or the resolver
raised, and whatever it said, is dropped: the log carries the exception TYPE from a
fixed allowlist (``log_type_name``), nothing else.

Limits (stated, not hidden)
---------------------------
* Tokens and runtime are known only when a call ends, so they are counted when it has
  settled: concurrent NEW tasks can pass the check together and exceed a token or
  runtime quota by what they use (requests and tasks are exact under concurrency).
* A usage row that was never settled (the process died between admission and
  settlement) stays ``in_flight``: it counts as a request but has no tokens or
  duration. There is no reaper yet.
* The budget charge is a separate transaction from the settlement.
* A prompt is not inspected here (credential detection and privacy filtering of what
  is sent to a cloud provider belong to the Orchestrator and the Tool Broker).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from paw_backend.authz import (
    AuditEvent,
    AuditSink,
    Authorizer,
    Capability,
    Principal,
    Resource,
)
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import SystemRole
from paw_backend.connections.adapter import (
    AdapterFailure,
    AdapterRegistry,
    AdapterRequest,
    AdapterResult,
)
from paw_backend.connections.domain import (
    DEFAULT_PERIOD_TIMEZONE,
    ConnectionKind,
    ConnectionStatus,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    RefusalReason,
    Unlimited,
    UsageStatus,
)
from paw_backend.connections.errors import (
    ConnectionBusyError,
    ConnectionCallError,
    ConnectionExistsError,
    ConnectionNotFoundError,
    ConnectionPermissionDeniedError,
    ConnectionUnavailableError,
    InputProblem,
    InvalidConnectionInputError,
    QuotaExceededError,
    TargetUserNotFoundError,
    TaskBudgetError,
    TaskNotUsableError,
)
from paw_backend.connections.limits import (
    DEFAULT_DATABASE_TIMEOUT_SECONDS,
    DEFAULT_HEALTH_TIMEOUT_SECONDS,
    DEFAULT_LIST_LIMIT,
    MAX_HEALTH_TIMEOUT_SECONDS,
    MAX_RESULT_CHARS,
    MAX_TOKENS_PER_CALL,
)
from paw_backend.connections.records import (
    ConnectionAvailability,
    ConnectionInfo,
    ConnectionRequest,
    ConnectionResult,
    Quota,
    QuotaUsage,
    UsageRecord,
)
from paw_backend.connections.secret import Secret, SecretResolver
from paw_backend.connections.store import Admitted, ConnectionStore, Refused
from paw_backend.connections.validation import (
    fail,
    require_type,
    validate_enum,
    validate_handle,
    validate_optional_tokens,
    validate_page,
    validate_quota_limit,
    validate_result_text,
    validate_seconds,
    validate_uuid,
    zone_of,
)
from paw_backend.db import Database
from paw_backend.research.providers.broker import log_type_name
from paw_backend.tasks.queueing.budget import BudgetTracker
from paw_backend.tasks.queueing.domain import BudgetKind
from paw_backend.tasks.queueing.errors import BudgetNotConfiguredError
from paw_backend.tools.calls import TaskContext
from paw_backend.tools.credentials import MAX_TEXT_CHARS, redact_text
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

# Audit actions written by this module (the Authorizer's rows carry capability names).
ACTION_CONNECT = "connection.connect"
ACTION_REPLACE = "connection.replace"
ACTION_ENABLE = "connection.enable"
ACTION_DISABLE = "connection.disable"
ACTION_DISCONNECT = "connection.disconnect"
ACTION_STATUS = "connection.status"
ACTION_USE = "connection.use"
ACTION_QUOTA_SET = "connection.quota.set"
ACTION_QUOTA_REMOVE = "connection.quota.remove"
OUTCOME_SUCCEEDED = "succeeded"
# The most an answer may be, as the adapter returned it and as it is returned: the
# limit of the adapter result, and never more than ``redact_text`` reads (it cuts an
# input over its own limit). ``tests/test_connections_result_limit.py`` keeps the
# two limits equal.
ANSWER_LIMIT = min(MAX_RESULT_CHARS, MAX_TEXT_CHARS)
OWNER_ONLY = "owner_quota_owner_only"  # reason of the refusal (audit)

_RESOURCE_CONNECTION = "connection"
_RESOURCE_QUOTA = "connection_quota"
_RESOURCE_USAGE = "connection_usage"
_SYSTEM_ROLE = SystemRole.SYSTEM.value

_TASK_REFUSALS = {
    RefusalReason.TASK_NOT_FOUND,
    RefusalReason.TASK_ENDED,
    RefusalReason.TASK_SUPERSEDED,
}


@dataclass(slots=True)
class _Outcome:
    """What one call ended with. Set by ``_call``; read by ``execute``'s ``finally``."""

    status: UsageStatus = UsageStatus.FAILED
    failure: FailureCode | None = FailureCode.INTERNAL_ERROR
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    redactions: int = 0

    def failed(self, code: FailureCode) -> None:
        self.status = UsageStatus.FAILED
        self.failure = code

    def cancelled(self) -> None:
        self.status = UsageStatus.CANCELLED
        self.failure = None


def _token_count(value: object) -> tuple[int | None, bool]:
    """``(the count to keep, whether it was valid)``: an unknown count (``None``) is
    valid and kept as unknown; an invalid one (negative, over the limit, a bool, a
    float, a string) is not kept."""
    try:
        return validate_optional_tokens("tokens", value, MAX_TOKENS_PER_CALL), True
    except InvalidConnectionInputError:
        return None, False


def _failure_code_of(error: BaseException) -> FailureCode:
    """The closed code of an exception an adapter raised. By ``type()`` and the
    slot of ``AdapterFailure`` only: an adapter's property or ``__class__`` cannot
    make this raise or lie."""
    error_type = type(error)
    if issubclass(error_type, AdapterFailure):
        code = AdapterFailure.code.__get__(error, AdapterFailure)  # type: ignore[attr-defined]
        return code if type(code) is FailureCode else FailureCode.INTERNAL_ERROR
    if issubclass(error_type, TimeoutError):
        return FailureCode.TIMEOUT
    return FailureCode.INTERNAL_ERROR


class ConnectionService:
    """The shared Codex / Claude connections. See the module docstring."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        audit_sink: AuditSink,
        adapters: AdapterRegistry,
        secrets: SecretResolver,
        *,
        budget: BudgetTracker | None = None,
        period_timezone: str = DEFAULT_PERIOD_TIMEZONE,
        database_timeout_seconds: float = DEFAULT_DATABASE_TIMEOUT_SECONDS,
        health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        clock=None,
        allow_explicit_clock: bool = False,
    ) -> None:
        """Wire the service.

        A wrong collaborator fails HERE (``InvalidConnectionInputError`` for a wrong
        type, a bad time zone or number), not on the first call.

        ``period_timezone`` is the IANA zone of the calendar windows (``day``, ``week``,
        ``month``); the default is ``Asia/Tokyo`` (Decision 0016, section 4, approved;
        it needs the system's time zone database). ``clock`` /
        ``allow_explicit_clock`` are the TEST SEAM of ``ConnectionStore``: production
        code passes neither, and every instant is the database's.
        """
        require_type("authorizer", authorizer, Authorizer)
        require_type("adapters", adapters, AdapterRegistry)
        try:
            require_async_method(audit_sink, "record", 1)
            require_async_method(secrets, "resolve", 1)
        except TypeError:
            raise fail("collaborator", InputProblem.WRONG_TYPE) from None
        if budget is not None:
            require_type("budget", budget, BudgetTracker)
        validate_seconds(
            "health_timeout_seconds", health_timeout_seconds, MAX_HEALTH_TIMEOUT_SECONDS
        )
        self._store = ConnectionStore(
            database,
            timeout_seconds=database_timeout_seconds,
            zone=zone_of(period_timezone),
            clock=clock,
            allow_explicit_clock=allow_explicit_clock,
        )
        self._authorizer = authorizer
        self._sink = audit_sink
        self._adapters = adapters
        self._secrets = secrets
        self._budget = budget
        self._health_timeout = float(health_timeout_seconds)
        self._audit_timeout = 3.0

    # --- connections (Owner / Admin) -----------------------------------------

    async def connect(
        self, principal: Principal, kind: ConnectionKind, secret_handle: str
    ) -> ConnectionInfo:
        """Create the workspace's connection of ``kind`` from a credential HANDLE.

        The plaintext is put into the secret store by other means and never passes
        through here; a value that is not a handle is refused
        (``InvalidConnectionInputError``, ``not_a_handle``) without being looked at.
        The connection starts ``unavailable`` (not verified): ``check_health`` says
        when it is ``connected``. ``ConnectionExistsError`` if one exists.
        """
        self._actor(principal)
        kind = validate_enum("kind", kind, ConnectionKind)
        handle = validate_handle("secret_handle", secret_handle)
        correlation_id = uuid.uuid4()
        await self._authorize(
            principal,
            Capability.ADMIN_CONFIG_MANAGE,
            _connection_resource(),
            correlation_id,
        )
        info = await self._call_store(self._store.insert_connection(kind, handle))
        if info is None:
            raise ConnectionExistsError()
        await self._audit(
            ACTION_CONNECT, OUTCOME_SUCCEEDED, True, principal, correlation_id,
            _RESOURCE_CONNECTION, info.id,
        )  # fmt: skip
        return info

    async def replace_credential(
        self, principal: Principal, kind: ConnectionKind, secret_handle: str
    ) -> ConnectionInfo:
        """Point the connection at another credential handle (a Security-sensitive
        change). The connection goes back to ``unavailable`` until a health check
        verifies the new credential. ``ConnectionNotFoundError`` if none exists."""
        self._actor(principal)
        kind = validate_enum("kind", kind, ConnectionKind)
        handle = validate_handle("secret_handle", secret_handle)
        correlation_id = uuid.uuid4()
        await self._authorize(
            principal,
            Capability.ADMIN_CONFIG_MANAGE,
            _connection_resource(),
            correlation_id,
        )
        info = await self._call_store(self._store.replace_handle(kind, handle))
        if info is None:
            raise ConnectionNotFoundError()
        await self._audit(
            ACTION_REPLACE, OUTCOME_SUCCEEDED, True, principal, correlation_id,
            _RESOURCE_CONNECTION, info.id,
        )  # fmt: skip
        return info

    async def disable(
        self, principal: Principal, kind: ConnectionKind
    ) -> ConnectionInfo:
        """Stop new calls on the connection (users see ``Unavailable``). Calls that
        have started finish. A health check never re-enables it."""
        return await self._switch(principal, kind, False, ACTION_DISABLE)

    async def enable(
        self, principal: Principal, kind: ConnectionKind
    ) -> ConnectionInfo:
        """Allow calls again (only if the last health check said ``connected``)."""
        return await self._switch(principal, kind, True, ACTION_ENABLE)

    async def _switch(
        self, principal: Principal, kind: ConnectionKind, enabled: bool, action: str
    ) -> ConnectionInfo:
        self._actor(principal)
        kind = validate_enum("kind", kind, ConnectionKind)
        correlation_id = uuid.uuid4()
        await self._authorize(
            principal,
            Capability.ADMIN_CONFIG_MANAGE,
            _connection_resource(),
            correlation_id,
        )
        info = await self._call_store(self._store.set_enabled(kind, enabled))
        if info is None:
            raise ConnectionNotFoundError()
        await self._audit(
            action, OUTCOME_SUCCEEDED, True, principal, correlation_id,
            _RESOURCE_CONNECTION, info.id,
        )  # fmt: skip
        return info

    async def disconnect(self, principal: Principal, kind: ConnectionKind) -> None:
        """Remove the connection (its usage history stays). The credential itself is
        the secret store's: revoking it there is a separate step."""
        self._actor(principal)
        kind = validate_enum("kind", kind, ConnectionKind)
        correlation_id = uuid.uuid4()
        existing = await self._authorize_existing(principal, kind, correlation_id)
        if not await self._call_store(self._store.delete_connection(kind)):
            raise ConnectionNotFoundError()
        await self._audit(
            ACTION_DISCONNECT, OUTCOME_SUCCEEDED, True, principal, correlation_id,
            _RESOURCE_CONNECTION, existing,
        )  # fmt: skip

    async def get_connection(
        self, principal: Principal, kind: ConnectionKind
    ) -> ConnectionInfo:
        """The connection as an Owner / Admin sees it: status, switch, last check;
        never the credential or its handle. ``ConnectionNotFoundError`` if none."""
        self._actor(principal)
        kind = validate_enum("kind", kind, ConnectionKind)
        await self._authorize(
            principal,
            Capability.ADMIN_CONFIG_MANAGE,
            _connection_resource(),
            uuid.uuid4(),
        )
        info = await self._call_store(self._store.get_connection(kind))
        if info is None:
            raise ConnectionNotFoundError()
        return info

    async def list_connections(
        self, principal: Principal
    ) -> tuple[ConnectionInfo, ...]:
        """The configured connections in ``ConnectionKind`` order (Owner / Admin)."""
        self._actor(principal)
        await self._authorize(
            principal,
            Capability.ADMIN_CONFIG_MANAGE,
            _connection_resource(),
            uuid.uuid4(),
        )
        return await self._call_store(self._store.list_connections())

    async def availability(
        self, principal: Principal
    ) -> tuple[ConnectionAvailability, ...]:
        """What a general user sees for each kind: available or not, nothing more.

        ``available`` means a call could start now: the connection exists, is
        enabled, was verified ``connected`` and an adapter is registered for it.
        Whether the user's own quota allows a NEW task is ``quota_status``.
        """
        self._actor(principal)
        await self._authorize(
            principal,
            Capability.AGENT_USE,
            Resource.owned_by(principal.user_id, _RESOURCE_CONNECTION),
            uuid.uuid4(),
        )
        configured = {
            info.kind: info
            for info in await self._call_store(self._store.list_connections())
        }
        return tuple(
            ConnectionAvailability(
                kind,
                kind in configured
                and configured[kind].available
                and kind in self._adapters,
            )
            for kind in ConnectionKind
        )

    async def check_health(self, kind: ConnectionKind) -> ConnectionStatus:
        """Ask the adapter whether the credential works and store the verdict.

        Backend-internal (a scheduler, the Owner's "reconnect"), no actor. The
        verdict is stored only while the connection still points at the credential
        that was checked. A missing adapter, a resolver or adapter that raises, or
        a check that runs past ``health_timeout_seconds`` is ``unavailable``;
        ``AdapterFailure(EXPIRED)`` is ``expired``. A change of status is audited
        (``connection.status``). ``ConnectionNotFoundError`` if none is configured.
        """
        kind = validate_enum("kind", kind, ConnectionKind)
        handle = await self._call_store(self._store.read_handle(kind))
        if handle is None:
            raise ConnectionNotFoundError()
        status = await self._probe(kind, handle)
        await self._store_health(kind, handle, status)
        return status

    async def _probe(self, kind: ConnectionKind, handle: str) -> ConnectionStatus:
        adapter = self._adapters.find(kind)
        if adapter is None:
            return ConnectionStatus.UNAVAILABLE
        try:
            async with asyncio.timeout(self._health_timeout):
                secret = await self._resolve(handle)
                verdict = await adapter.check_health(secret)
        except Exception as error:  # the resolver's or the adapter's: type only
            code = _failure_code_of(error)
            logger.warning(
                "connection health check failed: kind=%s code=%s exception_type=%s",
                kind.value,
                code.value,
                log_type_name(error),
            )
            if code is FailureCode.EXPIRED:
                return ConnectionStatus.EXPIRED
            return ConnectionStatus.UNAVAILABLE
        if type(verdict) is not ConnectionStatus:
            logger.warning("connection health check returned an invalid verdict")
            return ConnectionStatus.UNAVAILABLE
        return verdict

    async def _store_health(
        self, kind: ConnectionKind, handle: str, status: ConnectionStatus
    ) -> None:
        change = await self._call_store(self._store.record_health(kind, handle, status))
        if change is not None and change.previous is not status:
            await self._audit(
                ACTION_STATUS, status.value, True, None, uuid.uuid4(),
                _RESOURCE_CONNECTION, change.connection_id,
            )  # fmt: skip

    # --- quotas --------------------------------------------------------------

    async def set_quota(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        kind: ConnectionKind,
        metric: QuotaMetric,
        period: QuotaPeriod,
        limit: int | Unlimited,
    ) -> Quota:
        """Set a user's limit for a kind, metric and period: a number (0 blocks new
        tasks) or ``UNLIMITED``. Takes effect at the next admission. A user who does
        not exist or is deleted is ``TargetUserNotFoundError``."""
        self._actor(principal)
        user_id = validate_uuid("user_id", user_id)
        kind = validate_enum("kind", kind, ConnectionKind)
        metric = validate_enum("metric", metric, QuotaMetric)
        period = validate_enum("period", period, QuotaPeriod)
        limit = validate_quota_limit("limit", limit)
        correlation_id = uuid.uuid4()
        await self._authorize(
            principal,
            Capability.ADMIN_QUOTA_MANAGE,
            Resource(kind=_RESOURCE_QUOTA, id=user_id),
            correlation_id,
        )
        await self._require_owner_for_owner_quota(
            principal, user_id, ACTION_QUOTA_SET, correlation_id
        )
        quota = await self._call_store(
            self._store.upsert_quota(user_id, kind, metric, period, limit)
        )
        if quota is None:
            raise TargetUserNotFoundError()
        await self._audit(
            ACTION_QUOTA_SET, OUTCOME_SUCCEEDED, True, principal, correlation_id,
            _RESOURCE_QUOTA, user_id,
        )  # fmt: skip
        return quota

    async def remove_quota(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        kind: ConnectionKind,
        metric: QuotaMetric,
        period: QuotaPeriod,
    ) -> bool:
        """Remove one limit. ``True`` if it existed. What is not set is not enforced
        (Decision 0016, section 2): removing the last quota of a user and kind makes
        them unlimited for it; the calls stay attributed and audited."""
        self._actor(principal)
        user_id = validate_uuid("user_id", user_id)
        kind = validate_enum("kind", kind, ConnectionKind)
        metric = validate_enum("metric", metric, QuotaMetric)
        period = validate_enum("period", period, QuotaPeriod)
        correlation_id = uuid.uuid4()
        await self._authorize(
            principal,
            Capability.ADMIN_QUOTA_MANAGE,
            Resource(kind=_RESOURCE_QUOTA, id=user_id),
            correlation_id,
        )
        await self._require_owner_for_owner_quota(
            principal, user_id, ACTION_QUOTA_REMOVE, correlation_id
        )
        removed = await self._call_store(
            self._store.delete_quota(user_id, kind, metric, period)
        )
        if removed:
            await self._audit(
                ACTION_QUOTA_REMOVE, OUTCOME_SUCCEEDED, True, principal, correlation_id,
                _RESOURCE_QUOTA, user_id,
            )  # fmt: skip
        return removed

    async def quota_status(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        kind: ConnectionKind | None = None,
    ) -> tuple[QuotaUsage, ...]:
        """A user's quotas with what the current window of each has used (``kind``
        ``None``: both kinds). One's own needs ``agent.use``; another user's,
        ``admin.usage.view``."""
        self._actor(principal)
        user_id = validate_uuid("user_id", user_id)
        kind = None if kind is None else validate_enum("kind", kind, ConnectionKind)
        await self._authorize_view(principal, user_id)
        return await self._call_store(self._store.quota_status(user_id, kind))

    async def list_usage(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        kind: ConnectionKind | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> tuple[UsageRecord, ...]:
        """A user's usage records, newest first. Same authorization as
        ``quota_status``. A record has no prompt, answer or credential."""
        self._actor(principal)
        user_id = validate_uuid("user_id", user_id)
        kind = None if kind is None else validate_enum("kind", kind, ConnectionKind)
        limit, offset = validate_page(limit, offset)
        await self._authorize_view(principal, user_id)
        return await self._call_store(
            self._store.list_usage(user_id, kind, limit, offset)
        )

    # --- use -----------------------------------------------------------------

    async def execute(
        self,
        principal: Principal,
        context: TaskContext,
        kind: ConnectionKind,
        request: ConnectionRequest,
    ) -> ConnectionResult:
        """One call of a task through the shared connection of ``kind``.

        See the module docstring for the steps. ``principal`` must be the user the task
        works for (``context.delegator_id``) and the user who created the task. The
        result's ``text`` has the credential (exact value and recognisable formats)
        removed.

        Refusals before the call starts (all audited as ``connection.use``, nothing
        written to the usage table): ``TaskNotUsableError``,
        ``ConnectionUnavailableError``, ``QuotaExceededError``, ``TaskBudgetError``.
        A call that started and did not return an answer is ``ConnectionCallError``
        (recorded, counted). A database that does not answer in time is
        ``ConnectionBusyError``.
        """
        self._actor(principal)
        require_type("context", context, TaskContext)
        kind = validate_enum("kind", kind, ConnectionKind)
        require_type("request", request, ConnectionRequest)
        correlation_id = uuid.uuid4()
        await self._authorize(
            principal,
            Capability.AGENT_USE,
            Resource.owned_by(context.delegator_id, _RESOURCE_CONNECTION),
            correlation_id,
        )
        adapter = self._adapters.find(kind)
        if adapter is None:
            await self._refuse(
                principal, RefusalReason.CONNECTION_UNAVAILABLE, None, correlation_id
            )
        await self._check_task_budget(principal, context, correlation_id)

        admission = await self._call_store(
            self._store.admit(
                principal.user_id,
                context.task_id,
                context.run,
                kind,
                request.model,
                request.purpose,
            )
        )
        if isinstance(admission, Refused):
            await self._refuse(
                principal,
                admission.reason,
                admission.project_id,
                correlation_id,
                admission,
            )
        assert isinstance(admission, Admitted)

        outcome = _Outcome()
        record: UsageRecord | None = None
        try:
            try:
                await self._call(adapter, admission, request, outcome)
            except asyncio.CancelledError:
                outcome.cancelled()
                raise
        finally:
            # The call ran (or was cut short): it is recorded, and its tokens are
            # charged, before any cancellation of this task gets through.
            record = await self._account(kind, context.task_id, admission, outcome)
        if outcome.status is not UsageStatus.SUCCEEDED:
            assert outcome.failure is not None
            raise ConnectionCallError(outcome.failure)
        return ConnectionResult(
            admission.usage_id,
            outcome.text,
            outcome.input_tokens,
            outcome.output_tokens,
            None if record is None else record.duration_ms,
            outcome.redactions,
        )

    async def _call(
        self,
        adapter,
        admission: Admitted,
        request: ConnectionRequest,
        outcome: _Outcome,
    ) -> None:
        """Resolve the credential and run the adapter, under one deadline. Sets
        ``outcome`` on every path; raises only ``CancelledError`` (and, as for any
        code, ``BaseException`` that is not an ``Exception``)."""
        try:
            async with asyncio.timeout(request.timeout_seconds):
                await self._resolve_and_run(adapter, admission, request, outcome)
        except TimeoutError:
            logger.warning("connection call timed out: usage_id=%s", admission.usage_id)
            outcome.failed(FailureCode.TIMEOUT)

    async def _resolve_and_run(
        self,
        adapter,
        admission: Admitted,
        request: ConnectionRequest,
        outcome: _Outcome,
    ) -> None:
        try:
            secret = await self._resolve(admission.secret_handle)
        except Exception as error:
            logger.warning(
                "connection credential unavailable: exception_type=%s",
                log_type_name(error),
            )
            outcome.failed(FailureCode.UNAVAILABLE)
            return
        try:
            raw = await adapter.run(
                secret,
                AdapterRequest(request.model, request.prompt, request.timeout_seconds),
            )
        except Exception as error:
            code = _failure_code_of(error)
            logger.warning(
                "connection call failed: usage_id=%s code=%s exception_type=%s",
                admission.usage_id,
                code.value,
                log_type_name(error),
            )
            outcome.failed(code)
            return
        # The token counts are validated on their own (each of the two, independently
        # of the other and of the body), and every valid one is kept whatever happens
        # next: the provider consumed the tokens even if the answer cannot be
        # returned, so the usage row, the quotas and the task's budget count them. An
        # invalid count is not kept (NULL) and makes the response invalid.
        if type(raw) is not AdapterResult:
            self._log_invalid_response(admission, TypeError())
            outcome.failed(FailureCode.INVALID_RESPONSE)
            return
        outcome.input_tokens, input_valid = _token_count(raw.input_tokens)
        outcome.output_tokens, output_valid = _token_count(raw.output_tokens)
        if not (input_valid and output_valid):
            self._log_invalid_response(admission, ValueError())
            outcome.failed(FailureCode.INVALID_RESPONSE)
            return
        try:
            text = validate_result_text("text", raw.text, ANSWER_LIMIT)
        except Exception as error:
            self._log_invalid_response(admission, error)
            outcome.failed(FailureCode.INVALID_RESPONSE)
            return
        # The credential must not come back to the user or the agent, even if the
        # adapter (a bug, an echo of the provider) put it in the answer. Removing it
        # takes three steps, in this order, because each can change the text in a way
        # that matters to the others:
        #   1. scrub: the value, in the normalised, case-folded view of the text too
        #      (``ＡＢＣ１２３`` is the value ``ABC123``);
        #   2. redact the recognisable credential formats: ``redact_text`` normalises
        #      the WHOLE text when it finds one, which can CREATE the exact value
        #      (a composition, a format character removed between two halves), and it
        #      lengthens text (``token=abcdef`` becomes ``token=[REDACTED]``);
        #   3. scrub again, and only then check what is left.
        # An answer is returned whole or refused: never shortened (``redact_text``
        # cuts an input over its own limit and only marks it), never longer than the
        # limit, and never with the value still readable in it.
        text = secret.scrub(text)
        redactions = 0
        if len(text) <= ANSWER_LIMIT:
            text, redactions = redact_text(text)
            text = secret.scrub(text)
        if len(text) > ANSWER_LIMIT:
            logger.warning(
                "connection call returned an answer that is too long: usage_id=%s",
                admission.usage_id,
            )
            outcome.failed(FailureCode.INVALID_RESPONSE)
            return
        if secret.visible_in(text):
            logger.warning(
                "connection call returned an answer that still holds the credential:"
                " usage_id=%s",
                admission.usage_id,
            )
            outcome.failed(FailureCode.INVALID_RESPONSE)
            return
        outcome.status = UsageStatus.SUCCEEDED
        outcome.failure = None
        outcome.text = text
        outcome.redactions = redactions

    @staticmethod
    def _log_invalid_response(admission: Admitted, error: BaseException) -> None:
        logger.warning(
            "connection call returned an invalid response: usage_id=%s"
            " exception_type=%s",
            admission.usage_id,
            log_type_name(error),
        )

    async def _resolve(self, handle: str) -> Secret:
        secret = await self._secrets.resolve(handle)
        if type(secret) is not Secret:
            raise TypeError("the resolver did not return a Secret")
        return secret

    async def _account(
        self,
        kind: ConnectionKind,
        task_id: uuid.UUID,
        admission: Admitted,
        outcome: _Outcome,
    ) -> UsageRecord | None:
        """Settle the call and finish before any cancellation passes.

        The settlement runs as a task of its own that this coroutine keeps a
        reference to and waits for. A cancellation that arrives meanwhile (the
        database or the budget store is slow) is held back until the settlement has
        ended, and then raised: the caller still learns that it was cancelled (an
        ``asyncio.timeout`` around ``execute`` still becomes ``TimeoutError``), but
        never before the usage row and the budget charge exist. ``asyncio.shield``
        alone returned at once and left the settlement as a background task that
        nothing waited for or kept alive (the pattern is ``ToolRunner._account``).

        The wait is bounded by the settlement's own limits: the database timeout
        for the usage row, and the budget tracker's (unbounded: it uses a pooled
        session) for the charge. A task that the event loop itself cancels while it
        is closing (``asyncio.run`` cancels every task) cancels the settlement too;
        that cannot be prevented here, and the row then stays ``in_flight``.
        """
        settlement = asyncio.create_task(
            self._settle(kind, task_id, admission, outcome)
        )
        cancelled: asyncio.CancelledError | None = None
        while not settlement.done():
            try:
                # Unlike awaiting the task, asyncio.wait() does not cancel it when
                # this caller is cancelled.
                await asyncio.wait({settlement})
            except asyncio.CancelledError as error:
                cancelled = error  # raised below, once the settlement is done
        record = settlement.result()  # it never raises for a store or a budget
        if cancelled is not None:
            raise cancelled
        return record

    async def _settle(
        self,
        kind: ConnectionKind,
        task_id: uuid.UUID,
        admission: Admitted,
        outcome: _Outcome,
    ) -> UsageRecord | None:
        """Write the outcome; never raises (a failure is logged by exception type).

        Also: a call that failed because the provider no longer accepts the
        credential marks the connection ``expired``; the tokens the call reported
        are charged to the task's budget. Each is best effort and independent.
        """
        record: UsageRecord | None = None
        try:
            record = await self._store.settle(
                admission.usage_id,
                outcome.status,
                outcome.failure,
                outcome.input_tokens,
                outcome.output_tokens,
            )
        except Exception as error:
            logger.error(
                "connection usage could not be settled: usage_id=%s exception_type=%s",
                admission.usage_id,
                log_type_name(error),
            )
        if outcome.failure is FailureCode.EXPIRED:
            try:
                await self._store_health(
                    kind, admission.secret_handle, ConnectionStatus.EXPIRED
                )
            except Exception as error:
                logger.error(
                    "connection expiry could not be recorded: exception_type=%s",
                    log_type_name(error),
                )
        tokens = (outcome.input_tokens or 0) + (outcome.output_tokens or 0)
        if self._budget is not None and tokens > 0:
            try:
                await self._budget.record(task_id, BudgetKind.TOKENS, tokens)
            except Exception as error:
                logger.error(
                    "task budget could not be charged: usage_id=%s exception_type=%s",
                    admission.usage_id,
                    log_type_name(error),
                )
        return record

    async def _check_task_budget(
        self, principal: Principal, context: TaskContext, correlation_id: uuid.UUID
    ) -> None:
        """Refuse a call when the task's own token budget is used up (PAW-033).

        "May the task do one more?": ``consumed + 1 > limit``. A task without budget
        rows is refused (no budget is not unlimited, as in ``BudgetTracker``). Other
        errors of the tracker propagate: nothing runs.
        """
        if self._budget is None:
            return
        try:
            verdict = await self._budget.check(
                context.task_id, planned={BudgetKind.TOKENS: 1}
            )
        except BudgetNotConfiguredError:
            await self._refuse(
                principal,
                RefusalReason.TASK_BUDGET_NOT_CONFIGURED,
                None,
                correlation_id,
            )
        else:
            if BudgetKind.TOKENS in verdict.exceeded:
                await self._refuse(
                    principal, RefusalReason.TASK_BUDGET_EXCEEDED, None, correlation_id
                )

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _actor(principal: object) -> None:
        if type(principal) is not Principal:
            raise ConnectionPermissionDeniedError(Reason.UNAUTHENTICATED)

    async def _authorize(
        self,
        principal: Principal,
        capability: Capability,
        resource: Resource,
        correlation_id: uuid.UUID,
    ) -> None:
        decision = await self._authorizer.authorize(
            principal, capability, resource, correlation_id=correlation_id
        )
        if not decision.allowed:
            raise ConnectionPermissionDeniedError(decision.reason)

    async def _require_owner_for_owner_quota(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        action: str,
        correlation_id: uuid.UUID,
    ) -> None:
        """The quota of an Owner is changed by the Owner only (Decision 0016, section
        7: an Admin does not manage the Owner, as in Decision 0004).

        The target's role is read from the store, never taken from the caller. The
        refusal is audited (``reason`` ``owner_quota_owner_only``) and is a
        ``CAPABILITY_NOT_GRANTED`` denial. The read and the change are separate
        statements: a role that changes between them is not a case this guards.
        """
        role = await self._call_store(self._store.read_system_role(user_id))
        if (
            role == SystemRole.OWNER.value
            and principal.system_role is not SystemRole.OWNER
        ):
            await self._audit(
                action, OWNER_ONLY, False, principal, correlation_id,
                _RESOURCE_QUOTA, user_id,
            )  # fmt: skip
            raise ConnectionPermissionDeniedError(Reason.CAPABILITY_NOT_GRANTED)

    async def _authorize_view(self, principal: Principal, user_id: uuid.UUID) -> None:
        """One's own usage and quotas need ``agent.use`` (SELF); another user's need
        ``admin.usage.view``."""
        if user_id == principal.user_id:
            await self._authorize(
                principal,
                Capability.AGENT_USE,
                Resource.owned_by(user_id, _RESOURCE_USAGE, user_id),
                uuid.uuid4(),
            )
        else:
            await self._authorize(
                principal,
                Capability.ADMIN_USAGE_VIEW,
                Resource(kind=_RESOURCE_USAGE, id=user_id),
                uuid.uuid4(),
            )

    async def _authorize_existing(
        self, principal: Principal, kind: ConnectionKind, correlation_id: uuid.UUID
    ) -> uuid.UUID:
        """``admin.config.manage`` for removing a connection; returns its id (for the
        audit row) or raises ``ConnectionNotFoundError``."""
        await self._authorize(
            principal,
            Capability.ADMIN_CONFIG_MANAGE,
            _connection_resource(),
            correlation_id,
        )
        info = await self._call_store(self._store.get_connection(kind))
        if info is None:
            raise ConnectionNotFoundError()
        return info.id

    @staticmethod
    async def _call_store(awaitable):
        """Await a store call; a database that did not answer in time is
        ``ConnectionBusyError`` (the deadline of ``Database.transact_abortable``)."""
        try:
            return await awaitable
        except TimeoutError:
            raise ConnectionBusyError() from None

    async def _refuse(
        self,
        principal: Principal,
        reason: RefusalReason,
        project_id: uuid.UUID | None,
        correlation_id: uuid.UUID,
        refused: Refused | None = None,
    ):
        """Audit the refusal to start a call and raise its typed error."""
        await self._audit(
            ACTION_USE, reason.value, False, principal, correlation_id,
            _RESOURCE_CONNECTION, None, project_id,
        )  # fmt: skip
        if reason is RefusalReason.QUOTA_EXCEEDED and refused is not None:
            assert refused.metric is not None and refused.period is not None
            raise QuotaExceededError(refused.metric, refused.period, refused.resets_at)
        if reason in _TASK_REFUSALS:
            raise TaskNotUsableError(reason)
        if reason in (
            RefusalReason.TASK_BUDGET_EXCEEDED,
            RefusalReason.TASK_BUDGET_NOT_CONFIGURED,
        ):
            raise TaskBudgetError(reason)
        raise ConnectionUnavailableError()

    async def _audit(
        self,
        action: str,
        reason: str,
        allowed: bool,
        principal: Principal | None,
        correlation_id: uuid.UUID,
        resource_kind: str,
        resource_id: uuid.UUID | None,
        project_id: uuid.UUID | None = None,
    ) -> None:
        """Write one event of this module; never raises (a failure is logged by type).

        Ids and enums only. ``principal`` is ``None`` for a change the system found
        (a health check): the event then names the ``system`` role and no user.
        """
        try:
            event = AuditEvent(
                event_id=uuid.uuid4(),
                correlation_id=correlation_id,
                occurred_at=datetime.now(UTC),
                actor_id=None if principal is None else principal.user_id,
                actor_role=(
                    _SYSTEM_ROLE if principal is None else principal.system_role.value
                ),
                action=action,
                resource_kind=resource_kind,
                resource_id=resource_id,
                project_id=project_id,
                decision="allow" if allowed else "deny",
                reason=reason,
            )
            async with asyncio.timeout(self._audit_timeout):
                await self._sink.record(event)
        except Exception as error:
            logger.error(
                "connection audit write failed (%s) for action %s",
                log_type_name(error),
                action,
            )


def _connection_resource() -> Resource:
    return Resource(kind=_RESOURCE_CONNECTION)
