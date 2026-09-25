"""The persistent audit of external research sends (issue #87, Decision 0010).

``PrivacyGate`` (``gate.py``) hands an ``ExternalSendRecord`` to an
``ExternalSendAudit`` BEFORE anything leaves the backend and refuses the send when
the record is not accepted. ``InMemoryExternalSendAudit`` (``contract.py``) is the
sink of the tests. ``PostgresExternalSendAudit`` is the production sink: it appends
one row to the append-only ``audit_events`` table (PAW-025) per authorised send.

The row (``external_send_event``):

* ``action`` ``research.external_send``, ``resource_kind`` ``research_query``,
  ``project_id`` the project, ``actor_role`` ``system`` (the backend decides; the
  record has no user), ``decision`` ``allow``, ``reason`` ``send_authorized``,
  ``occurred_at`` the time of the decision (the gate's clock), ``correlation_id``
  and ``id`` fresh UUIDs. ``recorded_at`` is the database's clock (a trigger).
* ``details`` (a JSON object, migration ``0087``): ``query_fingerprint`` (the
  ``sha256:`` hash of the minimised query), ``query_chars``, ``provider_kinds``,
  ``withheld`` (pieces per label), ``credentials_removed``, ``pieces_matched``,
  ``abstractions`` and ``truncated``.

**Never the query, a removed text or a context piece.** The record has no field for
them; this module copies only fields it has checked to be exactly the expected type
and shape (a record that was tampered with after its own validation, or built by a
subclass, is refused: ``TypeError`` / ``ValueError`` with a fixed message, before
the database is touched); and the database refuses a row of this action whose
``details`` has another key or a value of another shape
(``ck_audit_events_external_send_details``), so a bug here still cannot store text.

Fail closed. The write is a single statement on a dedicated connection that is
aborted at the deadline (``Database.execute_abortable``: the same path as the
Authorizer's ``PostgresAuditSink``, and the same accounting of the connection
slots, at most ``PAW_DATABASE_POOL_SIZE`` at once). Waiting for a free slot and
running the statement share ONE deadline. Any failure (a stalled or unreachable
server, a refused privilege, a full slot pool) is an exception, which the gate turns
into ``PrivacyRefusal(AUDIT_FAILED)``: nothing is sent. An aborted write may or may
not have been committed, so a refused send can leave a row behind (the record then
says "authorised" for a query that was not sent); the reverse never happens: a
sent query always has its row.
"""

import uuid
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from paw_backend.authz.audit import AuditEvent
from paw_backend.authz.models import (
    EXTERNAL_SEND_ACTION,
    EXTERNAL_SEND_DETAILS_KEYS,
    EXTERNAL_SEND_REASON,
    EXTERNAL_SEND_WITHHELD_KEYS,
)
from paw_backend.authz.roles import SystemRole
from paw_backend.db import Database
from paw_backend.research.privacy.contract import (
    DEFAULT_AUDIT_TIMEOUT_SECONDS,
    FINGERPRINT_PATTERN,
    MAX_AUDIT_TIMEOUT_SECONDS,
    MAX_CONTEXT_PIECES,
    MAX_MINIMIZED_QUERY_CHARS,
    ExternalSendRecord,
    WithheldCounts,
)
from paw_backend.research.providers.contract import KIND_ORDER, ProviderKind

__all__ = [
    "EXTERNAL_SEND_ACTION",
    "EXTERNAL_SEND_REASON",
    "EXTERNAL_SEND_RESOURCE_KIND",
    "PostgresExternalSendAudit",
    "external_send_event",
]

EXTERNAL_SEND_RESOURCE_KIND = "research_query"

# The largest a removal count may be (``MinimizedQuery`` allows ``10**6``).
_MAX_COUNT = 10**6

# The columns of the row: the fields of ``AuditEvent`` (``event_id`` is the column
# ``id``) and ``details``. ``recorded_at`` is set by the database.
_COLUMNS = (
    *("id" if name == "event_id" else name for name in AuditEvent.model_fields),
    "details",
)
_INSERT = (
    f"INSERT INTO audit_events ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(f'%({column})s' for column in _COLUMNS)})"
)


def _exact_int(value: object, name: str, *, low: int, high: int) -> int:
    """``value`` if it is an ``int`` (exactly: no ``bool``, no subclass) in range."""
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    if not low <= value <= high:
        raise ValueError(f"{name} is out of range")
    return value


# The fields of a record, read once and by name (``record`` may be a subclass with
# more attributes: those are never read).
_RECORD_FIELDS = tuple(field.name for field in fields(ExternalSendRecord))


def _read_fields(record: ExternalSendRecord) -> dict[str, Any]:
    """Every field of ``record``; ``TypeError`` if a slot was never set."""
    try:
        return {name: getattr(record, name) for name in _RECORD_FIELDS}
    except AttributeError:
        raise TypeError("record is incomplete") from None


