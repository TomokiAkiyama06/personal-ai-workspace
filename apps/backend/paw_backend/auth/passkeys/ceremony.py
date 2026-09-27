"""The WebAuthn ceremonies: build the options, verify the browser's answer.

The ONLY module that imports the WebAuthn library (``webauthn``, py_webauthn; see
Decision 0025 for the choice). It is stateless: the challenge, the stored public
key and the counter come in as arguments, so nothing here touches the database, and
every policy that is not the library's own is written down here in one place.

**Policy** (Decision 0025):

* *User verification is required* (``userVerification: required``; the flag is
  checked on registration and on every assertion): a Passkey is proof of possession
  AND of the user's local verification (biometric / PIN); a stolen, unlocked device
  without it is not enough.
* *Attestation is ``none``*: the server does not ask for, and does not verify, where
  the authenticator came from (no attestation trust store to maintain, no privacy
  cost to the user). A browser answers a ``none`` request with a ``none`` statement
  (the W3C specification requires clients to anonymise it), so anything else is
  refused: it also keeps the X.509 / TPM / Android parsing paths of the library out
  of reach of hostile input.
* *Resident (discoverable) keys are preferred, not required*: the credential is
  looked up by the signed-in user, never by a user name typed at a login screen
  (there is no Passkey-only sign-in), so a device-bound security key that cannot
  store a discoverable credential works too.
* *Algorithms*: ES256, EdDSA and RS256 (the ones every mainstream authenticator
  supports).
* *Origin and RP ID*: the origin in the signed ``clientDataJSON`` must be one of the
  configured origins exactly (scheme, host and port) and the authenticator's RP ID
  hash must be the configured RP ID; a cross-origin frame (``crossOrigin: true``) is
  refused.
* *The signature counter* is NOT judged here: the database compares and stores it in
  one statement (``store.PasskeyRegistry.record_use_in``), which is what makes a replay
  and two concurrent uses of one assertion safe. Here the library's own check is
  switched off by passing 0 as the stored count.

The library's exceptions carry the client's origin and challenge in their text;
they are never logged or returned: a failure is ``CeremonyRejected`` (no message)
and a fixed log line.
"""

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json_dict, parse_cbor
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from paw_backend.auth.passkeys.config import PasskeyConfig
from paw_backend.auth.passkeys.models import (
    PUBLIC_KEY_MAX_BYTES,
    PUBLIC_KEY_MIN_BYTES,
    SIGN_COUNT_MAX,
)
from paw_backend.auth.passkeys.types import AssertionCredential, RegistrationCredential

logger = logging.getLogger(__name__)

SUPPORTED_ALGORITHMS = (
    COSEAlgorithmIdentifier.EDDSA,
    COSEAlgorithmIdentifier.ECDSA_SHA_256,
    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
)
_ZERO_AAGUID = uuid.UUID(int=0)


class CeremonyRejected(Exception):
    """The browser's answer did not verify. Carries nothing about why or about it."""


@dataclass(frozen=True, slots=True)
class VerifiedRegistration:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    aaguid: uuid.UUID | None
    backup_eligible: bool
    backed_up: bool


@dataclass(frozen=True, slots=True)
class VerifiedAssertion:
    sign_count: int
    backup_eligible: bool
    backed_up: bool


def _descriptors(ids: Sequence[bytes]) -> list[PublicKeyCredentialDescriptor]:
    return [PublicKeyCredentialDescriptor(id=credential_id) for credential_id in ids]


def registration_options(
    config: PasskeyConfig,
    *,
    user_id: uuid.UUID,
    login_name: str,
    challenge: bytes,
    exclude: Sequence[bytes],
) -> dict[str, Any]:
    """The JSON options for ``navigator.credentials.create()``.

    The user handle is the account's id (16 bytes; opaque to the authenticator and
    not personal data), so registering twice on one authenticator replaces its
    discoverable credential instead of listing two; the credentials the user has
    already registered are excluded for the same reason.
    """
    options = generate_registration_options(
        rp_id=config.rp_id,
        rp_name=config.rp_name,
        user_name=login_name,
        user_id=user_id.bytes,
        user_display_name=login_name,
        challenge=challenge,
        timeout=config.challenge_ttl_seconds * 1000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_descriptors(exclude),
        supported_pub_key_algs=list(SUPPORTED_ALGORITHMS),
    )
    return options_to_json_dict(options)


