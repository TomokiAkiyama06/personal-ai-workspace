"""Data, limits and the audit boundary of the Research Privacy Filter (PAW-053).

Before a search query leaves the backend, ``PrivacyGate`` (``gate.py``) turns the
caller's draft query and the classified context it came from into a
``MinimizedQuery`` that contains no text of a non-public context piece, no
credential and no identifying detail, or refuses with a ``PrivacyRefusal``. It
also delivers an ``ExternalSendRecord`` to an ``ExternalSendAudit`` sink before
anything is sent. This module has no I/O and no minimisation logic: it holds the
closed enums, the immutable value objects with their validation, the limits and
the in-memory audit sink used by tests (the persistent one is in ``audit.py``).

Wrong types raise ``TypeError`` and out-of-range values raise ``ValueError``;
every message is a fixed string that never echoes a value. ``ContextPiece.text``
is excluded from ``repr`` so that it cannot leak into a log or a traceback.
"""

import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from paw_backend.research.providers.contract import (
    KIND_ORDER,
    ProviderKind,
    ResearchItem,
)

# --- Limits (everything that comes from a caller is bounded) ------------------

# The limits below are counted twice by ``PrivacyGate`` (Decision 0010): on the text
# as written, and on its size after NFKC and full case folding
# (``gate.folded_length``), because those can make a text up to 18 times longer
# (U+FDFA is one code point and 18 after NFKC) and everything the gate does works
# on that form. ``ContextPiece`` can only check the text as written.
#
# A draft query longer than this is not a query but pasted material: refused.
MAX_DRAFT_CHARS = 2_000
# The minimised query is cut to this many characters (ResearchRequest allows 512).
MAX_MINIMIZED_QUERY_CHARS = 256
MAX_CONTEXT_PIECES = 32
MAX_PIECE_CHARS = 200_000
MAX_TOTAL_CONTEXT_CHARS = 400_000
# A stretch of the draft that is at least this long and also occurs in a
# non-public piece is treated as copied from it and removed. Secrets use a
# much shorter window because a secret is short and every part of it counts.
COPY_WINDOW_CHARS = 16
SECRET_WINDOW_CHARS = 4
# Abstraction thresholds (see ``rules.py``).
MIN_ID_DIGITS = 5
MIN_HEX_HASH_CHARS = 12
MIN_OPAQUE_TOKEN_CHARS = 40
# The last label of a host name that marks a private system. Only the last
# label counts: ``corp.example.com`` is public, ``db.corp`` is private.
PRIVATE_HOST_SUFFIXES = frozenset(
    {
        "local",
        "localhost",
        "internal",
        "lan",
        "home",
        "corp",
        "intranet",
        "localdomain",
        "private",
        "arpa",
    }
)
# The audit sink gets this long to record a send; a slower sink refuses the send.
DEFAULT_AUDIT_TIMEOUT_SECONDS = 5.0
MAX_AUDIT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_MEMORY_RECORDS = 1_000
MAX_MEMORY_RECORDS_LIMIT = 100_000
FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ContextLabel(StrEnum):
    """How a piece of context may be used in an external search (closed set).

    Only ``PUBLIC`` text may appear in a query. The caller (backend code that
    knows where the text came from) chooses the label; nothing here, and no LLM,
    infers it from the text.
    """

    PUBLIC = "public"
    PRIVATE_SOURCE = "private_source"  # text of a private repository / file
    PRIVATE_MEMORY = "private_memory"  # Private Memory of a user
    RAW_CONVERSATION = "raw_conversation"  # raw chat text
    SECRET = "secret"  # credential, token, password, key


# The labels whose text must never reach an external search, in enum order.
NON_PUBLIC_LABELS: tuple[ContextLabel, ...] = tuple(
    label for label in ContextLabel if label is not ContextLabel.PUBLIC
)


def copy_window(label: ContextLabel) -> int:
    """The copy-detection window of a non-public label.

    ``SECRET`` gives ``SECRET_WINDOW_CHARS`` (4); the other non-public labels give
    ``COPY_WINDOW_CHARS`` (16). ``PUBLIC`` raises ``ValueError`` (public text is
    never removed); a non-``ContextLabel`` raises ``TypeError``.
    """
    if not isinstance(label, ContextLabel):
        raise TypeError("label must be a ContextLabel")
    if label is ContextLabel.PUBLIC:
        raise ValueError("public text has no copy window")
    return SECRET_WINDOW_CHARS if label is ContextLabel.SECRET else COPY_WINDOW_CHARS


