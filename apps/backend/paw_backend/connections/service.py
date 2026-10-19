"""The shared connection service: Codex / Claude connections, quotas, attributed use.

What it is (and is not)
-----------------------
A backend library on PostgreSQL. Its quotas and usage are served over HTTP by
``api/v1/usage.py`` (issue #187); the connections themselves have no endpoint yet. It
makes no network call and starts no process: the provider is reached only through
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
* ``quota_status``, ``list_usage``, ``usage_report`` of ANOTHER user, and
  ``workspace_usage_report``: ``admin.usage.view``;
* ``quota_status``, ``list_usage``, ``usage_report`` of one's OWN, ``availability``
  and ``execute``: ``agent.use``, a ``Scope.SELF`` capability: the resource is
  owned by the user the call is for, so an Owner does not read or spend another
  user's;
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
``task_*``, ``task_budget_*``, ``lease_*``). An allowed call is recorded as its usage
row (user, task, project, kind, model, purpose, tokens, duration) and by the
Authorizer's ``agent.use`` row; not a third time. These events are written AFTER the
change or the refusal and are best effort (a failure is logged by exception type and
does not undo the change; the refusal stands): the decision rows are the fail-closed
ones.
Each refusal writes one row per attempt, so a caller that retries in a loop after a
quota refusal fills the audit table: queue the task until ``resets_at`` instead.

``execute``
-----------
1. the arguments; 2. ``agent.use`` on the user's own resource (the user is
``context.delegator_id``: a principal that is somebody else is denied); 3. an adapter
must be registered for the kind; 4. the task's own token budget (PAW-033
``BudgetTracker``, if the service was given one): exhausted or not configured refuses
the call; 4a. the worker's QUEUE LEASE (``context.lease``, asked of the service's
``LeaseVerifier`` for every call, as the Tool Broker does: issue #153, Decision 0057):
a lease that was lost, expired or taken over refuses the call (``lease_lost``), and so
does one that cannot be read (``lease_unavailable``: fail closed); 5. the ADMISSION
(``ConnectionStore.admit``): one transaction that checks the task, the connection and
the quotas and inserts the ``in_flight`` usage row; 6. the
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
  duration, until the orchestrator's reaper (``orchestrator/connection_reaper.py``,
  Decision 0016) settles it as ``failed`` a day and an hour after it started.
* The budget charge is a separate transaction from the settlement.
* A prompt is not inspected here (credential detection and privacy filtering of what
  is sent to a cloud provider belong to the Orchestrator and the Tool Broker).
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
    CredentialNotStoredError,
    InputProblem,
    InvalidConnectionInputError,
    QuotaExceededError,
    SessionSlotBusyError,
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
from paw_backend.connections.report import UsageRange, UsageReport
from paw_backend.connections.secret import Secret, SecretResolver
from paw_backend.connections.secret_store import (
    CredentialStore,
    StaleCredentialVersionError,
)
from paw_backend.connections.session import (
    SESSION_MIN_TOKENS,
    UNKNOWN_SESSION_TOKENS,
    AgentSessionAdapter,
    AgentSessionRequest,
    AgentSessionResult,
    SessionRequest,
    SessionResult,
    SessionStop,
    WriteBack,
)
from paw_backend.connections.session_slot import (
    SLOT_HEARTBEAT_SECONDS,
    SLOT_STOP_MARGIN_SECONDS,
    SLOT_TTL_SECONDS,
    SLOT_WAIT_SECONDS,
    SessionSlot,
    check_slot_timing,
    run_within,
)
from paw_backend.connections.store import (
    Admitted,
    ConnectionStore,
    Refused,
    SessionTurn,
)
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
from paw_backend.tools.credentials import (
    MAX_RESULT_CHARS as MAX_SUBMISSION_CHARS,
)
from paw_backend.tools.credentials import (
    MAX_RESULT_NODES,
    MAX_TEXT_CHARS,
    redact_text,
    redact_value,
)
from paw_backend.tools.interfaces import require_async_method
from paw_backend.tools.lease import FailClosedLeaseVerifier, LeaseStatus, LeaseVerifier

logger = logging.getLogger(__name__)

# Audit actions written by this module (the Authorizer's rows carry capability names).
ACTION_CONNECT = "connection.connect"
ACTION_REPLACE = "connection.replace"
ACTION_ENABLE = "connection.enable"
ACTION_DISABLE = "connection.disable"
ACTION_DISCONNECT = "connection.disconnect"
ACTION_ROTATE = "connection.rotate"
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
    RefusalReason.LEASE_LOST,
    RefusalReason.LEASE_UNAVAILABLE,
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


#: How often a session's settlement (the row and its charge, one transaction)
#: is tried before it is left to the reaper.
_SETTLE_ATTEMPTS = 3


def _session_finals(
    counted_input: int,
    counted_output: int,
    totals: tuple[int | None, int | None],
    estimate: int,
) -> tuple[int, int]:
    """The final input and output tokens of a session (``settle_session``'s
    rule): per part the larger of the known total and what was counted; when
    a part is unknown, the whole is topped up to ``estimate`` on that part."""
    known_input, known_output = totals
    final_input = max(counted_input, known_input or 0)
    final_output = max(counted_output, known_output or 0)
    gap = max(0, estimate - final_input - final_output)
    if known_input is None:
        final_input += gap
    elif known_output is None:
        final_output += gap
    return final_input, final_output


def _is_node_stop(error: BaseException) -> bool:
    """Is ``error`` the orchestrator's ``NodeStopped`` (imported here, late:
    the connections package does not depend on the orchestrator otherwise)?"""
    from paw_backend.orchestrator.errors import NodeStopped

    return isinstance(error, NodeStopped)


_SUBMISSION_MAX_DEPTH = 32


def _clean_submission(secret: Secret, submission: Mapping[str, Any]) -> dict | None:
    """``submission`` with every text (keys too) cleaned as a one-call answer
    is (scrub, redact the credential formats, scrub again), or ``None`` when a
    text still shows the value or the shape is not JSON-like (str keys; str,
    int, float, bool, None, lists and mappings, at most 32 levels deep)."""

    # The read limits of a tool result (``redact_value``), enforced while
    # this pass copies: a huge submission is refused, not walked.
    left = {"nodes": MAX_RESULT_NODES, "chars": MAX_SUBMISSION_CHARS}

    def text(value: str) -> str | None:
        left["chars"] -= len(value)
        if left["chars"] < 0:
            return None
        cleaned = secret.scrub(value)
        cleaned, _ = redact_text(cleaned)
        cleaned = secret.scrub(cleaned)
        return None if secret.visible_in(cleaned) else cleaned

    def clean(value: Any, depth: int) -> tuple[bool, Any]:
        left["nodes"] -= 1
        if depth > _SUBMISSION_MAX_DEPTH or left["nodes"] < 0:
            return False, None
        if isinstance(value, str):
            cleaned = text(value)
            return cleaned is not None, cleaned
        if value is None or isinstance(value, (bool, int, float)):
            return True, value
        if isinstance(value, Mapping):
            out = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    return False, None
                cleaned_key = text(key)
                ok, cleaned_item = clean(item, depth + 1)
                if cleaned_key is None or not ok:
                    return False, None
                out[cleaned_key] = cleaned_item
            return True, out
        if isinstance(value, (list, tuple)):
            items = []
            for item in value:
                ok, cleaned_item = clean(item, depth + 1)
                if not ok:
                    return False, None
                items.append(cleaned_item)
            return True, items
        return False, None

    ok, cleaned = clean(submission, 0)
    if not ok:
        return None
    # Then the structured rules of a tool result (``redact_value``): the value
    # under a sensitive-looking key (``api_key``, ``password``, ...) goes
    # whatever it holds. Its normalisation can compose text again, so the
    # value is looked for once more afterwards.
    redacted, _ = redact_value(cleaned)
    left["nodes"], left["chars"] = MAX_RESULT_NODES, MAX_SUBMISSION_CHARS
    ok, cleaned = clean(redacted, 0)
    return cleaned if ok else None


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


@dataclass(slots=True)
class _Session:
    """What one session came to (in memory; set by ``execute_session``)."""

    status: UsageStatus = UsageStatus.FAILED
    failure: FailureCode | None = FailureCode.INTERNAL_ERROR
    counted_input: int = 0
    counted_output: int = 0
    known_totals: tuple[int | None, int | None] = (None, None)
    submission: Any = None
    stopped: SessionStop | None = None
    final_input: int = 0
    final_output: int = 0

    def fail(self, code: FailureCode) -> None:
        self.status = UsageStatus.FAILED
        self.failure = code

    def __setattr__(self, name: str, value: object) -> None:
        if name == "status" and value is not UsageStatus.FAILED:
            object.__setattr__(self, "failure", None)
        object.__setattr__(self, name, value)


def _exclusive(adapter: object) -> bool:
    """Does ``adapter`` run one session per connection (Decision 0083, 6)?
    Read from its class, exactly ``True``."""
    return getattr(type(adapter), "exclusive_sessions", False) is True


def _session_adapters(
    adapters: Mapping[ConnectionKind, AgentSessionAdapter] | None,
) -> dict[ConnectionKind, AgentSessionAdapter]:
    """The session adapters by kind, each checked (``kind`` matches, an async
    ``run_session`` of two arguments): a wrong one fails when the service is
    built."""
    result: dict[ConnectionKind, AgentSessionAdapter] = {}
    for kind, adapter in (adapters or {}).items():
        if (
            type(kind) is not ConnectionKind
            or getattr(adapter, "kind", None) is not kind
        ):
            raise fail("session_adapters", InputProblem.WRONG_TYPE)
        try:
            require_async_method(adapter, "run_session", 2)
        except TypeError:
            raise fail("session_adapters", InputProblem.WRONG_TYPE) from None
        result[kind] = adapter
    return result


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
        lease: LeaseVerifier | None = None,
        session_adapters: Mapping[ConnectionKind, AgentSessionAdapter] | None = None,
        credential_store: CredentialStore | None = None,
        period_timezone: str = DEFAULT_PERIOD_TIMEZONE,
        database_timeout_seconds: float = DEFAULT_DATABASE_TIMEOUT_SECONDS,
        health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        clock=None,
        allow_explicit_clock: bool = False,
        session_slot_wait_seconds: float = SLOT_WAIT_SECONDS,
        session_slot_ttl_seconds: float = SLOT_TTL_SECONDS,
        session_slot_heartbeat_seconds: float = SLOT_HEARTBEAT_SECONDS,
        session_slot_margin_seconds: float = SLOT_STOP_MARGIN_SECONDS,
    ) -> None:
        """Wire the service.

        A wrong collaborator fails HERE (``InvalidConnectionInputError`` for a wrong
        type, a bad time zone or number), not on the first call.

        ``lease`` is the queue lease check of ``execute`` (issue #153, Decision
        0057): in production ``orchestrator.QueueLeaseVerifier`` on the task
        queue, as the Tool Broker's. Without one no lease is known and every call
        is refused (``FailClosedLeaseVerifier``, as in the Broker).

        ``credential_store`` (Decision 0087, 1): the secret store whose files
        the handles name (``FileSecretStore``). With it, a handle is registered
        only while the store holds it for that kind (under the handle's lock),
        a credential nothing refers to any more is deleted after a replacement
        or a disconnection, and ``rotate_credential`` is possible. Without it
        (the HTTP layer's quota service, the tests), handles are taken as given.

        ``session_slot_*`` (Decision 0083, 6; ``session_slot``): how long a
        session of an ``exclusive_sessions`` adapter waits for its
        connection's slot (60 s), how long a lease lasts after its heartbeat,
        how often it is renewed, and how long before it could expire a holder
        that cannot renew stops its session. The defaults are production's;
        the tests shorten them.

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
            if lease is not None:
                require_async_method(lease, "check", 2)
            if credential_store is not None:
                for method, arity in (("verify", 2), ("delete", 1), ("rotate", 4)):
                    require_async_method(credential_store, method, arity)
                if not callable(getattr(credential_store, "lock", None)):
                    raise TypeError("lock")
        except TypeError:
            raise fail("collaborator", InputProblem.WRONG_TYPE) from None
        if budget is not None:
            require_type("budget", budget, BudgetTracker)
        validate_seconds(
            "health_timeout_seconds", health_timeout_seconds, MAX_HEALTH_TIMEOUT_SECONDS
        )
        try:
            check_slot_timing(
                session_slot_wait_seconds,
                session_slot_ttl_seconds,
                session_slot_heartbeat_seconds,
                session_slot_margin_seconds,
            )
        except ValueError:
            raise fail("session_slot", InputProblem.OUT_OF_RANGE) from None
        self._slot_wait = float(session_slot_wait_seconds)
        self._slot_timing = {
            "ttl_seconds": float(session_slot_ttl_seconds),
            "heartbeat_seconds": float(session_slot_heartbeat_seconds),
            "margin_seconds": float(session_slot_margin_seconds),
        }
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
        self._credentials = credential_store
        self._budget = budget
        self._session_adapters = _session_adapters(session_adapters)
        if self._session_adapters and budget is None:
            # A session is counted turn by turn into the task's budget, with
            # the run fence of ``BudgetTracker.record_in`` (Decision 0083, 5):
            # without a budget there is neither, so sessions are not offered.
            raise fail("budget", InputProblem.WRONG_TYPE)
        self._lease: LeaseVerifier = (
            FailClosedLeaseVerifier() if lease is None else lease
        )
        # The lease check is one read of the queue: the deadline of a statement.
        self._lease_timeout = float(database_timeout_seconds)
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
        async with self._credential_lock(handle):
            await self._require_stored(handle, kind)
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
        async with self._current_handle_locked(kind, handle) as previous:
            await self._require_stored(handle, kind)
            info = await self._call_store(self._store.replace_handle(kind, handle))
        if info is None:
            raise ConnectionNotFoundError()
        if previous is not None and previous != handle:
            await self._discard_credential(previous)
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
        async with self._current_handle_locked(kind) as previous:
            deleted = await self._call_store(self._store.delete_connection(kind))
        if not deleted:
            raise ConnectionNotFoundError()
        if previous is not None:
            await self._discard_credential(previous)
        await self._audit(
            ACTION_DISCONNECT, OUTCOME_SUCCEEDED, True, principal, correlation_id,
            _RESOURCE_CONNECTION, existing,
        )  # fmt: skip

    async def rotate_credential(
        self,
        kind: ConnectionKind,
        handle: str,
        expected_version: int,
        value: str,
    ) -> int:
        """Write back a refreshed credential (Decision 0083, 6: a CLI that
        refreshed its OAuth token during a session). Backend-internal, no
        actor, like ``check_health``: the same connection's same handle only,
        and only while ``expected_version`` is still its current version
        (``StaleCredentialVersionError`` otherwise: somebody else wrote first).
        Never a different credential (that is ``replace_credential``, an
        Owner's / Admin's change). Audited as the system's ``connection.rotate``.
        Returns the new version."""
        kind = validate_enum("kind", kind, ConnectionKind)
        handle = validate_handle("secret_handle", handle)
        if self._credentials is None:
            raise ConnectionUnavailableError()
        current = await self._call_store(self._store.get_connection(kind))
        if current is None:
            raise ConnectionNotFoundError()

        async def still_attached() -> None:
            # Under the handle's lock: a replacement or a disconnection holds
            # the same lock while it changes the row (``_current_handle_
            # locked``), so a credential detached meanwhile is never rotated.
            if (await self._call_store(self._store.read_handle(kind))) != handle:
                raise ConnectionNotFoundError()

        version = await self._credentials.rotate(
            handle, kind, expected_version, value, check=still_attached
        )
        await self._audit(
            ACTION_ROTATE, OUTCOME_SUCCEEDED, True, None, uuid.uuid4(),
            _RESOURCE_CONNECTION, current.id,
        )  # fmt: skip
        return version

    async def collect_credentials(self) -> int:
        """Delete the stored credentials nothing refers to any more (the secret
        store's cleanup, Decision 0087, 1); 0 without a store."""
        if self._credentials is None or not hasattr(self._credentials, "collect"):
            return 0
        return await self._credentials.collect(self._store.handle_referenced)

    @contextlib.asynccontextmanager
    async def _current_handle_locked(self, kind: ConnectionKind, *also: str):
        """Hold the secret store's locks of the connection's current handle
        (read again once they are held: the handle that is current under the
        lock) and of ``also``, in a fixed order; yield the current handle
        (``None``: no connection). A rotation of the current handle
        (``rotate_credential``) checks under the same lock, so a replacement
        or a disconnection and a write-back never cross. Without a store,
        no lock."""
        while True:
            previous = await self._call_store(self._store.read_handle(kind))
            handles = sorted({h for h in (previous, *also) if h is not None})
            async with contextlib.AsyncExitStack() as stack:
                for name in handles:
                    await stack.enter_async_context(self._credential_lock(name))
                again = await self._call_store(self._store.read_handle(kind))
                if again == previous:
                    yield previous
                    return

    def _credential_lock(self, handle: str):
        """The secret store's lock of ``handle`` (none without a store)."""
        if self._credentials is None:
            return contextlib.nullcontext()
        return self._credentials.lock(handle)

    async def _require_stored(self, handle: str, kind: ConnectionKind) -> None:
        if self._credentials is not None and not await self._credentials.verify(
            handle, kind
        ):
            raise CredentialNotStoredError()

    async def _discard_credential(self, handle: str) -> None:
        """Delete the stored credential ``handle`` if nothing refers to it any
        more (checked under its lock, so a registration of the same handle in
        the meantime keeps it). Best effort: a failure is logged by type and
        the store's cleanup deletes it later."""
        if self._credentials is None:
            return
        try:
            async with self._credentials.lock(handle):
                if not await self._call_store(self._store.handle_referenced(handle)):
                    await self._credentials.delete(handle)
        except Exception as error:
            logger.error(
                "a replaced credential could not be deleted: exception_type=%s",
                log_type_name(error),
            )

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

    async def usage_report(
        self, principal: Principal, usage_range: UsageRange, user_id: uuid.UUID
    ) -> UsageReport:
        """One user's report for the Usage screen (``connections/report.py``). One's
        own needs ``agent.use``, another user's ``admin.usage.view`` (as
        ``quota_status``)."""
        self._actor(principal)
        usage_range = validate_enum("usage_range", usage_range, UsageRange)
        user_id = validate_uuid("user_id", user_id)
        await self._authorize_view(principal, user_id)
        return await self._call_store(
            self._store.usage_report(usage_range, user_id, principal.user_id)
        )

    async def workspace_usage_report(
        self, principal: Principal, usage_range: UsageRange
    ) -> UsageReport:
        """The whole workspace's report: every user's totals and quotas, and the
        asking user's own quotas. Needs ``admin.usage.view``."""
        self._actor(principal)
        usage_range = validate_enum("usage_range", usage_range, UsageRange)
        await self._authorize(
            principal,
            Capability.ADMIN_USAGE_VIEW,
            Resource(kind=_RESOURCE_USAGE),
            uuid.uuid4(),
        )
        return await self._call_store(
            self._store.usage_report(usage_range, None, principal.user_id)
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
        await self._check_lease(principal, context, correlation_id)

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

    async def execute_session(
        self,
        principal: Principal,
        context: TaskContext,
        kind: ConnectionKind,
        request: SessionRequest,
        *,
        gate: Callable[[], Awaitable[None]] | None = None,
        alive: Callable[[], bool] | None = None,
    ) -> SessionResult:
        """One CLI session of a node attempt through the shared connection of
        ``kind`` (Decision 0083, 1 and 5). The same checks as :meth:`execute`, in
        the same order, plus the session's token floor (5 (a): at least
        ``SESSION_MIN_TOKENS`` of the task's budget left, ``TaskBudgetError``
        ``task_token_floor``); then the admission (a session row, ``connection.
        use`` audited as for a call); then ``gate`` (the caller's placement
        record, Decision 0083, 1: ``CloudPolicy`` -> admission -> placement ->
        adapter), whose failure closes the row ``cancelled`` without sending
        anything and propagates; then the adapter.

        Turn by turn the adapter reports its tokens (``on_usage``): each turn is
        counted into the row and charged to the task's budget in one transaction
        with the lease check (:meth:`ConnectionStore.add_session_turn`); the
        answer is ``False`` once the budget is used up, the lease is lost or the
        run was replaced, and the adapter stops (``SessionResult.stopped``). At
        the end only the remainder is settled (the totals less what was counted,
        never less than counted); unknown totals (an abnormal end, a
        cancellation) count ``UNKNOWN_SESSION_TOKENS`` less what was counted
        (5 (c), (d)). A cancellation is settled (``cancelled``) before it passes.

        ``alive`` (the caller's attempt fence, Decision 0046): asked after every
        turn is counted; once it answers ``False`` (the orchestrator abandoned
        the attempt) the session stops (``SessionStop.ABANDONED``).

        An adapter with ``exclusive_sessions`` (Decision 0083, 6: Codex's
        ChatGPT login) runs one session per connection at a time: after the
        checks above and BEFORE the admission, the connection's session slot
        (a database lease, ``session_slot``) is waited for up to 60 s; when it
        stays taken, ``SessionSlotBusyError`` (``connection_busy``) is raised
        with nothing admitted, audited or recorded. The slot is held, with its
        heartbeat, until the session was settled; a holder that cannot renew
        it cancels its session in time (the adapter stops the CLI and writes
        its credential back) and the session fails ``unavailable``. Such a
        session's adapter is also given ``write_back``: the refreshed
        credential, written as the stored credential's next version only while
        the version the session was given is still the current one
        (compare-and-swap under the credential's lock, ``rotate_credential``).
        """
        self._actor(principal)
        require_type("context", context, TaskContext)
        kind = validate_enum("kind", kind, ConnectionKind)
        require_type("request", request, SessionRequest)
        if gate is not None and not callable(gate):
            raise fail("gate", InputProblem.WRONG_TYPE)
        if alive is not None and not callable(alive):
            raise fail("alive", InputProblem.WRONG_TYPE)
        correlation_id = uuid.uuid4()
        await self._authorize(
            principal,
            Capability.AGENT_USE,
            Resource.owned_by(context.delegator_id, _RESOURCE_CONNECTION),
            correlation_id,
        )
        adapter = self._session_adapters.get(kind)
        if adapter is None:
            await self._refuse(
                principal, RefusalReason.CONNECTION_UNAVAILABLE, None, correlation_id
            )
        await self._check_task_budget(principal, context, correlation_id)
        await self._check_session_floor(principal, context, correlation_id)
        await self._check_lease(principal, context, correlation_id)
        slot = None
        if _exclusive(adapter):
            slot = SessionSlot(
                self._store, kind, call=self._call_store, **self._slot_timing
            )
            if not await slot.acquire(self._slot_wait):
                logger.warning(
                    "connection session slot stayed taken: kind=%s", kind.value
                )
                raise SessionSlotBusyError()
        try:
            return await self._admitted_session(
                principal,
                context,
                kind,
                request,
                adapter,
                correlation_id,
                gate,
                alive,
                slot,
            )
        finally:
            if slot is not None:
                await self._shielded(slot.release())

    async def _admitted_session(
        self,
        principal: Principal,
        context: TaskContext,
        kind: ConnectionKind,
        request: SessionRequest,
        adapter: AgentSessionAdapter,
        correlation_id: uuid.UUID,
        gate: Callable[[], Awaitable[None]] | None,
        alive: Callable[[], bool] | None,
        slot: SessionSlot | None,
    ) -> SessionResult:
        admission = await self._call_store(
            self._store.admit(
                principal.user_id,
                context.task_id,
                context.run,
                kind,
                request.model,
                request.purpose,
                session=True,
                min_tokens=SESSION_MIN_TOKENS,
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
        state = _Session()
        try:
            if gate is not None:
                try:
                    await gate()
                except BaseException:
                    state.status = UsageStatus.CANCELLED
                    state.known_totals = (0, 0)  # nothing was sent
                    raise
            try:
                work = self._run_session(
                    adapter, admission, context, request, state, alive, kind, slot
                )
                if slot is None:
                    await work
                elif not await run_within(slot, work):
                    # The slot could not be renewed: the session was stopped
                    # before its lease could expire (Decision 0083's approval,
                    # condition (2)); what it spent is unknown.
                    state.fail(FailureCode.UNAVAILABLE)
                    state.known_totals = (None, None)
            except asyncio.CancelledError:
                state.status = UsageStatus.CANCELLED
                raise
        finally:
            await self._shielded(self._settle_session(kind, context, admission, state))
        return SessionResult(
            admission.usage_id,
            state.submission,
            state.final_input,
            state.final_output,
            state.failure,
            state.stopped,
        )

    async def _check_session_floor(
        self, principal: Principal, context: TaskContext, correlation_id: uuid.UUID
    ) -> None:
        if self._budget is None:
            return
        verdict = await self._budget.check(
            context.task_id, planned={BudgetKind.TOKENS: SESSION_MIN_TOKENS}
        )
        if BudgetKind.TOKENS in verdict.exceeded:
            await self._refuse(
                principal, RefusalReason.TASK_TOKEN_FLOOR, None, correlation_id
            )

    async def _run_session(
        self,
        adapter: AgentSessionAdapter,
        admission: Admitted,
        context: TaskContext,
        request: SessionRequest,
        state: "_Session",
        alive: Callable[[], bool] | None = None,
        kind: ConnectionKind | None = None,
        slot: SessionSlot | None = None,
    ) -> None:
        async def on_usage(input_tokens: int, output_tokens: int) -> bool:
            if state.stopped is not None:
                return False
            for value in (input_tokens, output_tokens):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= MAX_TOKENS_PER_CALL
                ):
                    raise fail("on_usage", InputProblem.OUT_OF_RANGE)
            turn = await self._store.add_session_turn(
                admission.usage_id,
                context.task_id,
                context.run,
                context.lease,
                input_tokens,
                output_tokens,
                self._budget,
            )
            if turn in (SessionTurn.RECORDED, SessionTurn.USED_UP):
                state.counted_input += input_tokens
                state.counted_output += output_tokens
                if turn is SessionTurn.USED_UP:
                    state.stopped = SessionStop.BUDGET_EXCEEDED
            elif turn is SessionTurn.LEASE_LOST:
                state.stopped = SessionStop.LEASE_LOST
            elif turn is SessionTurn.RUN_REPLACED:
                state.stopped = SessionStop.RUN_REPLACED
            else:
                state.stopped = SessionStop.BUDGET_EXCEEDED
            # (Whether the budget allows another turn was decided in the
            # turn's transaction, with its run and lease fences: ``USED_UP``.)
            # The turn was spent (and counted above) either way; an abandoned
            # attempt must not get another one.
            if state.stopped is None and alive is not None and alive() is not True:
                state.stopped = SessionStop.ABANDONED
            return state.stopped is None

        try:
            async with asyncio.timeout(request.timeout_seconds):
                write_back = None
                if slot is not None and kind is not None:
                    secret, write_back = await self._session_credential(
                        kind, admission.secret_handle
                    )
                else:
                    secret = await self._resolve(admission.secret_handle)
                # The last fences before the first turn is sent: the attempt,
                # and (a turn of 0 tokens) the run and the queue lease, after
                # everything this side waited for.
                if not await on_usage(0, 0):
                    state.status = UsageStatus.CANCELLED
                    state.known_totals = (0, 0)  # nothing was sent
                    return
                raw = await adapter.run_session(
                    secret,
                    AgentSessionRequest(
                        request.model,
                        request.prompt,
                        request.timeout_seconds,
                        on_usage,
                        attachment=request.attachment,
                        connection_id=admission.connection_id,
                        write_back=write_back,
                    ),
                )
        except TimeoutError:
            state.fail(FailureCode.TIMEOUT)
            return
        except Exception as error:
            if _is_node_stop(error) or isinstance(error, MemoryError):
                # The attempt was stopped (its bridge's tool call was fenced
                # out), or memory ran out (the orchestrator records it as
                # such): either passes to the runtime unchanged, after the
                # settlement (``execute_session``'s ``finally``).
                state.status = UsageStatus.CANCELLED
                raise
            code = _failure_code_of(error)
            logger.warning(
                "connection session failed: usage_id=%s code=%s exception_type=%s",
                admission.usage_id,
                code.value,
                log_type_name(error),
            )
            state.fail(code)
            return
        if type(raw) is not AgentSessionResult:
            state.fail(FailureCode.INVALID_RESPONSE)
            return
        # Each part of the totals as far as it is known (a known input is kept
        # even when the output is not, and the other way round), read before
        # anything below can refuse the result: what was spent is charged.
        state.known_totals = (raw.input_tokens, raw.output_tokens)
        if raw.submission is not None:
            # The credential must not come back in the submission either (an
            # adapter's bug, an echo of the CLI): every text is cleaned as the
            # one-call answer is, and a submission that still holds the value
            # is refused whole.
            submission = _clean_submission(secret, raw.submission)
            if submission is None:
                logger.warning(
                    "connection session returned a submission that holds the"
                    " credential or is malformed: usage_id=%s",
                    admission.usage_id,
                )
                state.fail(FailureCode.INVALID_RESPONSE)
                return
            state.submission = submission
        if raw.failure is not None:
            state.fail(raw.failure)
        elif state.stopped is None:
            state.status = UsageStatus.SUCCEEDED
        else:
            state.status = UsageStatus.CANCELLED

    async def _session_credential(self, kind: ConnectionKind, handle: str):
        """The credential of an exclusive session and its write-back
        (Decision 0083, 6), or the resolver's credential and no write-back
        when there is no secret store to write to."""
        reader = getattr(self._credentials, "read", None)
        if self._credentials is None or not callable(reader):
            return await self._resolve(handle), None
        stored = await reader(handle)
        if stored.kind is not kind or type(stored.secret) is not Secret:
            raise TypeError("the stored credential is not this connection's")
        version = stored.version

        async def write_back(value: str) -> WriteBack:
            nonlocal version
            try:
                version = await self.rotate_credential(kind, handle, version, value)
            except (StaleCredentialVersionError, ConnectionNotFoundError):
                logger.warning(
                    "refreshed credential not written back: kind=%s stale",
                    kind.value,
                )
                return WriteBack.STALE
            except Exception as error:
                logger.error(
                    "refreshed credential not written back: kind=%s exception_type=%s",
                    kind.value,
                    log_type_name(error),
                )
                return WriteBack.FAILED
            return WriteBack.WRITTEN

        return stored.secret, write_back

    async def _settle_session(
        self,
        kind: ConnectionKind,
        context: TaskContext,
        admission: Admitted,
        state: "_Session",
    ) -> None:
        """Settle the session's row with the remainder (never raises).

        What the turns counted is read from the row (under its lock), not from
        ``state``: a cancellation between a turn's commit and its bookkeeping
        here must not charge that turn twice."""
        settled = None
        for attempt in range(_SETTLE_ATTEMPTS):
            try:
                settled = await self._store.settle_session(
                    admission.usage_id,
                    context.task_id,
                    state.status,
                    state.failure,
                    state.known_totals,
                    UNKNOWN_SESSION_TOKENS,
                    self._budget,
                )
                break
            except Exception as error:
                # The row and its charge are one transaction: retried as one
                # (an uncertain COMMIT that did go through finds the row
                # closed and writes nothing), never charged separately.
                logger.error(
                    "connection session could not be settled: usage_id=%s"
                    " attempt=%s exception_type=%s",
                    admission.usage_id,
                    attempt + 1,
                    log_type_name(error),
                )
        if settled is not None:
            record, _ = settled
            state.final_input = record.input_tokens or 0
            state.final_output = record.output_tokens or 0
        else:
            # Closed by somebody else (the reaper keeps what the turns
            # counted) or not settled at all (left in flight for the reaper):
            # report what this side knows; nothing is charged outside the
            # settlement's own transaction.
            state.final_input, state.final_output = _session_finals(
                state.counted_input,
                state.counted_output,
                state.known_totals,
                UNKNOWN_SESSION_TOKENS,
            )
        if state.failure is FailureCode.EXPIRED:
            try:
                await self._store_health(
                    kind, admission.secret_handle, ConnectionStatus.EXPIRED
                )
            except Exception as error:
                logger.error(
                    "connection expiry could not be recorded: exception_type=%s",
                    log_type_name(error),
                )

    @staticmethod
    async def _shielded(awaitable) -> None:
        """Run ``awaitable`` to its end even while being cancelled; a
        cancellation is raised once it ended (``_account``'s pattern)."""
        work = asyncio.ensure_future(awaitable)
        cancelled: asyncio.CancelledError | None = None
        while not work.done():
            try:
                await asyncio.wait({work})
            except asyncio.CancelledError as error:
                cancelled = error
        work.result()
        if cancelled is not None:
            raise cancelled

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

    async def _check_lease(
        self, principal: Principal, context: TaskContext, correlation_id: uuid.UUID
    ) -> None:
        """Refuse a call of a worker that no longer holds the task's queue lease
        (issue #153, Decision 0057; the rule of the Tool Broker, Decision 0046).

        Asked for every call, after the checks that refuse on their own and
        before the admission: a refused call writes no usage row and spends no
        quota. ``LOST`` is ``lease_lost``; ``UNKNOWN``, an error, a timeout and an
        answer that is not a ``LeaseStatus`` are ``lease_unavailable`` (the
        exception's text is not logged). A read at one instant: a call that
        passed it is not stopped when the lease is lost while it runs, and its
        settlement asks nothing (the tokens were spent).
        """
        try:
            async with asyncio.timeout(self._lease_timeout):
                status = await self._lease.check(context.task_id, context.lease)
        except Exception as error:
            logger.error(
                "connection lease check failed: exception_type=%s",
                log_type_name(error),
            )
            status = None
        if status is LeaseStatus.HELD:
            return
        reason = (
            RefusalReason.LEASE_LOST
            if status is LeaseStatus.LOST
            else RefusalReason.LEASE_UNAVAILABLE
        )
        await self._refuse(principal, reason, None, correlation_id)

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
            RefusalReason.TASK_TOKEN_FLOOR,
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
