"""The health probe of a CLI connection: its vocabulary and its rules (Decision
0100, approved 2026-10-29; issue #306).

A probe is one short CLI session with a fixed prompt and no tools, run through
the connection's probe adapter (``ConnectionAdapter.check_health``) on the
same container and egress as a node's session (the adapter is a later PR). It
belongs to no task and no user: it is not admitted, not counted into a quota or
a task's budget, and not a ``connection_usage`` row. Each probe is one row of
``connection_probes`` (revision ``0306``), written with its ``connection.probe``
audit event in one transaction BEFORE anything is sent (Decision 0100, 3 and
9: no record, no send).

This module holds what the store and the service share:

* :class:`ProbeTrigger`: why a probe ran (``attach`` / ``replace``: once, right
  after an Admin attached or replaced the credential; ``manual``: the Admin's
  recheck, Decision 0100, 6 and 7);
* :class:`ProbeState` / :class:`ProbeOutcome`: a probe's row
  (``running`` -> ``finished`` with an outcome, or ``abandoned`` by the reaper);
* :class:`ProbeReport`: what a probe adapter may answer besides a bare
  ``ConnectionStatus`` (the outcome, a failure code, the tokens and the model);
* :func:`changes_status`: which outcomes are written to the connection's status
  (Decision 0100, 5: ``busy``, ``cancelled`` and a rate limit say nothing about
  the credential);
* :func:`probe_available_at`: the spacing of manual rechecks (Decision 0100, 8).
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from paw_backend.connections.domain import ConnectionKind, FailureCode, ProbeRefusal
from paw_backend.connections.limits import MAX_TOKENS_PER_CALL
from paw_backend.connections.validation import validate_model, validate_optional_tokens

#: Decision 0100, 9: the audit events of a probe (ids and enums only). The
#: start, written with the probe's row (``allow``, the trigger as the reason)
#: or for a refused recheck (``deny``, the refusal as the reason) ...
ACTION_PROBE: Final = "connection.probe"
#: ... and its end (the system, the outcome as the reason).
ACTION_PROBE_RESULT: Final = "connection.probe_result"
PROBE_RESOURCE_KIND: Final = "connection"

#: Decision 0100, 2: how long one probe may take in all (the container's
#: start, the CLI's start and one short model call). The probe adapter's
#: service is built with it as its ``health_timeout_seconds``.
PROBE_TIMEOUT_SECONDS: Final = 120.0
#: Decision 0100, 8: the spacing of manual rechecks after a probe that found the
#: connection working (and the first step of the back-off after a failure).
PROBE_COOLDOWN_SECONDS: Final = 60
#: ... doubled for every further failure in a row, up to this.
PROBE_MAX_COOLDOWN_SECONDS: Final = 900
#: Decision 0100, 6: a ``running`` probe this old belongs to no live process
#: (the deadline of 120 s plus the worst stop of a unit, about 200 s): the
#: reaper marks it ``abandoned``.
PROBE_ABANDONED_AGE_SECONDS: Final = 600
#: How many finished probes of a credential the spacing looks at (the back-off
#: reaches its cap after five failures; ``busy`` / ``cancelled`` are skipped).
PROBE_HISTORY_ROWS: Final = 32


class ProbeTrigger(StrEnum):
    """Why a probe ran (Decision 0100, 6)."""

    ATTACH = "attach"
    REPLACE = "replace"
    MANUAL = "manual"


class ProbeState(StrEnum):
    """Where a probe's row is."""

    RUNNING = "running"
    FINISHED = "finished"
    ABANDONED = "abandoned"  # the process died: the reaper closed it


class ProbeOutcome(StrEnum):
    """What a finished probe found (Decision 0100, 2, 5 and 10)."""

    CONNECTED = "connected"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"  # Codex: the connection's session slot stayed taken
    CANCELLED = "cancelled"  # the backend stopped before the probe ended


#: The outcomes an adapter may report (``cancelled`` is the service's own).
REPORTABLE_OUTCOMES: Final = frozenset(
    {
        ProbeOutcome.CONNECTED,
        ProbeOutcome.EXPIRED,
        ProbeOutcome.UNAVAILABLE,
        ProbeOutcome.BUSY,
    }
)
#: The outcomes that count as a failure for the back-off of rechecks.
_FAILURES: Final = frozenset({ProbeOutcome.EXPIRED, ProbeOutcome.UNAVAILABLE})
#: The outcomes the back-off skips (they say nothing about the credential).
_NEUTRAL: Final = frozenset({ProbeOutcome.BUSY, ProbeOutcome.CANCELLED})


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """What a probe adapter's ``check_health`` may return instead of a bare
    ``ConnectionStatus``: the outcome, a failure code (only when it is not
    ``connected``), the tokens the CLI reported (``None``: unknown, never
    estimated) and the model it ran on. The answer's text is never part of it."""

    outcome: ProbeOutcome
    failure: FailureCode | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = field(default=None)

    def __post_init__(self) -> None:
        validate_probe_report(self)


