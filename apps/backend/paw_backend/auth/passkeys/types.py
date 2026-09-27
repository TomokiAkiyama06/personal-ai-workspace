"""The strictly validated shape of what a browser sends in a WebAuthn ceremony.

``navigator.credentials.create()`` / ``get()`` return a ``PublicKeyCredential``;
its JSON form (``toJSON()``) is what the client posts. This module accepts
exactly that shape and nothing else: every value has a type, a character set and a
size bound (in BYTES after decoding, and in characters before decoding, so the
bound is checked before any allocation the input could inflate), and the result is
rebuilt from the values that were checked. A field this module does not read (the
optional ``authenticatorAttachment``, the extension outputs, the transports) is not
passed on: nothing unchecked reaches the WebAuthn library, the database, a log line
or an audit row.

Base64url is the unpadded URL-safe alphabet (RFC 4648 section 5) and must be
canonical: decoding and encoding again gives the same text (no stray trailing
bits, no padding, no whitespace), so a value has exactly one spelling and cannot be
used to slip a second "equal" credential id past a uniqueness check.

Nothing here imports the WebAuthn library or touches the database.
"""

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any

from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.passkeys.models import (
    CREDENTIAL_ID_MAX_BYTES,
    CREDENTIAL_ID_MIN_BYTES,
)

_BASE64URL = re.compile(r"[A-Za-z0-9_-]*")

# Byte bounds of the parts of a response (decoded). The realistic sizes are far
# below: clientDataJSON ~150 B, a "none" attestation object well under 1 KiB, the
# authenticator data 37 B plus a public key, an ES256 signature 72 B (RSA-4096: 512).
CLIENT_DATA_MAX_BYTES = 2_048
ATTESTATION_OBJECT_MAX_BYTES = 4_096
AUTHENTICATOR_DATA_MAX_BYTES = 1_024
SIGNATURE_MAX_BYTES = 1_024
USER_HANDLE_MAX_BYTES = 64  # WebAuthn: a user handle is at most 64 bytes
MAX_CLIENT_EXTENSION_KEYS = 16


def _encoded_length(byte_count: int) -> int:
    return (byte_count * 4 + 2) // 3


def decode_base64url(
    name: str, value: object, *, min_bytes: int, max_bytes: int
) -> bytes:
    """``value`` decoded, if it is a canonical unpadded base64url of an allowed size."""
    if (
        not isinstance(value, str)
        or len(value) > _encoded_length(max_bytes)
        or _BASE64URL.fullmatch(value) is None
        or len(value) % 4 == 1
    ):
        raise InvalidAuthInputError(name)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise InvalidAuthInputError(name) from None
    if not min_bytes <= len(raw) <= max_bytes or encode_base64url(raw) != value:
        raise InvalidAuthInputError(name)
    return raw


def encode_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class RegistrationCredential:
    """A checked answer to ``navigator.credentials.create()``."""

    credential_id: bytes
    client_data_json: bytes = field(repr=False)
    attestation_object: bytes = field(repr=False)

    def as_json(self) -> dict[str, Any]:
        """The dictionary the WebAuthn library is given (rebuilt from checked parts)."""
        credential_id = encode_base64url(self.credential_id)
        return {
            "id": credential_id,
            "rawId": credential_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": encode_base64url(self.client_data_json),
                "attestationObject": encode_base64url(self.attestation_object),
            },
        }


@dataclass(frozen=True, slots=True)
class AssertionCredential:
    """A checked answer to ``navigator.credentials.get()``."""

    credential_id: bytes
    client_data_json: bytes = field(repr=False)
    authenticator_data: bytes = field(repr=False)
    signature: bytes = field(repr=False)
    user_handle: bytes | None = field(default=None, repr=False)

    def as_json(self) -> dict[str, Any]:
        credential_id = encode_base64url(self.credential_id)
        response: dict[str, Any] = {
            "clientDataJSON": encode_base64url(self.client_data_json),
            "authenticatorData": encode_base64url(self.authenticator_data),
            "signature": encode_base64url(self.signature),
        }
        if self.user_handle is not None:
            response["userHandle"] = encode_base64url(self.user_handle)
        return {
            "id": credential_id,
            "rawId": credential_id,
            "type": "public-key",
            "response": response,
        }


def _envelope(value: object) -> tuple[bytes, dict]:
    """The common part: a ``dict`` with matching ``id`` / ``rawId`` and a response."""
    if type(value) is not dict:
        raise InvalidAuthInputError("credential")
    if value.get("type") != "public-key":
        raise InvalidAuthInputError("credential")
    credential_id = decode_base64url(
        "credential",
        value.get("rawId"),
        min_bytes=CREDENTIAL_ID_MIN_BYTES,
        max_bytes=CREDENTIAL_ID_MAX_BYTES,
    )
    # ``id`` is the base64url of ``rawId`` (WebAuthn): the two must agree.
    if value.get("id") != encode_base64url(credential_id):
        raise InvalidAuthInputError("credential")
    extensions = value.get("clientExtensionResults", {})
    if type(extensions) is not dict or len(extensions) > MAX_CLIENT_EXTENSION_KEYS:
        raise InvalidAuthInputError("credential")
    response = value.get("response")
    if type(response) is not dict:
        raise InvalidAuthInputError("credential")
    return credential_id, response


def parse_registration_credential(value: object) -> RegistrationCredential:
    """A registration answer (a client's ``dict``), or ``InvalidAuthInputError``."""
    credential_id, response = _envelope(value)
    return RegistrationCredential(
        credential_id=credential_id,
        client_data_json=decode_base64url(
            "credential",
            response.get("clientDataJSON"),
            min_bytes=2,
            max_bytes=CLIENT_DATA_MAX_BYTES,
        ),
        attestation_object=decode_base64url(
            "credential",
            response.get("attestationObject"),
            min_bytes=2,
            max_bytes=ATTESTATION_OBJECT_MAX_BYTES,
        ),
    )


def parse_assertion_credential(value: object) -> AssertionCredential:
    """An authentication answer as the ``dict`` a client posted."""
    credential_id, response = _envelope(value)
    handle = response.get("userHandle")
    user_handle = (
        None
        if handle in (None, "")
        else decode_base64url(
            "credential", handle, min_bytes=1, max_bytes=USER_HANDLE_MAX_BYTES
        )
    )
    return AssertionCredential(
        credential_id=credential_id,
        client_data_json=decode_base64url(
            "credential",
            response.get("clientDataJSON"),
            min_bytes=2,
            max_bytes=CLIENT_DATA_MAX_BYTES,
        ),
        authenticator_data=decode_base64url(
            "credential",
            response.get("authenticatorData"),
            min_bytes=37,
            max_bytes=AUTHENTICATOR_DATA_MAX_BYTES,
        ),
        signature=decode_base64url(
            "credential",
            response.get("signature"),
            min_bytes=8,
            max_bytes=SIGNATURE_MAX_BYTES,
        ),
        user_handle=user_handle,
    )
