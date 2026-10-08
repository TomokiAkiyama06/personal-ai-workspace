"""Typed errors of the Inferred Preference flow (PAW-044).

They extend the Memory versioning errors (``memory/versioning/errors.py``): same
rules, fixed messages from closed vocabularies, never a caller's text.
"""

from paw_backend.memory.versioning.errors import MemoryVersioningError


class PreferenceCandidateChangedError(MemoryVersioningError):
    """The candidate is no longer the one the person answered.

    A memory candidate was confirmed, rejected or replaced in the meantime, or a
    held candidate was answered already or a newer one of the same key arrived.
    Nothing was written; the client reloads the candidates.
    """

    code = "preference_candidate_changed"

    def __init__(self) -> None:
        super().__init__("The candidate changed; reload it")


class PreferenceHighRiskError(MemoryVersioningError):
    """A high-risk preference needs the person's explicit acknowledgement.

    REQUIREMENTS.md: an inference about merge, delete, visibility, ACL / role /
    permission, credentials or sending outside is never applied on a guess; the
    structured interpretation is shown and applied only after an explicit
    confirmation. Nothing was written.
    """

    code = "preference_high_risk_unacknowledged"

    def __init__(self) -> None:
        super().__init__("A high-risk preference must be acknowledged explicitly")
