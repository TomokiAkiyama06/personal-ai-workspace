"""Inferred Preference / Confirmation Flow (PAW-044, Decision 0081).

Observation -> Candidate -> Confirmation -> Confirmed (REQUIREMENTS.md "Inferred
Preference / Confirmation Flow", docs/MEMORY_ARCHITECTURE.md section 9):

* ``rules``: the pure decisions: the evidence of a candidate (frequency, scope
  diversity, language strength, consistency, risk), the recommended scope (Repo /
  Project / User), when to ask, and the buttons.
* ``interpretation``: the free-text answer [その他...] and its structured preview
  (a model interpreter behind a port, and a deterministic fallback).
* ``service.PreferenceConfirmationService``: the person's candidates, the preview,
  and the confirmation or rejection, written as memory versions with the rules of
  ``MemoryVersioningService`` (PAW-042). A high-risk preference is never applied
  without an explicit acknowledgement, and no preference changes a permission.

The observations are the Immediate Journal's (PAW-041); the only new table is
``memory_preference_resolutions`` (the answers to held candidates, revision 0192).
"""

from paw_backend.memory.preferences.domain import Resolution
from paw_backend.memory.preferences.errors import (
    PreferenceCandidateChangedError,
    PreferenceHighRiskError,
)
from paw_backend.memory.preferences.interpretation import (
    InterpretedScope,
    Interpreter,
    PreferenceInterpreter,
    PreferencePreview,
    RuleInterpreter,
    Strength,
    StructuredPreference,
    parse_interpreter_output,
)
from paw_backend.memory.preferences.records import (
    CandidateRef,
    Confirmation,
    HeldCandidateRef,
    MemoryCandidateRef,
    PreferenceCandidate,
)
from paw_backend.memory.preferences.rules import (
    CandidateKind,
    Consistency,
    Evidence,
    LanguageStrength,
    Option,
    Recommendation,
    RiskLevel,
    TargetScope,
)
from paw_backend.memory.preferences.service import (
    MEMORY_TYPE,
    POLICY_EFFECT,
    PreferenceConfirmationService,
)

__all__ = [
    "MEMORY_TYPE",
    "POLICY_EFFECT",
    "CandidateKind",
    "CandidateRef",
    "Confirmation",
    "Consistency",
    "Evidence",
    "HeldCandidateRef",
    "InterpretedScope",
    "Interpreter",
    "LanguageStrength",
    "MemoryCandidateRef",
    "Option",
    "PreferenceCandidate",
    "PreferenceCandidateChangedError",
    "PreferenceConfirmationService",
    "PreferenceHighRiskError",
    "PreferenceInterpreter",
    "PreferencePreview",
    "Recommendation",
    "Resolution",
    "RiskLevel",
    "RuleInterpreter",
    "Strength",
    "StructuredPreference",
    "TargetScope",
    "parse_interpreter_output",
]
