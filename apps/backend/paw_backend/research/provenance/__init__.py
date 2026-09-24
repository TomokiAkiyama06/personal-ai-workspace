"""Evidence / Claim Provenance (PAW-052).

Which sources back a claim (with their type and their ``fetched_at`` /
``published_at``), which claims an answer or a task used, and which claims or
sources duplicate or contradict each other. Project scoped; full source content
is never stored. There is no HTTP surface yet; see ``apps/backend/README.md``
(Evidence / Claim Provenance) for the rules.
"""

from paw_backend.research.provenance.errors import (
    ClaimNotFoundError,
    InputProblem,
    InvalidProvenanceInputError,
    ProvenanceBusyError,
    ProvenanceConflictError,
    ProvenanceError,
    ProvenanceLimitError,
    SourceNotFoundError,
)
from paw_backend.research.provenance.limits import (
    DEFAULT_TRACE_LIMIT,
    MAX_CLAIM_TEXT_CHARS,
    MAX_CLAIMS_PER_CALL,
    MAX_SOURCE_TITLE_CHARS,
    MAX_SOURCES_PER_CALL,
    MAX_SOURCES_PER_CLAIM,
    MAX_TRACE_LIMIT,
)
from paw_backend.research.provenance.records import (
    Claim,
    EntityKind,
    RecordedClaim,
    Reference,
    ReferenceKind,
    Relation,
    RelationKind,
    Source,
    SourceInput,
    SourceLink,
    SourceLinkInput,
    Stance,
    Trace,
    TracedClaim,
)
from paw_backend.research.provenance.rules import (
    assemble_traced_claims,
    claim_fingerprint,
    merge_duplicate_sources,
    normalize_claim_text,
    order_links,
    order_pair,
    order_relations,
)
from paw_backend.research.provenance.store import Clock, ProvenanceStore

__all__ = [
    "DEFAULT_TRACE_LIMIT",
    "MAX_CLAIMS_PER_CALL",
    "MAX_CLAIM_TEXT_CHARS",
    "MAX_SOURCES_PER_CALL",
    "MAX_SOURCES_PER_CLAIM",
    "MAX_SOURCE_TITLE_CHARS",
    "MAX_TRACE_LIMIT",
    "Claim",
    "ClaimNotFoundError",
    "Clock",
    "EntityKind",
    "InputProblem",
    "InvalidProvenanceInputError",
    "ProvenanceBusyError",
    "ProvenanceConflictError",
    "ProvenanceError",
    "ProvenanceLimitError",
    "ProvenanceStore",
    "RecordedClaim",
    "Reference",
    "ReferenceKind",
    "Relation",
    "RelationKind",
    "Source",
    "SourceInput",
    "SourceLink",
    "SourceLinkInput",
    "SourceNotFoundError",
    "Stance",
    "Trace",
    "TracedClaim",
    "assemble_traced_claims",
    "claim_fingerprint",
    "merge_duplicate_sources",
    "normalize_claim_text",
    "order_links",
    "order_pair",
    "order_relations",
]
