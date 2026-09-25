"""Typed errors of the shared connection module (PAW-030).

Messages are fixed strings built from closed vocabularies written in this code
base (field names, :class:`InputProblem`, enum values). They never contain
caller-supplied content (a prompt, a model name, an id), a credential, an adapter's
or a driver's message or SQL, so they are safe to log and to map to an API response.
``code`` is the stable machine-readable identifier. Database errors that the
module does not handle (a lost connection, say) propagate unchanged; their text can
contain SQL parameters, so a caller must never show ``str(error)`` to a user.

Existence is not disclosed to users: a task that does not exist and a task that
belongs to somebody else are the same :class:`TaskNotUsableError`, and a connection
that is not configured, disabled, not connected or expired is the same
:class:`ConnectionUnavailableError` (only an Owner / Admin reads the details, with
``ConnectionService.get_connection``).
"""

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from paw_backend.authz.policy import Reason
from paw_backend.connections.domain import (
    ConnectionKind,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    RefusalReason,
)


class InputProblem(StrEnum):
    """Why an argument was rejected. A closed set; never a caller's own text."""

    WRONG_TYPE = "wrong_type"
    NOT_A_UUID = "not_a_uuid"
    NOT_A_STRING = "not_a_string"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    INVALID_CHARACTERS = "invalid_characters"
    NOT_ONE_OF = "not_one_of"
    NOT_A_HANDLE = "not_a_handle"
    NOT_AN_INTEGER = "not_an_integer"
    NOT_A_NUMBER = "not_a_number"
    OUT_OF_RANGE = "out_of_range"
    NOT_A_BOOL = "not_a_bool"
    NOT_CALLABLE = "not_callable"
    NOT_ALLOWED = "not_allowed"  # a test seam passed without its opt-in


class ConnectionsError(Exception):
    """Base class of every error raised by ``paw_backend.connections``."""

    code: ClassVar[str] = "connections_error"


class InvalidConnectionInputError(ConnectionsError, ValueError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_connection_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class ConnectionPermissionDeniedError(ConnectionsError):
    """The Authorizer (or the actor check) denied the action.

    ``reason`` is the stable reason code of the decision
    (``paw_backend.authz.Reason``). The API layer maps ``Reason.AUDIT_UNAVAILABLE``
    to 503 and every other reason to a fixed 403 body that does not say which rule
    applied.
    """

    code = "connection_permission_denied"

    def __init__(self, reason: Reason) -> None:
        self.reason = reason
        super().__init__("Permission denied")


class ConnectionNotFoundError(ConnectionsError):
    """No connection of this kind is configured (an Owner / Admin operation)."""

    code = "connection_not_found"

    def __init__(self) -> None:
        super().__init__("Connection not found")


class ConnectionExistsError(ConnectionsError):
    """A connection of this kind is already configured: replace its credential."""

    code = "connection_exists"

    def __init__(self) -> None:
        super().__init__("A connection of this kind already exists")


class ConnectionUnavailableError(ConnectionsError):
    """The connection cannot be used now (see the module docstring).

    Raised for a user for every reason at once: nothing about the credential, the
    provider or the state of the connection is disclosed.
    """

    code = "connection_unavailable"

    def __init__(self) -> None:
        super().__init__("The connection is unavailable")


class TargetUserNotFoundError(ConnectionsError):
    """A quota was set for a user who does not exist or is deleted."""

    code = "target_user_not_found"

    def __init__(self) -> None:
        super().__init__("User not found")


class QuotaExceededError(ConnectionsError):
    """The user has reached a quota, so a new task may not start (typed refusal).

    ``metric`` and ``period`` name the first quota that was reached (closed enums,
    in declaration order); ``resets_at`` is when a calendar window ends and the
    count starts again (``None`` for a rolling window). A caller may queue the task
    until then or refuse it; an Owner / Admin can raise the limit.
    """

    code = "quota_exceeded"

    def __init__(
        self, metric: QuotaMetric, period: QuotaPeriod, resets_at: datetime | None
    ) -> None:
        self.metric = metric
        self.period = period
        self.resets_at = resets_at
        super().__init__(f"Quota reached: {metric.value} per {period.value}")


class TaskNotUsableError(ConnectionsError):
    """The task cannot use a connection: it does not exist (or is not the user's),
    it has ended, or a Retry / Restart replaced the run of the caller."""

    code = "task_not_usable"

    def __init__(self, reason: RefusalReason) -> None:
        self.reason = reason
        super().__init__(f"The task cannot use a connection: {reason.value}")


class TaskBudgetError(ConnectionsError):
    """The task's own token budget (PAW-033) is exhausted or not configured."""

    code = "task_budget_refused"

    def __init__(self, reason: RefusalReason) -> None:
        self.reason = reason
        super().__init__(f"The task budget refuses the call: {reason.value}")


class ConnectionCallError(ConnectionsError):
    """The adapter call ended without a result. ``failure`` is a closed code.

    The call is recorded (and counted) as failed. Nothing of the adapter's
    exception is kept: not its text, not its name.
    """

    code = "connection_call_failed"

    def __init__(self, failure: FailureCode) -> None:
        self.failure = failure
        super().__init__(f"The call failed: {failure.value}")


class ConnectionBusyError(ConnectionsError):
    """The database did not answer in time (a busy lock, a stalled server).

    Nothing was changed: a write is abandoned before its COMMIT and the server is
    told to give up too. Only when the time ran out during the COMMIT itself is the
    outcome unknown (repeat the read to see it).
    """

    code = "connection_busy"

    def __init__(self) -> None:
        super().__init__("The database did not answer in time")


_ADAPTER_MEMBERS = frozenset({"kind", "check_health", "run"})


class AdapterRegistryError(ConnectionsError):
    """Base class of the errors of ``AdapterRegistry``."""

    code = "adapter_registry_error"


class AdapterInterfaceError(AdapterRegistryError, TypeError):
    """An object does not satisfy the ``ConnectionAdapter`` interface.

    ``member`` is the first failing member (``kind``, ``check_health`` or
    ``run``). The message never contains the object's values.
    """

    code = "adapter_interface"

    def __init__(self, member: str) -> None:
        if member not in _ADAPTER_MEMBERS:
            raise ValueError("member must be one of kind, check_health, run")
        self.member = member
        super().__init__(f"Adapter does not satisfy ConnectionAdapter: {member}")


class DuplicateAdapterError(AdapterRegistryError, ValueError):
    """An adapter for this kind is already registered."""

    code = "duplicate_adapter"

    def __init__(self, kind: ConnectionKind) -> None:
        self.kind = kind
        super().__init__(f"An adapter for {kind.value} is already registered")


class UnknownAdapterError(AdapterRegistryError, LookupError):
    """No adapter is registered for this kind."""

    code = "unknown_adapter"

    def __init__(self, kind: ConnectionKind) -> None:
        self.kind = kind
        super().__init__(f"No adapter is registered for {kind.value}")
