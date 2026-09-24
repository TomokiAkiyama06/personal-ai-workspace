"""Credential handles, plaintext detection and redaction.

The design rule is structural: a credential is used through an **opaque
handle** (``cred_`` + 32 hex characters) that only the backend can resolve, and
its plaintext never enters an agent's arguments or results. Detection here is a
second, best-effort net on top of that (it cannot recognise every secret
format, and says so): a tool argument that looks like credential plaintext is
denied, and results are redacted before they are returned or logged.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence

CREDENTIAL_HANDLE_PATTERN = r"cred_[0-9a-f]{32}"
_HANDLE = re.compile(CREDENTIAL_HANDLE_PATTERN)
REDACTED = "[REDACTED]"
UNSUPPORTED = "[UNSUPPORTED]"
_MAX_DEPTH = 32
# Redaction reads a result on the event loop: a longer text is cut (and counted
# as one redaction) so that a huge or adversarial output cannot stall it. The
# patterns above are bounded as well, so their cost is linear in the length.
MAX_TEXT_CHARS = 1_000_000
TRUNCATED = "[TRUNCATED]"

# Formats with a recognisable shape. Used for arguments (deny) and results
# (redact). They are deliberately specific: an agent that writes source code
# or documentation must not be blocked by the word "token".
_HIGH_CONFIDENCE = tuple(
    re.compile(pattern)
    for pattern in (
        # PEM private keys, through the END line when there is one.
        r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----[\s\S]*?"
        r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|\Z)",
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,255}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,255}",
        r"\bsk-[A-Za-z0-9_-]{20,256}",  # OpenAI / Anthropic style keys
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS access key id
        r"\bxox[abprs]-[A-Za-z0-9-]{10,256}",  # Slack
        r"\beyJ[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,2048}",
        r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,2048}",
        # user:password@host in a URL
        r"(?i)\b[a-z][a-z0-9+.-]{0,15}://[^\s/:@]{1,256}:[^\s/@]{1,256}@",
    )
)
# ``password = "..."`` style assignments. Too imprecise to deny an argument
# with (source code assigns test passwords), so results only: a tool output
# that prints one is redacted.
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|authorization)\b[\"']?\s*[:=]\s*[\"']?"
    r"(?!cred_[0-9a-f]{32})[^\s\"',;]{8,}"
)
_SENSITIVE_KEY_SUFFIXES = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "authorization",
)
_SENSITIVE_KEY_SUBSTRINGS = ("credential", "sshkey")


def is_credential_handle(value: object) -> bool:
    """Exactly a backend-issued handle: no case folding, no surrounding text."""
    return isinstance(value, str) and _HANDLE.fullmatch(value) is not None


def _clean(text: str) -> str:
    """The text with format characters removed and compatibility forms folded.

    Zero-width characters and look-alike (full-width) letters inside a token
    would otherwise hide it from the patterns.
    """
    without_format = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", without_format)


def contains_credential_plaintext(text: object) -> bool:
    """Whether ``text`` contains a recognisable credential (high-confidence formats)."""
    if not isinstance(text, str):
        return False
    cleaned = _clean(text)
    return any(pattern.search(cleaned) for pattern in _HIGH_CONFIDENCE)


def redact_text(text: str) -> tuple[str, int]:
    """``text`` with every recognisable credential replaced; and how many were."""
    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]
    cleaned = _clean(text)
    count = 0
    result = cleaned
    for pattern in (*_HIGH_CONFIDENCE, _ASSIGNMENT):
        result, replaced = pattern.subn(REDACTED, result)
        count += replaced
    if truncated:
        return result + TRUNCATED, count + 1
    # Only a text that held a secret is changed (and then in its cleaned form,
    # so that an obfuscated token cannot survive redaction).
    return (result, count) if count else (text, 0)


def _sensitive_key(key: str) -> bool:
    folded = re.sub(r"[^a-z0-9]", "", key.lower())
    return folded.endswith(_SENSITIVE_KEY_SUFFIXES) or any(
        part in folded for part in _SENSITIVE_KEY_SUBSTRINGS
    )


def redact_value(value: object, *, _depth: int = 0) -> tuple[object, int]:
    """A JSON-shaped copy of ``value`` that is safe to return and to log.

    Recognisable credentials in strings are replaced; the value under a
    sensitive-looking key (``password``, ``api_key``, ...) is replaced whatever
    it holds; anything that is not plain JSON data (an arbitrary object, whose
    text form could hold anything) becomes a fixed marker. Returns the copy and
    the number of redactions.
    """
    if _depth > _MAX_DEPTH:
        return REDACTED, 1
    if value is None or isinstance(value, bool | int | float):
        return value, 0
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        total = 0
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                out[UNSUPPORTED] = UNSUPPORTED
                total += 1
                continue
            if (
                _sensitive_key(key)
                and item is not None
                and not isinstance(item, bool)
                and not is_credential_handle(item)
            ):
                out[key] = REDACTED
                total += 1
                continue
            out[key], count = redact_value(item, _depth=_depth + 1)
            total += count
        return out, total
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        total = 0
        items: list[object] = []
        for item in value:
            redacted, count = redact_value(item, _depth=_depth + 1)
            items.append(redacted)
            total += count
        return items, total
    return UNSUPPORTED, 1