def authentication_options(
    config: PasskeyConfig, *, challenge: bytes, allow: Sequence[bytes]
) -> dict[str, Any]:
    """The JSON options for ``navigator.credentials.get()``."""
    options = generate_authentication_options(
        rp_id=config.rp_id,
        challenge=challenge,
        timeout=config.challenge_ttl_seconds * 1000,
        allow_credentials=_descriptors(allow),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json_dict(options)


def _client_data_is_acceptable(raw: bytes, expected_type: str) -> bool:
    """The parts of ``clientDataJSON`` the library does not judge (see the module)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    return (
        type(data) is dict
        and data.get("type") == expected_type
        # A page inside a cross-origin frame must not be able to use the credential.
        and data.get("crossOrigin") in (None, False)
        and "topOrigin" not in data
    )


def verify_registration(
    config: PasskeyConfig, credential: RegistrationCredential, challenge: bytes
) -> VerifiedRegistration:
    """Verify an answer to a registration; ``CeremonyRejected`` if anything is wrong."""
    if not isinstance(credential, RegistrationCredential):
        raise TypeError("credential must be a RegistrationCredential")
    if not _client_data_is_acceptable(credential.client_data_json, "webauthn.create"):
        raise CeremonyRejected
    try:
        # Anything but a "none" statement is refused before the library parses it:
        # exactly the three members of an attestation object, and an EMPTY statement
        # (the library would ignore members of a "none" statement it does not know).
        decoded = parse_cbor(credential.attestation_object)
        if (
            type(decoded) is not dict
            or set(decoded) != {"fmt", "attStmt", "authData"}
            or decoded["fmt"] != "none"
            or decoded["attStmt"] != {}
        ):
            raise CeremonyRejected
        verified = verify_registration_response(
            credential=credential.as_json(),
            expected_challenge=challenge,
            expected_rp_id=config.rp_id,
            expected_origin=list(config.origins),
            require_user_presence=True,
            require_user_verification=True,
            supported_pub_key_algs=list(SUPPORTED_ALGORITHMS),
        )
        aaguid = uuid.UUID(verified.aaguid)
        result = VerifiedRegistration(
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            aaguid=None if aaguid == _ZERO_AAGUID else aaguid,
            backup_eligible=verified.credential_device_type == "multi_device",
            backed_up=bool(verified.credential_backed_up),
        )
    except CeremonyRejected:
        raise
    except Exception:
        # The library's exception text names the client's origin and challenge.
        logger.info("A passkey registration response was rejected")
        raise CeremonyRejected from None
    if (
        # The attested credential id is the one that is stored: it must be the one
        # the client named (and its own bytes, checked once more here).
        result.credential_id != credential.credential_id
        or not PUBLIC_KEY_MIN_BYTES <= len(result.public_key) <= PUBLIC_KEY_MAX_BYTES
        or not 0 <= result.sign_count <= SIGN_COUNT_MAX
        or (result.backed_up and not result.backup_eligible)
    ):
        raise CeremonyRejected
    return result


def verify_assertion(
    config: PasskeyConfig,
    credential: AssertionCredential,
    challenge: bytes,
    *,
    public_key: bytes,
    user_id: uuid.UUID,
) -> VerifiedAssertion:
    """Verify an answer to an authentication; ``CeremonyRejected`` if anything is wrong.

    ``public_key`` is the stored key of the credential the answer names, and
    ``user_id`` the signed-in user: a user handle in the answer (a discoverable
    credential) must be theirs.
    """
    if not isinstance(credential, AssertionCredential):
        raise TypeError("credential must be an AssertionCredential")
    if not _client_data_is_acceptable(credential.client_data_json, "webauthn.get"):
        raise CeremonyRejected
    if credential.user_handle is not None and credential.user_handle != user_id.bytes:
        raise CeremonyRejected
    try:
        verified = verify_authentication_response(
            credential=credential.as_json(),
            expected_challenge=challenge,
            expected_rp_id=config.rp_id,
            expected_origin=list(config.origins),
            credential_public_key=public_key,
            # 0 switches the library's counter comparison off: the database judges
            # the counter atomically with the update (``record_use_in``).
            credential_current_sign_count=0,
            require_user_verification=True,
        )
        result = VerifiedAssertion(
            sign_count=verified.new_sign_count,
            backup_eligible=verified.credential_device_type == "multi_device",
            backed_up=bool(verified.credential_backed_up),
        )
    except Exception:
        logger.info("A passkey assertion was rejected")
        raise CeremonyRejected from None
    if not 0 <= result.sign_count <= SIGN_COUNT_MAX:
        raise CeremonyRejected
    return result
