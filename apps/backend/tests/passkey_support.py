"""A software WebAuthn authenticator and helpers for the Passkey tests.

No hardware exists in the tests, so this module plays the browser and the
authenticator: it reads the options the server produced (the JSON that
``navigator.credentials.create()`` / ``get()`` would receive) and answers with real
WebAuthn payloads: authenticator data with the RP ID hash, flags and counter, a COSE
public key, a ``none`` attestation object, a ``clientDataJSON``, and a signature made
with a private key (``cryptography``: ES256, EdDSA or RS256).

It is written from the W3C WebAuthn specification and CTAP2 canonical CBOR, NOT from
the library under test: a small CBOR encoder of its own, its own authenticator data
layout. A bug the library shares with its own serialiser therefore cannot make these
tests agree with themselves.

Every knob a hostile or faulty client could turn is a parameter (origin, RP ID,
challenge, flags, counter, attestation format, extra client data fields), so the
tests can build the wrong answer as easily as the right one.
"""

import base64
import hashlib
import json
import os
import struct
import uuid
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

UP = 0x01
UV = 0x04
BE = 0x08
BS = 0x10
AT = 0x40
ED = 0x80

ES256 = -7
EDDSA = -8
RS256 = -257


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# -- CTAP2 canonical CBOR (just what a WebAuthn response needs) -------------------


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([major << 5 | value])
    if value < 256:
        return bytes([major << 5 | 24, value])
    if value < 65536:
        return bytes([major << 5 | 25]) + struct.pack(">H", value)
    return bytes([major << 5 | 26]) + struct.pack(">I", value)