class RefusalReason(StrEnum):
    """Why the gate refused a request (closed set; never derived from content)."""

    # The context is missing, is not a ``PrivacyInput``, or has an element that
    # is not a ``ContextPiece`` (nothing says how it may be used): default deny.
    UNCLASSIFIED_CONTEXT = "unclassified_context"
    # Over ``MAX_DRAFT_CHARS``, as written or after NFKC and full case folding.
    DRAFT_TOO_LONG = "draft_too_long"
    # More than ``MAX_CONTEXT_PIECES`` pieces, or a piece over ``MAX_PIECE_CHARS`` or
    # all of them over ``MAX_TOTAL_CONTEXT_CHARS``, as written or after NFKC and
    # full case folding.
    CONTEXT_TOO_LARGE = "context_too_large"
    # Nothing searchable is left after minimisation.
    EMPTY_QUERY = "empty_query"
    # Safety net: the finished query still holds a credential.
    CREDENTIAL_REMAINS = "credential_remains"
    # Safety net: the finished query still holds a whole non-public piece, or a
    # word (4 or more characters) of a secret.
    PRIVATE_TEXT_REMAINS = "private_text_remains"
    # The audit sink failed, timed out or is full: no record, so no send.
    AUDIT_FAILED = "audit_failed"


class PrivacyRefusal(Exception):
    """The gate refused: nothing was sent. Carries the closed ``reason`` only.

    ``str(error)`` is the reason's value. There is never a query, a piece, a
    fingerprint or an exception text in it.
    """

    def __init__(self, reason: RefusalReason) -> None:
        if not isinstance(reason, RefusalReason):
            raise TypeError("reason must be a RefusalReason")
        self.reason = reason
        super().__init__(reason.value)


class AuditSinkFullError(RuntimeError):
    """``InMemoryExternalSendAudit`` is at its capacity (message never varies)."""

    def __init__(self) -> None:
        super().__init__("the in-memory audit sink is full")


# --- Validation helpers -------------------------------------------------------


