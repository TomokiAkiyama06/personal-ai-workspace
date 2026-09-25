"""Session ids, and the hashed keys of the throttles and the audit trail.

A session id is 32 random bytes from the operating system's CSPRNG. It is high
entropy, so a plain SHA-256 is enough to store (an offline guess of a 256-bit
value is infeasible, and a salt would only prevent looking a session up by its
hash). The other hashes here are *domain separated* (each starts with its own
label), so the hash of a login name can never be mistaken for the hash of an
address, and none of them can be used as a session id.
"""

import hashlib
import ipaddress
import re
import secrets
import unicodedata
import uuid

from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.limits import (
    LOGIN_NAME_MAX_INPUT,
    SESSION_TOKEN_BYTES,
    SESSION_TOKEN_LENGTH,
)

_SESSION_TOKEN = re.compile(rf"[A-Za-z0-9_-]{{{SESSION_TOKEN_LENGTH}}}")
_UNKNOWN_SOURCE = "unknown"


def new_session_token() -> str:
    """A fresh session id (43 URL-safe characters, 256 bits)."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def parse_session_token(value: object) -> str | None:
    """``value`` if it has the exact shape of a session id, else ``None``.

    Checked before anything else touches the value: a cookie that cannot be a
    session id never causes a database query.
    """
    if isinstance(value, str) and _SESSION_TOKEN.fullmatch(value):
        return value
    return None


def hash_session_token(token: str) -> bytes:
    if not isinstance(token, str) or not token.isascii():
        raise InvalidAuthInputError("token")
    return hashlib.sha256(b"paw.session.v1\0" + token.encode("ascii")).digest()


def _digest(label: str, value: str) -> bytes:
    if not isinstance(value, str):
        raise InvalidAuthInputError("value")
    return hashlib.sha256(f"paw.auth.{label}.v1\0{value}".encode()).digest()


def account_key(login_name: str) -> bytes:
    """The throttle key of a login name (normalised; it need not exist)."""
    return _digest("account", login_name)


def raw_account_name(value: str) -> str:
    """The name a throttle key is made of when the input is not a valid login name.

    An account that does not exist must be throttled exactly like one that
    does, and a string that is not even a well-formed name is also just "a name
    that nobody has". Normalised like a name (NFKC, trimmed, lower case) and
    bounded, so that spellings of the same bad name share one counter.
    """
    return unicodedata.normalize("NFKC", value[:LOGIN_NAME_MAX_INPUT]).strip().lower()


def source_bucket(host: str | None) -> str:
    """The bucket a client address belongs to: an IPv4 address, or an IPv6 /64.

    An IPv6 user owns at least a /64, so counting single addresses would let an
    attacker rotate through millions of them. A value that is not an address (a
    test client's host name, a missing peer) is one bucket of its own.
    """
    if not isinstance(host, str) or not host:
        return _UNKNOWN_SOURCE
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return _UNKNOWN_SOURCE
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


def source_key(bucket: str) -> bytes:
    return _digest("source", bucket)


GLOBAL_KEY = _digest("global", "redeem")


def source_audit_id(bucket: str) -> uuid.UUID:
    """A UUID that stands for a source bucket in the audit trail.

    The audit trail holds ids and enum values only (Decision 0004), so the
    bucket is recorded as a stable, opaque id: the same source always maps to the
    same id (events can be grouped) but the address is not stored. It is a
    pseudonym, not protection: an operator can compute it for an address they
    suspect.
    """
    return uuid.UUID(bytes=_digest("audit-source", bucket)[:16], version=5)
