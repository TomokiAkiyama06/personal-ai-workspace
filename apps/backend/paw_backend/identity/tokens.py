"""One-time setup / recovery tokens: generation, parsing and verification.

A token looks like ``pawst1.<token id>.<secret>``:

* the *token id* (32 hex digits) is public. It only says which row to check, so
  that attempts can be counted (and locked out) per token;
* the *secret* is 32 random bytes from the operating system's CSPRNG
  (``secrets``), 256 bits, encoded as 43 URL-safe characters.

Only ``HMAC-SHA256(key=salt, message=secret)`` is stored, with a random salt per
token. The secret is high-entropy, so a fast hash is enough (there is nothing to
brute-force, unlike a password); the salt keeps two stored hashes unrelated.
Verification compares digests with ``hmac.compare_digest`` (constant time).

Anything that is not a well-formed token is verified against a fixed dummy
salt / hash, so a malformed or unknown token costs the same as a wrong one.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass, field

TOKEN_PREFIX = "pawst1"
SECRET_BYTES = 32  # 256 bits
SALT_BYTES = 16
_SECRET_CHARS = 43  # base64url of 32 bytes, unpadded
_TOKEN = re.compile(
    rf"{TOKEN_PREFIX}\.([0-9a-f]{{32}})\.([A-Za-z0-9_-]{{{_SECRET_CHARS}}})"
)
TOKEN_LENGTH = len(TOKEN_PREFIX) + 1 + 32 + 1 + _SECRET_CHARS


def hash_secret(salt: bytes, secret: str) -> bytes:
    """The stored form of ``secret``: HMAC-SHA256 keyed with the token's salt."""
    return hmac.new(salt, secret.encode("ascii"), hashlib.sha256).digest()


@dataclass(frozen=True, slots=True)
class NewToken:
    """A freshly generated token. ``token`` exists only here, never in the database."""

    token: str = field(repr=False)
    token_id: uuid.UUID
    salt: bytes = field(repr=False)
    secret_hash: bytes = field(repr=False)


def generate() -> NewToken:
    token_id = uuid.uuid4()
    secret = secrets.token_urlsafe(SECRET_BYTES)
    salt = secrets.token_bytes(SALT_BYTES)
    return NewToken(
        token=f"{TOKEN_PREFIX}.{token_id.hex}.{secret}",
        token_id=token_id,
        salt=salt,
        secret_hash=hash_secret(salt, secret),
    )


@dataclass(frozen=True, slots=True)
class ParsedToken:
    token_id: uuid.UUID
    secret: str = field(repr=False)


def parse(token: object) -> ParsedToken | None:
    """The parts of a well-formed token, or ``None`` for anything else."""
    if not isinstance(token, str) or len(token) != TOKEN_LENGTH:
        return None
    match = _TOKEN.fullmatch(token)
    if match is None:
        return None
    return ParsedToken(uuid.UUID(hex=match.group(1)), match.group(2))


# What a token that names no row is checked against.
_DUMMY_SALT = bytes(SALT_BYTES)
_DUMMY_SECRET = "A" * _SECRET_CHARS
_DUMMY_HASH = hash_secret(_DUMMY_SALT, _DUMMY_SECRET)


def verify(
    parsed: ParsedToken | None, salt: bytes | None, secret_hash: bytes | None
) -> bool:
    """Whether ``parsed`` matches the stored ``salt`` / ``secret_hash``.

    The same amount of work is done whether the token is well-formed and names
    an existing row or not: the missing parts are replaced by fixed dummy
    values, and the (never successful) dummy comparison is still made.
    """
    if parsed is None or salt is None or secret_hash is None:
        candidate = hash_secret(
            _DUMMY_SALT, parsed.secret if parsed is not None else _DUMMY_SECRET
        )
        hmac.compare_digest(candidate, _DUMMY_HASH)
        return False
    return hmac.compare_digest(hash_secret(salt, parsed.secret), secret_hash)
