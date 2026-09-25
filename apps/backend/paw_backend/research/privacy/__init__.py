"""Research Privacy Filter and query minimisation (PAW-053).

``PrivacyGate`` turns a draft search query and the classified context it came
from into a ``MinimizedQuery`` that may be sent to an external search, or
refuses; it records every authorised send with an ``ExternalSendAudit`` sink
before anything leaves. ``ResearchBroker`` (PAW-051) accepts a gate as an
optional pre-flight. No provider, no store and no HTTP endpoint is involved.
"""

from paw_backend.research.privacy.contract import (
    COPY_WINDOW_CHARS,
    DEFAULT_AUDIT_TIMEOUT_SECONDS,
    DEFAULT_MAX_MEMORY_RECORDS,
    MAX_AUDIT_TIMEOUT_SECONDS,
    MAX_CONTEXT_PIECES,
    MAX_DRAFT_CHARS,
    MAX_MEMORY_RECORDS_LIMIT,
    MAX_MINIMIZED_QUERY_CHARS,
    MAX_PIECE_CHARS,
    MAX_TOTAL_CONTEXT_CHARS,
    MIN_HEX_HASH_CHARS,
    MIN_ID_DIGITS,
    MIN_OPAQUE_TOKEN_CHARS,
    NON_PUBLIC_LABELS,
    PRIVATE_HOST_SUFFIXES,
    SECRET_WINDOW_CHARS,
    AuditSinkFullError,
    ContextLabel,
    ContextPiece,
    ExternalSendAudit,
    ExternalSendRecord,
    InMemoryExternalSendAudit,
    MinimizedQuery,
    PrivacyInput,
    PrivacyRefusal,
    RefusalReason,
    WithheldCounts,
    context_pieces_from_items,
    copy_window,
)
from paw_backend.research.privacy.gate import PrivacyGate

__all__ = [
    "COPY_WINDOW_CHARS",
    "DEFAULT_AUDIT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_MEMORY_RECORDS",
    "MAX_AUDIT_TIMEOUT_SECONDS",
    "MAX_CONTEXT_PIECES",
    "MAX_DRAFT_CHARS",
    "MAX_MEMORY_RECORDS_LIMIT",
    "MAX_MINIMIZED_QUERY_CHARS",
    "MAX_PIECE_CHARS",
    "MAX_TOTAL_CONTEXT_CHARS",
    "MIN_HEX_HASH_CHARS",
    "MIN_ID_DIGITS",
    "MIN_OPAQUE_TOKEN_CHARS",
    "NON_PUBLIC_LABELS",
    "PRIVATE_HOST_SUFFIXES",
    "SECRET_WINDOW_CHARS",
    "AuditSinkFullError",
    "ContextLabel",
    "ContextPiece",
    "ExternalSendAudit",
    "ExternalSendRecord",
    "InMemoryExternalSendAudit",
    "MinimizedQuery",
    "PrivacyGate",
    "PrivacyInput",
    "PrivacyRefusal",
    "RefusalReason",
    "WithheldCounts",
    "context_pieces_from_items",
    "copy_window",
]