def _require_int(value: object, name: str, *, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")


def _require_utc(value: object, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware and in UTC")


def _require_fingerprint(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    if FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be sha256: plus 64 lowercase hex digits")


# --- The classified context the caller supplies --------------------------------


@dataclass(frozen=True, slots=True)
class ContextPiece:
    """One piece of context text with the label that says how it may be used.

    ``label`` must be a ``ContextLabel`` (a plain string is a ``TypeError``: a
    piece cannot exist unclassified). ``text`` must be a ``str`` that is
    UTF-8 encodable, not blank (``strip()`` is not empty) and at most
    ``MAX_PIECE_CHARS`` characters as written (``ValueError`` otherwise). The gate
    counts the size after NFKC and full case folding against the same limit (and
    all pieces against ``MAX_TOTAL_CONTEXT_CHARS``) and refuses a context that is
    larger in that form. ``text`` is excluded from ``repr``.
    """

    label: ContextLabel
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.label, ContextLabel):
            raise TypeError("label must be a ContextLabel")
        if not isinstance(self.text, str):
            raise TypeError("text must be a str")
        if not self.text.strip():
            raise ValueError("text must not be blank")
        if len(self.text) > MAX_PIECE_CHARS:
            raise ValueError("text is too long")
        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("text must be encodable as UTF-8") from None


@dataclass(frozen=True, slots=True)
class PrivacyInput:
    """What a caller hands to ``ResearchBroker.gather(preflight_input=...)``.

    ``context`` is a ``list`` or ``tuple`` (anything else is a ``TypeError``),
    stored as a ``tuple``. Its elements are NOT checked here: an element that is
    not a ``ContextPiece`` is what the gate refuses as unclassified. ``project_id``
    must be a ``uuid.UUID``. An empty ``context`` means "no context".
    """

    context: tuple[Any, ...]
    project_id: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.context, list | tuple):
            raise TypeError("context must be a list or a tuple")
        object.__setattr__(self, "context", tuple(self.context))
        if not isinstance(self.project_id, uuid.UUID):
            raise TypeError("project_id must be a uuid.UUID")


def context_pieces_from_items(
    items: Sequence[ResearchItem],
) -> tuple[ContextPiece, ...]:
    """Label the text of earlier research results so the gate can protect it.

    ``items`` must be a ``list`` or ``tuple`` of ``ResearchItem`` (``TypeError``
    otherwise). For every item, in order: an item whose ``source.private_source``
    is False gives ``ContextPiece(PUBLIC, item.text)``; a private one gives
    ``ContextPiece(PRIVATE_SOURCE, item.text)`` followed, when its title is not
    blank, by ``ContextPiece(PRIVATE_SOURCE, item.source.title)``. An item whose
    text is blank gives no text piece. The locator is not turned into a piece.
    """
    if not isinstance(items, list | tuple):
        raise TypeError("items must be a list or a tuple")
    pieces: list[ContextPiece] = []
    for item in items:
        if not isinstance(item, ResearchItem):
            raise TypeError("items must contain only ResearchItem values")
        private = item.source.private_source
        label = ContextLabel.PRIVATE_SOURCE if private else ContextLabel.PUBLIC
        if item.text.strip():
            pieces.append(ContextPiece(label, item.text))
        if private and item.source.title.strip():
            pieces.append(ContextPiece(label, item.source.title))
    return tuple(pieces)


# --- Counts, the minimised query and the audit record --------------------------


@dataclass(frozen=True, slots=True)
class WithheldCounts:
    """How many context pieces of each non-public label the gate withheld.

    Every non-public piece of the context is withheld from the query by
    construction, so these are the numbers of pieces per label that were in the
    context. Each is an ``int`` (not a bool) between 0 and ``MAX_CONTEXT_PIECES``.
    """

    private_source: int = 0
    private_memory: int = 0
    raw_conversation: int = 0
    secret: int = 0

    def __post_init__(self) -> None:
        for name in ("private_source", "private_memory", "raw_conversation", "secret"):
            _require_int(getattr(self, name), name, low=0, high=MAX_CONTEXT_PIECES)

    @classmethod
    def from_pieces(cls, pieces: Sequence[ContextPiece]) -> "WithheldCounts":
        """Count the non-public pieces of ``pieces`` per label (PUBLIC is ignored)."""
        counts = dict.fromkeys(NON_PUBLIC_LABELS, 0)
        for piece in pieces:
            if not isinstance(piece, ContextPiece):
                raise TypeError("pieces must contain only ContextPiece values")
            if piece.label in counts:
                counts[piece.label] += 1
        return cls(
            private_source=counts[ContextLabel.PRIVATE_SOURCE],
            private_memory=counts[ContextLabel.PRIVATE_MEMORY],
            raw_conversation=counts[ContextLabel.RAW_CONVERSATION],
            secret=counts[ContextLabel.SECRET],
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "private_source": self.private_source,
            "private_memory": self.private_memory,
            "raw_conversation": self.raw_conversation,
            "secret": self.secret,
        }


@dataclass(frozen=True, slots=True)
class MinimizedQuery:
    """A query that may be sent to an external search, and how it was made.

    ``query`` has 1 to ``MAX_MINIMIZED_QUERY_CHARS`` characters, is in normal
    form (no control or format character, no leading, trailing or repeated
    space) and contains at least one word character. ``fingerprint`` is
    ``sha256:`` plus 64 lowercase hex digits (the hash of the UTF-8 ``query``).
    ``truncated`` says whether the query was cut to the length limit.
    ``credentials_removed`` is the number of credentials removed from the draft;
    ``pieces_matched`` the number of non-public pieces from which the draft had
    copied text that was removed; ``abstractions`` the number of details (paths,
    hosts, e-mail addresses, IDs, long numbers, opaque tokens, URLs) removed or
    generalised. ``withheld`` counts the non-public pieces per label. Counts are
    ``int`` values (not bool) of at least 0.
    """

    query: str
    fingerprint: str
    truncated: bool
    credentials_removed: int
    pieces_matched: int
    abstractions: int
    withheld: WithheldCounts

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise TypeError("query must be a str")
        if not 1 <= len(self.query) <= MAX_MINIMIZED_QUERY_CHARS:
            raise ValueError("query has an invalid length")
        if self.query != " ".join(self.query.split()):
            raise ValueError("query must have single spaces and no outer whitespace")
        if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in self.query):
            raise ValueError("query must not contain control or format characters")
        if not any(ch.isalnum() or ch == "_" for ch in self.query):
            raise ValueError("query must contain a word character")
        _require_fingerprint(self.fingerprint, "fingerprint")
        _require_bool(self.truncated, "truncated")
        _require_int(self.credentials_removed, "credentials_removed", low=0, high=10**6)
        _require_int(
            self.pieces_matched, "pieces_matched", low=0, high=MAX_CONTEXT_PIECES
        )
        _require_int(self.abstractions, "abstractions", low=0, high=10**6)
        if not isinstance(self.withheld, WithheldCounts):
            raise TypeError("withheld must be a WithheldCounts")


