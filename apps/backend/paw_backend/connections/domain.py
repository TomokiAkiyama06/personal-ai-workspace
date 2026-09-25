"""Closed vocabularies and the pure rules of the shared connection module (PAW-030).

Nothing here does I/O. Everything an operator, a caller or the database can name
is a member of one of these enums, so a value that does not belong to one of them
can never reach a SQL statement, an audit row or a log line.

Connections (``REQUIREMENTS.md`` "Shared Codex / Claude system connection")
--------------------------------------------------------------------------
Codex and Claude are workspace-wide connections: one per :class:`ConnectionKind`,
used by every user through the backend. The credential is never a user's.

Quota
-----
A quota is a limit on one :class:`QuotaMetric` over one :class:`QuotaPeriod` for
one user and one connection kind. The limit is a number or :data:`UNLIMITED`
(explicit: "no row" is not "unlimited", see ``ConnectionService.execute``). The rule
of the module is one comparison, ``used >= limit``: the quota is *reached* when
what was used already equals the limit (a limit of 0 blocks everything). It is
applied to a call that starts a NEW task only (Decision 0016, section 3).

Windows
-------
``rolling_5h`` is the last five hours; ``day``, ``week`` and ``month`` are calendar
periods in the configured time zone (a week starts on Monday), so a calendar window
has a fixed start and a fixed end while a rolling one has neither. The instant is
always the DATABASE's clock (read by the store after the row locks are held); this
module only turns an instant into the start and the end of a window.
"""

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from paw_backend.connections.limits import ROLLING_WINDOW_HOURS


class ConnectionKind(StrEnum):
    """The two shared connections. Declaration order is the canonical order."""

    CODEX = "codex"
    CLAUDE = "claude"


class ConnectionStatus(StrEnum):
    """What the last health check (or a failed call) found out about a credential."""

    CONNECTED = "connected"
    UNAVAILABLE = "unavailable"  # not verified, unreachable or refused for now
    EXPIRED = "expired"  # the provider refused the credential as no longer valid


class UsagePurpose(StrEnum):
    """Why a call was made: a closed usage category, never text (privacy-safe).

    ``REQUIREMENTS.md`` asks for "usage category" analytics without raw chat
    text and defines no list; this set is proposed in Decision 0016.
    """

    CHAT = "chat"
    CODING = "coding"
    REVIEW = "review"
    RESEARCH = "research"
    EVALUATION = "evaluation"
    OTHER = "other"


class UsageStatus(StrEnum):
    """How a usage record ended. ``IN_FLIGHT`` is the only state that changes."""

    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailureCode(StrEnum):
    """Why a call failed. Chosen by the service or raised by an adapter as a member
    (``AdapterFailure``); never derived from exception text."""

    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    EXPIRED = "expired"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    INTERNAL_ERROR = "internal_error"


class QuotaMetric(StrEnum):
    """What a quota counts. Declaration order is the order limits are checked in.

    ``requests``: calls admitted; ``tasks``: distinct tasks that used the
    connection; ``tokens``: input plus output tokens the adapters reported;
    ``runtime_seconds``: whole seconds of call time. The last two are known only
    after a call ends, so they are counted when it has settled.
    """

    REQUESTS = "requests"
    TASKS = "tasks"
    TOKENS = "tokens"
    RUNTIME_SECONDS = "runtime_seconds"


class QuotaPeriod(StrEnum):
    """The window a quota is counted over. Declaration order is the check order."""

    ROLLING_5H = "rolling_5h"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class Unlimited(StrEnum):
    """The explicit "no limit" of a quota (a distinct type: never ``None``, never 0)."""

    UNLIMITED = "unlimited"


UNLIMITED = Unlimited.UNLIMITED


class RefusalReason(StrEnum):
    """Why a use of a connection was refused before it started.

    These are the ``reason`` values of the ``connection.use`` denial in the audit
    log and the ``reason`` of the typed errors: a closed set, never text.
    """

    TASK_NOT_FOUND = "task_not_found"  # unknown, or another user's task
    TASK_ENDED = "task_ended"
    TASK_SUPERSEDED = "task_superseded"
    CONNECTION_UNAVAILABLE = "connection_unavailable"
    QUOTA_NOT_CONFIGURED = "quota_not_configured"
    QUOTA_EXCEEDED = "quota_exceeded"
    TASK_BUDGET_EXCEEDED = "task_budget_exceeded"
    TASK_BUDGET_NOT_CONFIGURED = "task_budget_not_configured"


def window_start(period: QuotaPeriod, now: datetime, zone: tzinfo = UTC) -> datetime:
    """The instant (UTC) the window of ``period`` that contains ``now`` began."""
    _require_aware(now)
    if period is QuotaPeriod.ROLLING_5H:
        return now.astimezone(UTC) - timedelta(hours=ROLLING_WINDOW_HOURS)
    return _midnight(_first_day(period, now.astimezone(zone).date()), zone)


def window_end(
    period: QuotaPeriod, now: datetime, zone: tzinfo = UTC
) -> datetime | None:
    """The instant (UTC) the window that contains ``now`` ends and the count starts
    again; ``None`` for a rolling window (it never resets at one moment)."""
    _require_aware(now)
    if period is QuotaPeriod.ROLLING_5H:
        return None
    first = _first_day(period, now.astimezone(zone).date())
    if period is QuotaPeriod.DAY:
        following = first + timedelta(days=1)
    elif period is QuotaPeriod.WEEK:
        following = first + timedelta(days=7)
    else:
        following = (
            date(first.year + 1, 1, 1)
            if first.month == 12
            else date(first.year, first.month + 1, 1)
        )
    return _midnight(following, zone)


def _require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")


def _first_day(period: QuotaPeriod, day: date) -> date:
    """The first calendar day of the window of ``period`` that holds ``day``."""
    if period is QuotaPeriod.DAY or period is QuotaPeriod.ROLLING_5H:
        return day
    if period is QuotaPeriod.WEEK:
        return day - timedelta(days=day.weekday())  # Monday
    return day.replace(day=1)


def _midnight(day: date, zone: tzinfo) -> datetime:
    """Midnight at the start of ``day`` in ``zone``, as a UTC instant."""
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