def cbor(value) -> bytes:
    if isinstance(value, bool):
        return b"\xf5" if value else b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, str):
        encoded = value.encode()
        return _head(3, len(encoded)) + encoded
    if isinstance(value, list | tuple):
        return _head(4, len(value)) + b"".join(cbor(item) for item in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(
            cbor(key) + cbor(item) for key, item in value.items()
        )
    raise TypeError(type(value).__name__)


class NotAllowed(Exception):
    """What a browser reports when no authenticator can do what was asked."""


@dataclass
class StoredCredential:
    credential_id: bytes
    private_key: object
    alg: int
    user_handle: bytes
    rp_id: str
    sign_count: int = 0


@dataclass
class SoftwareAuthenticator:
    """One authenticator (a phone, a security key) with any number of credentials."""

    origin: str = "https://paw.example.test"
    rp_id: str = "paw.example.test"
    user_verifies: bool = True
    backup_eligible: bool = False
    backed_up: bool = False
    # A counter that never moves (many platform authenticators): every assertion
    # then reports 0.
    counts: bool = True
    credentials: list = field(default_factory=list)

    # -- registration -----------------------------------------------------------

    def create(
        self,
        options: dict,
        *,
        alg: int | None = None,
        origin: str | None = None,
        rp_id: str | None = None,
        challenge: bytes | None = None,
        flags: int | None = None,
        initial_count: int = 0,
        fmt: str = "none",
        att_stmt: dict | None = None,
        client_data_extra: dict | None = None,
        credential_id: bytes | None = None,
        client_data_type: str = "webauthn.create",
        aaguid: bytes = b"\x00" * 16,
        self_attest: bool = False,
    ) -> dict:
        """The ``RegistrationResponseJSON`` for ``options`` (or a doctored one).

        ``self_attest`` makes a VALID "packed" self-attestation (the credential's own
        key signs the authenticator data and the client data hash): what a real
        authenticator may answer when it ignores a request for ``none``.
        """
        wanted = {p["alg"] for p in options["pubKeyCredParams"]}
        if alg is None:
            alg = next(a for a in (ES256, EDDSA, RS256) if a in wanted)
        if alg not in wanted:
            raise NotAllowed
        excluded = {unb64url(c["id"]) for c in options.get("excludeCredentials", [])}
        if any(c.credential_id in excluded for c in self.credentials):
            raise NotAllowed
        selection = options.get("authenticatorSelection", {})
        if selection.get("userVerification") == "required" and not self.user_verifies:
            raise NotAllowed
        rp = rp_id or options["rp"]["id"]
        private_key, cose = new_key(alg)
        cid = credential_id or os.urandom(32)
        stored = StoredCredential(
            cid, private_key, alg, unb64url(options["user"]["id"]), rp, initial_count
        )
        # Registering again for the same user handle replaces the credential.
        self.credentials = [
            c
            for c in self.credentials
            if not (c.rp_id == rp and c.user_handle == stored.user_handle)
        ]
        self.credentials.append(stored)
        chosen_flags = flags
        if chosen_flags is None:
            chosen_flags = UP | AT
            chosen_flags |= UV if self.user_verifies else 0
            chosen_flags |= BE if self.backup_eligible else 0
            chosen_flags |= BS if self.backed_up else 0
        auth_data = (
            hashlib.sha256(rp.encode()).digest()
            + bytes([chosen_flags])
            + struct.pack(">I", initial_count)
            + aaguid
            + struct.pack(">H", len(cid))
            + cid
            + cbor(cose)
        )
        client_data = {
            "type": client_data_type,
            "challenge": options["challenge"]
            if challenge is None
            else b64url(challenge),
            "origin": origin or self.origin,
            "crossOrigin": False,
        }
        client_data.update(client_data_extra or {})
        client_data_json = json.dumps(client_data, separators=(",", ":")).encode()
        if self_attest:
            fmt = "packed"
            att_stmt = {
                "alg": alg,
                "sig": sign(
                    private_key,
                    alg,
                    auth_data + hashlib.sha256(client_data_json).digest(),
                ),
            }
        attestation = cbor(
            {"fmt": fmt, "attStmt": att_stmt or {}, "authData": auth_data}
        )
        return {
            "id": b64url(cid),
            "rawId": b64url(cid),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data_json),
                "attestationObject": b64url(attestation),
                "transports": ["internal"],
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }

    # -- authentication -----------------------------------------------------------

    def get(
        self,
        options: dict,
        *,
        credential: StoredCredential | None = None,
        origin: str | None = None,
        rp_id: str | None = None,
        challenge: bytes | None = None,
        flags: int | None = None,
        count: int | None = None,
        client_data_extra: dict | None = None,
        client_data_type: str = "webauthn.get",
        user_handle: bytes | None = ...,
        sign_with=None,
    ) -> dict:
        """The ``AuthenticationResponseJSON`` for ``options`` (or a doctored one)."""
        allowed = {unb64url(c["id"]) for c in options.get("allowCredentials", [])}
        rp = options["rpId"]
        candidates = [
            c
            for c in self.credentials
            if c.rp_id == rp and (not allowed or c.credential_id in allowed)
        ]
        chosen = credential or (candidates[0] if candidates else None)
        if chosen is None:
            raise NotAllowed
        if options.get("userVerification") == "required" and not self.user_verifies:
            raise NotAllowed
        if count is None:
            if self.counts:
                chosen.sign_count += 1
            count = chosen.sign_count
        chosen_flags = flags
        if chosen_flags is None:
            chosen_flags = UP | (UV if self.user_verifies else 0)
            chosen_flags |= BE if self.backup_eligible else 0
            chosen_flags |= BS if self.backed_up else 0
        auth_data = (
            hashlib.sha256((rp_id or rp).encode()).digest()
            + bytes([chosen_flags])
            + struct.pack(">I", count)
        )
        client_data = {
            "type": client_data_type,
            "challenge": options["challenge"]
            if challenge is None
            else b64url(challenge),
            "origin": origin or self.origin,
            "crossOrigin": False,
        }
        client_data.update(client_data_extra or {})
        client_data_json = json.dumps(client_data, separators=(",", ":")).encode()
        signed = auth_data + hashlib.sha256(client_data_json).digest()
        signature = sign(sign_with or chosen.private_key, chosen.alg, signed)
        handle = chosen.user_handle if user_handle is ... else user_handle
        response = {
            "clientDataJSON": b64url(client_data_json),
            "authenticatorData": b64url(auth_data),
            "signature": b64url(signature),
        }
        if handle is not None:
            response["userHandle"] = b64url(handle)
        return {
            "id": b64url(chosen.credential_id),
            "rawId": b64url(chosen.credential_id),
            "type": "public-key",
            "response": response,
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }


def new_key(alg: int):
    """A fresh private key and its COSE_Key (RFC 9052 / WebAuthn section 6.5.1.1)."""
    if alg == ES256:
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        return key, {
            1: 2,
            3: ES256,
            -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }
    if alg == EDDSA:
        key = ed25519.Ed25519PrivateKey.generate()
        raw = key.public_key().public_bytes_raw()
        return key, {1: 1, 3: EDDSA, -1: 6, -2: raw}
    if alg == RS256:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        return key, {
            1: 3,
            3: RS256,
            -1: numbers.n.to_bytes(256, "big"),
            -2: numbers.e.to_bytes(3, "big"),
        }
    raise ValueError(alg)


def sign(key, alg: int, message: bytes) -> bytes:
    if alg == ES256:
        return key.sign(message, ec.ECDSA(hashes.SHA256()))
    if alg == EDDSA:
        return key.sign(message)
    if alg == RS256:
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    raise ValueError(alg)


def user_handle_of(user_id: uuid.UUID) -> bytes:
    return user_id.bytes