@dataclass(frozen=True, slots=True)
class ExternalSendRecord:
    """The audit record of one external send. Never holds text.

    It holds the fingerprint of the sent query, its length, the provider kinds
    it may be sent to, the project, the counts of what was removed and when the
    send was authorised. It never holds the query, a removed text, a piece or an
    exception text. ``recorded_at`` is timezone-aware UTC. ``project_id`` is a
    ``uuid.UUID``. ``provider_kinds`` is a non-empty ``tuple`` of distinct
    ``ProviderKind`` in ``KIND_ORDER`` order. ``query_chars`` is 1 to
    ``MAX_MINIMIZED_QUERY_CHARS``.
    """

    recorded_at: datetime
    project_id: uuid.UUID
    query_fingerprint: str
    query_chars: int
    provider_kinds: tuple[ProviderKind, ...]
    withheld: WithheldCounts
    credentials_removed: int
    pieces_matched: int
    abstractions: int
    truncated: bool

    def __post_init__(self) -> None:
        _require_utc(self.recorded_at, "recorded_at")
        if not isinstance(self.project_id, uuid.UUID):
            raise TypeError("project_id must be a uuid.UUID")
        _require_fingerprint(self.query_fingerprint, "query_fingerprint")
        _require_int(
            self.query_chars, "query_chars", low=1, high=MAX_MINIMIZED_QUERY_CHARS
        )
        if not isinstance(self.provider_kinds, tuple):
            raise TypeError("provider_kinds must be a tuple")
        if not self.provider_kinds:
            raise ValueError("provider_kinds must not be empty")
        if not all(isinstance(kind, ProviderKind) for kind in self.provider_kinds):
            raise TypeError("provider_kinds must contain only ProviderKind values")
        positions = [KIND_ORDER[kind] for kind in self.provider_kinds]
        if positions != sorted(set(positions)):
            raise ValueError("provider_kinds must be distinct and in kind order")
        if not isinstance(self.withheld, WithheldCounts):
            raise TypeError("withheld must be a WithheldCounts")
        _require_int(self.credentials_removed, "credentials_removed", low=0, high=10**6)
        _require_int(
            self.pieces_matched, "pieces_matched", low=0, high=MAX_CONTEXT_PIECES
        )
        _require_int(self.abstractions, "abstractions", low=0, high=10**6)
        _require_bool(self.truncated, "truncated")

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready copy: UUID as text, datetime as ISO 8601, enums as values."""
        return {
            "recorded_at": self.recorded_at.isoformat(),
            "project_id": str(self.project_id),
            "query_fingerprint": self.query_fingerprint,
            "query_chars": self.query_chars,
            "provider_kinds": [kind.value for kind in self.provider_kinds],
            "withheld": self.withheld.to_dict(),
            "credentials_removed": self.credentials_removed,
            "pieces_matched": self.pieces_matched,
            "abstractions": self.abstractions,
            "truncated": self.truncated,
        }


# --- The audit boundary ---------------------------------------------------------


@runtime_checkable
class ExternalSendAudit(Protocol):
    """Where the gate records an external send, BEFORE anything is sent.

    ``record`` must durably accept the record (or raise). If it raises or takes
    longer than the gate's audit timeout, the gate refuses the send with
    ``RefusalReason.AUDIT_FAILED``: an unaudited send never happens.
    ``PostgresExternalSendAudit`` (``audit.py``, issue #87) is the persistent
    implementation; ``InMemoryExternalSendAudit`` is the one of the tests.
    """

    async def record(self, record: ExternalSendRecord) -> None: ...


class InMemoryExternalSendAudit:
    """An ``ExternalSendAudit`` that keeps records in memory (tests, demos).

    It holds at most ``max_records`` (an ``int`` from 1 to
    ``MAX_MEMORY_RECORDS_LIMIT``, default ``DEFAULT_MAX_MEMORY_RECORDS``). When
    it is full ``record`` raises ``AuditSinkFullError`` and stores nothing: it
    never drops an old record. A record that is not an ``ExternalSendRecord``
    is a ``TypeError``.
    """

    def __init__(self, max_records: int = DEFAULT_MAX_MEMORY_RECORDS) -> None:
        _require_int(max_records, "max_records", low=1, high=MAX_MEMORY_RECORDS_LIMIT)
        self._max_records = max_records
        self._records: list[ExternalSendRecord] = []

    @property
    def records(self) -> tuple[ExternalSendRecord, ...]:
        return tuple(self._records)

    async def record(self, record: ExternalSendRecord) -> None:
        if not isinstance(record, ExternalSendRecord):
            raise TypeError("record must be an ExternalSendRecord")
        if len(self._records) >= self._max_records:
            raise AuditSinkFullError
        self._records.append(record)
