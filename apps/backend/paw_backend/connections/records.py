"""The values the connection service returns and accepts (PAW-030).

All are frozen dataclasses of plain data. None of them holds a credential or a
credential handle (a handle is read only on the path that runs an adapter and is
never returned), and the only ones that carry content (``ConnectionRequest.prompt``,
``ConnectionResult.text``) keep it out of their ``repr``.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from paw_backend.connections.domain import (
    ConnectionKind,
    ConnectionStatus,
    FailureCode,
    QuotaMetric,
    QuotaPeriod,
    Unlimited,
    UsagePurpose,
    UsageStatus,
)
from paw_backend.connections.limits import (
    DEFAULT_CALL_TIMEOUT_SECONDS,
    MAX_CALL_TIMEOUT_SECONDS,
)
from paw_backend.connections.validation import (
    validate_enum,
    validate_model,
    validate_prompt,
    validate_seconds,
)


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """A configured connection as an Owner / Admin sees it: no credential, no handle."""

    id: uuid.UUID
    kind: ConnectionKind
    status: ConnectionStatus
    enabled: bool
    checked_at: datetime | None  # the last health check that reached a verdict
    created_at: datetime
    updated_at: datetime

    @property
    def available(self) -> bool:
        """Whether a call may start: enabled by the admin and verified connected."""
        return self.enabled and self.status is ConnectionStatus.CONNECTED


@dataclass(frozen=True, slots=True)
class ConnectionAvailability:
    """What a general user sees: ``Claude: Available / Unavailable``, nothing more."""

    kind: ConnectionKind
    available: bool


@dataclass(frozen=True, slots=True)
class Quota:
    """One configured limit: a number or ``UNLIMITED`` for one user, kind, metric
    and period."""

    user_id: uuid.UUID
    kind: ConnectionKind
    metric: QuotaMetric
    period: QuotaPeriod
    limit: int | Unlimited
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    """A quota with what has been used in its current window (``quota_status``).

    ``used`` is in the unit of the metric (``runtime_seconds``: whole seconds,
    rounded down). ``window_end`` is ``None`` for a rolling window.
    """

    kind: ConnectionKind
    metric: QuotaMetric
    period: QuotaPeriod
    limit: int | Unlimited
    used: int
    window_start: datetime
    window_end: datetime | None

    @property
    def reached(self) -> bool:
        return not isinstance(self.limit, Unlimited) and self.used >= self.limit


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One call, attributed to a user and a task. No prompt, no answer, no text."""

    id: uuid.UUID
    user_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    kind: ConnectionKind
    model: str
    purpose: UsagePurpose
    status: UsageStatus
    failure_code: FailureCode | None
    input_tokens: int | None
    output_tokens: int | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class ConnectionRequest:
    """One call a task makes through a shared connection.

    ``purpose`` is the usage category recorded with it (a closed set: never text).
    ``prompt`` is content: it goes to the adapter and nowhere else, and is not part
    of the ``repr``. Both enums accept the member or its exact string.
    """

    model: str
    purpose: UsagePurpose
    prompt: str = field(repr=False)
    timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", validate_model("model", self.model))
        object.__setattr__(
            self, "purpose", validate_enum("purpose", self.purpose, UsagePurpose)
        )
        object.__setattr__(self, "prompt", validate_prompt("prompt", self.prompt))
        object.__setattr__(
            self,
            "timeout_seconds",
            validate_seconds(
                "timeout_seconds", self.timeout_seconds, MAX_CALL_TIMEOUT_SECONDS
            ),
        )


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    """What a finished call gives back. ``text`` is the adapter's answer after the
    credential (exact value and recognisable formats) has been removed from it;
    ``redactions`` counts what was removed. It is content, so not in the ``repr``."""

    usage_id: uuid.UUID
    text: str = field(repr=False)
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    redactions: int = 0