def validate_probe_report(report: "ProbeReport") -> None:
    """Check the fields (the class is frozen, but a field can be forced with
    ``object.__setattr__``: the service checks what an adapter returned)."""
    if type(report.outcome) is not ProbeOutcome or (
        report.outcome not in REPORTABLE_OUTCOMES
    ):
        raise ValueError("outcome")
    if report.failure is not None and type(report.failure) is not FailureCode:
        raise ValueError("failure")
    if report.outcome is ProbeOutcome.CONNECTED and report.failure is not None:
        raise ValueError("failure")
    validate_optional_tokens("input_tokens", report.input_tokens, MAX_TOKENS_PER_CALL)
    validate_optional_tokens("output_tokens", report.output_tokens, MAX_TOKENS_PER_CALL)
    if report.model is not None:
        validate_model("model", report.model)


def changes_status(outcome: ProbeOutcome, failure: FailureCode | None) -> bool:
    """Is this outcome written to the connection's status (Decision 0100, 5)?

    ``connected``, ``expired`` and ``unavailable`` are; ``busy`` and
    ``cancelled`` are not, and neither is an ``unavailable`` because of the
    provider's rate limit (a working credential at its window's end)."""
    if outcome in (ProbeOutcome.CONNECTED, ProbeOutcome.EXPIRED):
        return True
    return outcome is ProbeOutcome.UNAVAILABLE and failure is not (
        FailureCode.RATE_LIMITED
    )


def failures_in_a_row(outcomes: Iterable[ProbeOutcome]) -> int:
    """The ``expired`` / ``unavailable`` outcomes since the last ``connected``
    (``outcomes`` newest first; ``busy`` / ``cancelled`` are skipped)."""
    count = 0
    for outcome in outcomes:
        if outcome in _NEUTRAL:
            continue
        if outcome not in _FAILURES:
            break
        count += 1
    return count


def cooldown_seconds(failures: int) -> int:
    """60 s after a success, then 60, 120, 240, 480 and at most 900 s
    (Decision 0100, 8: ``60 * 2^(n - 1)``, capped)."""
    if failures <= 1:
        return PROBE_COOLDOWN_SECONDS
    return min(
        PROBE_MAX_COOLDOWN_SECONDS, PROBE_COOLDOWN_SECONDS * 2 ** min(failures - 1, 16)
    )


def probe_available_at(
    history: Iterable[tuple[ProbeOutcome, datetime]],
) -> datetime | None:
    """When the next manual recheck of a credential may start.

    ``history`` is the credential's FINISHED probes, newest first, as
    ``(outcome, finished_at)``; ``None`` (no probe of it yet: a new
    credential) means now. Counted from the end of the latest probe, whatever
    its outcome."""
    rows = list(history)
    if not rows:
        return None
    failures = failures_in_a_row(outcome for outcome, _ in rows)
    return rows[0][1] + timedelta(seconds=cooldown_seconds(failures))


@dataclass(frozen=True, slots=True)
class ProbeTicket:
    """A probe that was recorded and started: its row's id (also the
    ``correlation_id`` of its audit events), the connection, why it runs and
    when it started (the database's clock). The credential's handle is the
    service's only (not in the ``repr``)."""

    id: uuid.UUID
    connection_id: uuid.UUID
    kind: ConnectionKind
    trigger: ProbeTrigger
    started_at: datetime
    secret_handle: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """How a probe ended: its outcome and failure code, and whether the
    connection's status was written (``False``: the outcome does not change it,
    a newer verdict was recorded after the probe started, or the connection
    points at another credential now)."""

    id: uuid.UUID
    outcome: ProbeOutcome
    failure: FailureCode | None
    status_written: bool


__all__ = [
    "ACTION_PROBE",
    "ACTION_PROBE_RESULT",
    "PROBE_ABANDONED_AGE_SECONDS",
    "PROBE_COOLDOWN_SECONDS",
    "PROBE_HISTORY_ROWS",
    "PROBE_MAX_COOLDOWN_SECONDS",
    "PROBE_RESOURCE_KIND",
    "PROBE_TIMEOUT_SECONDS",
    "REPORTABLE_OUTCOMES",
    "ProbeOutcome",
    "ProbeRefusal",
    "ProbeReport",
    "ProbeResult",
    "ProbeState",
    "ProbeTicket",
    "ProbeTrigger",
    "changes_status",
    "cooldown_seconds",
    "failures_in_a_row",
    "probe_available_at",
    "validate_probe_report",
]
