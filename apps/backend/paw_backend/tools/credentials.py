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
TRUNCATED = "[TRUNCATED]"
_MAX_DEPTH = 32
# Redaction reads a result on the event loop, so its work is bounded: a text
# longer than ``MAX_TEXT_CHARS`` is cut (and counted as one redaction), and one
# result may hold at most ``MAX_RESULT_NODES`` values and ``MAX_RESULT_CHARS``
# characters in all; the rest is dropped and replaced by one marker. The
# patterns are bounded as well, so their cost is linear in the length.
MAX_TEXT_CHARS = 1_000_000
MAX_RESULT_NODES = 100_000
MAX_RESULT_CHARS = 4_000_000

# A token may be glued to a name (``MYTOKEN_ghp_...``, ``OPENAI_API_KEY_sk-...``,
# ``key_AKIA...``), so the formats below are anchored on "not preceded by a
# letter or digit" (an underscore or hyphen before them is fine), not on ``\b``
# (an underscore is a word character, which hid exactly those spellings).
_B = r"(?<![A-Za-z0-9])"

# Formats with a recognisable shape. Used for arguments (deny) and results
# (redact). They are deliberately specific: an agent that writes source code
# or documentation must not be blocked by the word "token".
_HIGH_CONFIDENCE = tuple(
    re.compile(pattern)
    for pattern in (
        # PEM / PGP private keys, through the END line when there is one.
        r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
        r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----|\Z)",
        _B + r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,255}",  # GitHub
        _B + r"github_pat_[A-Za-z0-9_]{20,255}",
        _B + r"glpat-[A-Za-z0-9_-]{20,255}",  # GitLab
        _B + r"sk-[A-Za-z0-9_-]{20,256}",  # OpenAI / Anthropic style keys
        _B + r"(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,255}",  # Stripe
        _B + r"(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}(?![A-Za-z0-9])",  # AWS
        _B + r"AIza[0-9A-Za-z_-]{35}",  # Google API key
        _B + r"ya29\.[0-9A-Za-z_-]{20,512}",  # Google OAuth access token
        _B + r"xox[abprs]-[A-Za-z0-9-]{10,256}",  # Slack token
        r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,200}",  # Slack webhook
        _B + r"npm_[A-Za-z0-9]{36}",
        _B + r"pypi-[A-Za-z0-9_-]{50,255}",
        _B + r"hf_[A-Za-z0-9]{30,255}",  # Hugging Face
        _B + r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}",  # SendGrid
        _B + r"dckr_pat_[A-Za-z0-9_-]{20,255}",  # Docker
        _B + r"dop_v1_[a-f0-9]{64}",  # DigitalOcean
        _B + r"eyJ[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,2048}",
        r"(?i)" + _B + r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,2048}",
        # user:password@host in a URL
        r"(?i)" + _B + r"[a-z][a-z0-9+.-]{0,15}://[^\s/:@]{1,256}:[^\s/@]{1,256}@",
    )
)
# ``password = "..."`` style assignments: env files (``DB_PASSWORD=...``,
# ``AWS_SECRET_ACCESS_KEY=...``), JSON (``"db_password": "..."``), YAML, ini.
# A name that *contains* one of the words (a prefix or a suffix on it) counts.
# Too imprecise to deny an argument with (source code assigns test passwords),
# so results only: a tool output that prints one is redacted. The value is
# any quoted text, or an unquoted word of at least 6 characters; a handle is
# an opaque id, not a secret.
_KEYWORDS = (
    r"pass(?:word|wd|phrase)|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|credential|account[_-]?key|"
    r"shared[_-]?access[_-]?(?:key|signature)"
)
_ASSIGNMENT = re.compile(
    r"(?i)(?P<head>(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{0,64}?"
    rf"(?:{_KEYWORDS})[A-Za-z0-9_-]{{0,32}}[\"']?\s*[:=]\s*)"
    r"(?![\"']?cred_[0-9a-f]{32})"
    r"(?:\"[^\"\r\n]{1,512}\"|'[^'\r\n]{1,512}'|[^\s\"',;]{6,})"
)
# ``--password hunter2`` (a command line, the value in the next word).
_FLAG = re.compile(
    r"(?i)(?P<head>(?<![A-Za-z0-9])--[a-z0-9-]{0,32}"
    r"(?:password|passwd|token|secret|api-?key)[a-z0-9-]{0,16}\s+)(?!-)[^\s]{6,}"
)
# Words that make the value under a dict key secret. The key is folded to
# lower-case letters and digits first, so ``DB_PASSWORD`` and ``dbPassword``
# match alike. A number or a bool under such a key (``max_tokens``) is data.
_SENSITIVE_KEY_WORDS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "accesskey",
    "accountkey",
    "sharedaccess",
    "authorization",
    "credential",
    "sshkey",
)


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
    for pattern in (*_HIGH_CONFIDENCE,):
        result, replaced = pattern.subn(REDACTED, result)
        count += replaced
    # The name stays (``DB_PASSWORD=[REDACTED]``): only the value goes.
    for pattern in (_ASSIGNMENT, _FLAG):
        result, replaced = pattern.subn(rf"\g<head>{REDACTED}", result)
        count += replaced
    if truncated:
        return result + TRUNCATED, count + 1
    # Only a text that held a secret is changed (and then in its cleaned form,
    # so that an obfuscated token cannot survive redaction).
    return (result, count) if count else (text, 0)


