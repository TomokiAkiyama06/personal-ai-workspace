"""The relying party of the WebAuthn ceremonies, built from the settings.

A ``PasskeyConfig`` exists only when the operator configured ``PAW_PASSKEY_RP_ID``
and ``PAW_PASSKEY_ORIGINS`` (the settings validate them together, see
``paw_backend.config``). Without it the Passkey feature is off and nothing in the
application enforces a Passkey (Decision 0025: an account must never be held in an
enrolment it cannot finish).

* ``rp_id``: the WebAuthn Relying Party ID, a domain the origins belong to. It is
  hashed into every authenticator's response; a credential registered for one RP ID
  can never be used for another (phishing resistance).
* ``origins``: the exact origins (scheme, host and port) a browser may run the
  ceremony from. The origin in the browser's signed ``clientDataJSON`` must be one of
  them; comparing the ``Host`` of the HTTP request would let a request the attacker
  can influence decide what is trusted.
"""

from dataclasses import dataclass

from paw_backend.config import Settings


@dataclass(frozen=True, slots=True)
class PasskeyConfig:
    rp_id: str
    rp_name: str
    origins: tuple[str, ...]
    challenge_ttl_seconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.rp_id, str)
            or not self.rp_id
            or not isinstance(self.rp_name, str)
            or not self.rp_name
        ):
            raise ValueError("rp_id and rp_name must be non-empty strings")
        if (
            not isinstance(self.origins, tuple)
            or not self.origins
            or not all(isinstance(origin, str) and origin for origin in self.origins)
        ):
            raise ValueError("origins must be a non-empty tuple of strings")
        if (
            isinstance(self.challenge_ttl_seconds, bool)
            or not isinstance(self.challenge_ttl_seconds, int)
            or not 30 <= self.challenge_ttl_seconds <= 900
        ):
            raise ValueError("challenge_ttl_seconds must be 30 to 900")

    @classmethod
    def from_settings(cls, settings: Settings) -> "PasskeyConfig | None":
        """The configuration, or ``None`` when Passkeys are not configured."""
        if not isinstance(settings, Settings):
            raise TypeError("settings must be Settings")
        if settings.passkey_rp_id is None:
            return None
        return cls(
            rp_id=settings.passkey_rp_id,
            rp_name=settings.passkey_rp_name,
            origins=tuple(settings.passkey_origins),
            challenge_ttl_seconds=settings.passkey_challenge_ttl_seconds,
        )
