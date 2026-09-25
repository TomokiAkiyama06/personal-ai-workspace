"""Research Privacy Filter and query minimisation (PAW-053).

``PrivacyGate`` turns a draft search query and the classified context it came
from into a ``MinimizedQuery`` that may be sent to an external search, or
refuses; it records every authorised send with an ``ExternalSendAudit`` sink
before anything leaves. ``ResearchBroker`` (PAW-051) accepts a gate as an
optional pre-flight. The sink of the production gate is ``PostgresExternalSendAudit``
(``audit.py``, issue #87: the append-only ``audit_events`` table); build the gate and
the broker with ``build_privacy_gate`` / ``build_research_broker`` (``factory.py``).
No provider and no HTTP endpoint is involved.
"""

from paw_backend.research.privacy.audit import (
    EXTERNAL_SEND_ACTION,
    EXTERNAL_SEND_REASON,
    EXTERNAL_SEND_RESOURCE_KIND,
    PostgresExternalSendAudit,
    external_send_event,
)
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
from paw_backend.research.privacy.factory import (
    build_privacy_gate,
    build_research_broker,
)
from paw_backend.research.privacy.gate import PrivacyGate

__all__ = [
    "COPY_WINDOW_CHARS",
    "DEFAULT_AUDIT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_MEMORY_RECORDS",
    "EXTERNAL_SEND_ACTION",
    "EXTERNAL_SEND_REASON",
    "EXTERNAL_SEND_RESOURCE_KIND",
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
    "PostgresExternalSendAudit",
    "PrivacyGate",
    "PrivacyInput",
    "PrivacyRefusal",
    "RefusalReason",
    "WithheldCounts",
    "build_privacy_gate",
    "build_research_broker",
    "context_pieces_from_items",
    "copy_window",
    "external_send_event",
]