def _sensitive_key(key: str) -> bool:
    folded = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(word in folded for word in _SENSITIVE_KEY_WORDS)


class _Budget:
    """How much of one result may still be read."""

    __slots__ = ("chars", "nodes")

    def __init__(self) -> None:
        self.nodes = MAX_RESULT_NODES
        self.chars = MAX_RESULT_CHARS


def redact_value(value: object) -> tuple[object, int]:
    """A JSON-shaped copy of ``value`` that is safe to return and to log.

    Recognisable credentials in strings and in dict **keys** are replaced; the
    value under a sensitive-looking key (``password``, ``DB_PASSWORD``,
    ``api_key``, ...) is replaced whatever text or structure it holds; anything
    that is not plain JSON data (an arbitrary object, whose text form could
    hold anything) becomes a fixed marker. A result larger than the read budget
    is cut, with one marker where it was cut. Returns the copy and the number
    of redactions.
    """
    return _redact(value, 0, _Budget())


def _redact(value: object, depth: int, budget: _Budget) -> tuple[object, int]:
    if depth > _MAX_DEPTH:
        return REDACTED, 1
    if budget.nodes <= 0:
        return TRUNCATED, 1
    budget.nodes -= 1
    if value is None or isinstance(value, bool | int | float):
        return value, 0
    if isinstance(value, str):
        budget.chars -= len(value)
        if budget.chars < 0:
            budget.nodes = 0  # nothing more is read after this
            return TRUNCATED, 1
        return redact_text(value)
    if isinstance(value, Mapping):
        return _redact_mapping(value, depth, budget)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        total = 0
        items: list[object] = []
        for item in value:
            if budget.nodes <= 0:
                if not items or items[-1] != TRUNCATED:  # one marker, not one each
                    items.append(TRUNCATED)
                    total += 1
                break
            redacted, count = _redact(item, depth + 1, budget)
            items.append(redacted)
            total += count
        return items, total
    return UNSUPPORTED, 1


def _redact_mapping(
    value: Mapping, depth: int, budget: _Budget
) -> tuple[dict[str, object], int]:
    total = 0
    out: dict[str, object] = {}

    def put(key: str, item: object) -> None:
        # Two keys can redact to the same text: never let one overwrite another.
        candidate, suffix = key, 1
        while candidate in out:
            suffix += 1
            candidate = f"{key}#{suffix}"
        out[candidate] = item

    for key, item in value.items():
        if budget.nodes <= 0:
            if TRUNCATED not in out:
                put(TRUNCATED, TRUNCATED)
                total += 1
            break
        if not isinstance(key, str):
            put(UNSUPPORTED, UNSUPPORTED)
            total += 1
            continue
        budget.chars -= len(key)
        shown_key, key_count = redact_text(key) if budget.chars >= 0 else (TRUNCATED, 1)
        total += key_count
        if (
            _sensitive_key(key)
            and isinstance(item, str | Mapping | Sequence)
            and not isinstance(item, bytes | bytearray)
            and not is_credential_handle(item)
            and item != ""
        ):
            budget.nodes -= 1
            put(shown_key, REDACTED)
            total += 1
            continue
        redacted, count = _redact(item, depth + 1, budget)
        put(shown_key, redacted)
        total += count
    return out, total
