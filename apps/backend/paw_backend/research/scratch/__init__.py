"""Research Scratch Store: temporary research results with a 24 hour TTL (PAW-050).

Kept apart from Long-term Memory. There is no HTTP surface yet; see
``apps/backend/README.md`` (Research Scratch Store) for the rules.
"""

from paw_backend.research.scratch.errors import (
    InputProblem,
    InvalidScratchInputError,
    ScratchBusyError,
    ScratchError,
    ScratchItemNotFoundError,
    ScratchLeaseLimitError,
    ScratchStateError,
)
from paw_backend.research.scratch.limits import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_PURGE_BATCH_SIZE,
    MAX_ACTIVE_LEASES_PER_ITEM,
    MAX_LEASE_SECONDS,
    MAX_LIST_LIMIT,
    MAX_PURGE_BATCH_SIZE,
    SCRATCH_TTL,
)
from paw_backend.research.scratch.records import (
    DeferralReason,
    Lease,
    PromotionOutcome,
    PromotionState,
    PurgeResult,
    ScratchItem,
)
from paw_backend.research.scratch.service import Clock, ScratchStore

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_PURGE_BATCH_SIZE",
    "MAX_ACTIVE_LEASES_PER_ITEM",
    "MAX_LEASE_SECONDS",
    "MAX_LIST_LIMIT",
    "MAX_PURGE_BATCH_SIZE",
    "SCRATCH_TTL",
    "Clock",
    "DeferralReason",
    "InputProblem",
    "InvalidScratchInputError",
    "Lease",
    "PromotionOutcome",
    "PromotionState",
    "PurgeResult",
    "ScratchBusyError",
    "ScratchError",
    "ScratchItem",
    "ScratchItemNotFoundError",
    "ScratchLeaseLimitError",
    "ScratchStateError",
    "ScratchStore",
]
