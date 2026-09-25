"""Shared Memory administration (PAW-046).

Read, create, edit, delete and restore Shared Memory, approve or reject Shared
Memory Candidates, and resolve the precedence of the System Security Policy.
There is no HTTP surface yet; see ``apps/backend/README.md`` ("Shared Memory
Administration") and Decision 0009 (approved) for the rules and the choices.
"""

from paw_backend.memory.shared import limits
from paw_backend.memory.shared.audit import COMPLETED_REASON
from paw_backend.memory.shared.errors import (
    AutomaticPromotionRefusedError,
    CandidateLimitError,
    CandidateNotFoundError,
    InputProblem,
    InvalidSharedMemoryInputError,
    PolicySourceError,
    RulesContractError,
    SharedMemoryBusyError,
    SharedMemoryDataError,
    SharedMemoryError,
    SharedMemoryNotFoundError,
    SharedMemoryPermissionError,
    SharedMemoryStateError,
    SharedMemoryVersionConflictError,
    StateProblem,
)
from paw_backend.memory.shared.policy import (
    StaticPolicySource,
    SystemPolicySource,
    load_policies,
)
from paw_backend.memory.shared.records import (
    Actor,
    AgentActor,
    CandidateAction,
    CandidateDecision,
    CandidateProposal,
    CandidateState,
    EditPlan,
    EffectiveSharedMemory,
    InternalEffectiveView,
    OriginScope,
    OverriddenMemory,
    SharedMemory,
    SharedMemoryCandidate,
    SharedMemoryChanges,
    SharedMemoryDraft,
    SharedMemoryStatus,
    SystemPolicyItem,
)
from paw_backend.memory.shared.service import (
    Clock,
    SharedMemoryService,
    memory_lock_key,
    proposer_lock_key,
)

__all__ = [
    "Actor",
    "AgentActor",
    "AutomaticPromotionRefusedError",
    "CandidateAction",
    "CandidateDecision",
    "CandidateLimitError",
    "CandidateNotFoundError",
    "CandidateProposal",
    "CandidateState",
    "Clock",
    "COMPLETED_REASON",
    "EditPlan",
    "EffectiveSharedMemory",
    "InputProblem",
    "InternalEffectiveView",
    "InvalidSharedMemoryInputError",
    "OriginScope",
    "OverriddenMemory",
    "PolicySourceError",
    "RulesContractError",
    "SharedMemory",
    "SharedMemoryBusyError",
    "SharedMemoryCandidate",
    "SharedMemoryChanges",
    "SharedMemoryDataError",
    "SharedMemoryDraft",
    "SharedMemoryError",
    "SharedMemoryNotFoundError",
    "SharedMemoryPermissionError",
    "SharedMemoryService",
    "SharedMemoryStateError",
    "SharedMemoryStatus",
    "SharedMemoryVersionConflictError",
    "StateProblem",
    "StaticPolicySource",
    "SystemPolicyItem",
    "SystemPolicySource",
    "limits",
    "load_policies",
    "memory_lock_key",
    "proposer_lock_key",
]
