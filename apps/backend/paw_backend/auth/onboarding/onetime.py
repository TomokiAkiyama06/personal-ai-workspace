"""One-time tokens of invitations and device pairing: generation, parsing, checks.

The same construction as the Owner's setup token (``paw_backend.identity.tokens``,
Decision 0005), with a prefix of each kind so that one kind can never be handed in
as another:

* ``pawiv1.<id>.<secret>``: an invitation token;
* ``pawpr1.<id>.<secret>``: a pairing token (the QR code / link);
* ``pawpc1.<id>.<secret>``: a new device's claim while it waits for an approval.

``id`` (32 hex digits) is the row's public lookup key; ``secret`` is 32 bytes of the
operating system's CSPRNG (256 bits, 43 URL-safe characters). Only
``HMAC-SHA256(key=salt, message=secret)`` is stored, with a random salt per token,
and compared in constant time. Anything that is not a well-formed token is checked
against a fixed dummy salt / hash, so a malformed or unknown token costs the same
as a wrong one.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass, field

from paw_backend.auth.onboarding.models import HASH_BYTES, SALT_BYTES

SECRET_BYTES = 32
_SECRET_CHARS = 43  # base64url of 32 bytes, unpadded


def hash_secret(salt: bytes, secret: str) -> bytes:
    return hmac.new(salt, secret.encode("ascii"), hashlib.sha256).digest()


@dataclass(frozen=True, slots=True)
class NewToken:
    """A freshly generated token. ``token`` exists only here, never in the database."""

    token: str = field(repr=False)
    token_id: uuid.UUID
    salt: bytes = field(repr=False)
    secret_hash: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ParsedToken:
    token_id: uuid.UUID
    secret: str = field(repr=False)


_DUMMY_SALT = bytes(SALT_BYTES)
_DUMMY_SECRET = "A" * _SECRET_CHARS
_DUMMY_HASH = hash_secret(_DUMMY_SALT, _DUMMY_SECRET)


class TokenKind:
    """One kind of one-time token (its prefix)."""

    def __init__(self, prefix: str) -> None:
        if re.fullmatch(r"paw[a-z]{2}[0-9]", prefix) is None:
            raise ValueError("prefix must look like pawxx1")
        self.prefix = prefix
        self.length = len(prefix) + 1 + 32 + 1 + _SECRET_CHARS
        self._pattern = re.compile(
            rf"{prefix}\.([0-9a-f]{{32}})\.([A-Za-z0-9_-]{{{_SECRET_CHARS}}})"
        )

    def generate(self, token_id: uuid.UUID | None = None) -> NewToken:
        """A new token; ``token_id`` reuses a row's id (a claim of a pairing)."""
        token_id = token_id or uuid.uuid4()
        secret = secrets.token_urlsafe(SECRET_BYTES)
        salt = secrets.token_bytes(SALT_BYTES)
        return NewToken(
            token=f"{self.prefix}.{token_id.hex}.{secret}",
            token_id=token_id,
            salt=salt,
            secret_hash=hash_secret(salt, secret),
        )

    def parse(self, value: object) -> ParsedToken | None:
        """The parts of a well-formed token of this kind, or ``None``."""
        if not isinstance(value, str) or len(value) != self.length:
            return None
        match = self._pattern.fullmatch(value)
        if match is None:
            return None
        return ParsedToken(uuid.UUID(hex=match.group(1)), match.group(2))


def verify(
    parsed: ParsedToken | None, salt: bytes | None, secret_hash: bytes | None
) -> bool:
    """Whether ``parsed`` matches the stored salt / hash (the same work either way)."""
    if (
        parsed is None
        or not isinstance(salt, bytes)
        or not isinstance(secret_hash, bytes)
        or len(secret_hash) != HASH_BYTES
    ):
        candidate = hash_secret(
            _DUMMY_SALT, parsed.secret if parsed is not None else _DUMMY_SECRET
        )
        hmac.compare_digest(candidate, _DUMMY_HASH)
        return False
    return hmac.compare_digest(hash_secret(salt, parsed.secret), secret_hash)


INVITATION = TokenKind("pawiv1")
PAIRING = TokenKind("pawpr1")
CLAIM = TokenKind("pawpc1")