def _checked_details(read: dict[str, Any]) -> dict[str, Any]:
    """The ``details`` of ``record``, built from values checked here, again.

    ``ExternalSendRecord`` validates itself when it is built, but a frozen dataclass
    can be changed afterwards (``object.__setattr__``), and a ``str`` or ``int``
    subclass can pass its ``isinstance`` check and still hold something else. So each
    field is checked to be exactly the expected type and shape, and the result is
    made only of new plain ``str`` / ``int`` / ``bool`` / ``list`` / ``dict`` values.
    """
    fingerprint = read["query_fingerprint"]
    if type(fingerprint) is not str:
        raise TypeError("query_fingerprint must be a str")
    if FINGERPRINT_PATTERN.fullmatch(fingerprint) is None:
        raise ValueError("query_fingerprint has an invalid shape")
    kinds = read["provider_kinds"]
    if type(kinds) is not tuple:
        raise TypeError("provider_kinds must be a tuple")
    if not 1 <= len(kinds) <= len(ProviderKind):
        raise ValueError("provider_kinds has an invalid length")
    if any(type(kind) is not ProviderKind for kind in kinds):
        raise TypeError("provider_kinds must contain only ProviderKind values")
    positions = [KIND_ORDER[kind] for kind in kinds]
    if positions != sorted(set(positions)):
        raise ValueError("provider_kinds must be distinct and in kind order")
    withheld = read["withheld"]
    if type(withheld) is not WithheldCounts:
        raise TypeError("withheld must be a WithheldCounts")
    if type(read["truncated"]) is not bool:
        raise TypeError("truncated must be a bool")
    try:
        withheld_counts = {
            name: getattr(withheld, name) for name in EXTERNAL_SEND_WITHHELD_KEYS
        }
    except AttributeError:
        raise TypeError("withheld is incomplete") from None
    counts = {
        name: _exact_int(value, name, low=0, high=MAX_CONTEXT_PIECES)
        for name, value in withheld_counts.items()
    }
    details: dict[str, Any] = {
        "query_fingerprint": fingerprint,
        "query_chars": _exact_int(
            read["query_chars"], "query_chars", low=1, high=MAX_MINIMIZED_QUERY_CHARS
        ),
        "provider_kinds": [kind.value for kind in kinds],
        "withheld": counts,
        "credentials_removed": _exact_int(
            read["credentials_removed"], "credentials_removed", low=0, high=_MAX_COUNT
        ),
        "pieces_matched": _exact_int(
            read["pieces_matched"], "pieces_matched", low=0, high=MAX_CONTEXT_PIECES
        ),
        "abstractions": _exact_int(
            read["abstractions"], "abstractions", low=0, high=_MAX_COUNT
        ),
        "truncated": read["truncated"],
    }
    if tuple(details) != EXTERNAL_SEND_DETAILS_KEYS:  # a bug here, not the caller's
        raise RuntimeError("the audit details do not match the schema")
    return details


def _checked_time(value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("recorded_at must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError("recorded_at must be timezone-aware and in UTC")
    return value.astimezone(UTC)


def external_send_event(
    record: ExternalSendRecord,
    *,
    event_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
) -> tuple[AuditEvent, dict[str, Any]]:
    """The audit event of ``record`` and its ``details``; no I/O.

    ``record`` must be an ``ExternalSendRecord`` (``TypeError`` otherwise) whose every
    field is exactly the expected type and shape (``TypeError`` / ``ValueError``; a
    fixed message that names the field, never a value). ``event_id`` and
    ``correlation_id`` default to fresh UUIDs (``uuid.UUID`` if given). The event holds
    no text: ``AuditEvent`` has no field that could; the details are described in
    the module docstring.
    """
    if not isinstance(record, ExternalSendRecord):
        raise TypeError("record must be an ExternalSendRecord")
    for name, value in (("event_id", event_id), ("correlation_id", correlation_id)):
        if value is not None and type(value) is not uuid.UUID:
            raise TypeError(f"{name} must be a uuid.UUID")
    read = _read_fields(record)
    project_id = read["project_id"]
    if type(project_id) is not uuid.UUID:
        raise TypeError("project_id must be a uuid.UUID")
    details = _checked_details(read)
    event = AuditEvent(
        event_id=event_id or uuid.uuid4(),
        correlation_id=correlation_id or uuid.uuid4(),
        occurred_at=_checked_time(read["recorded_at"]),
        actor_role=SystemRole.SYSTEM.value,
        action=EXTERNAL_SEND_ACTION,
        resource_kind=EXTERNAL_SEND_RESOURCE_KIND,
        project_id=project_id,
        decision="allow",
        reason=EXTERNAL_SEND_REASON,
    )
    return event, details


class PostgresExternalSendAudit:
    """An ``ExternalSendAudit`` that appends the record to ``audit_events``.

    ``database`` must be a ``Database`` (``TypeError``); ``timeout_seconds`` (a number
    greater than 0 and at most ``MAX_AUDIT_TIMEOUT_SECONDS``, default
    ``DEFAULT_AUDIT_TIMEOUT_SECONDS``, the gate's own default) is the one deadline
    of a write, including the wait for a free connection slot. Give the gate the same
    value (``build_privacy_gate`` does).

    ``record`` validates the record first (see ``external_send_event``: nothing
    reaches the database that is not checked), then writes one row on a dedicated,
    abortable connection. It raises whenever the row was not confirmed in time.
    """

    def __init__(
        self,
        database: Database,
        *,
        timeout_seconds: float = DEFAULT_AUDIT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, int | float
        ):
            raise TypeError("timeout_seconds must be a number")
        # ``nan`` and ``inf`` fail the comparison.
        if not 0 < timeout_seconds <= MAX_AUDIT_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds is out of range")
        self._database = database
        self._timeout = float(timeout_seconds)

    async def record(self, record: ExternalSendRecord) -> None:
        event, details = external_send_event(record)
        values = event.model_dump()
        values["id"] = values.pop("event_id")
        values["details"] = Jsonb(details)
        await self._database.execute_abortable(
            _INSERT, values, timeout_seconds=self._timeout
        )
