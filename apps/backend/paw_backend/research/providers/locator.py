"""Canonical, sanitised source locators (PAW-051).

Every ``SourceMetadata.locator`` is produced by ``canonicalize_locator``, so two
providers that found the same page produce the same string and the broker can
deduplicate by it. The function is pure: no DNS lookup, no network access.
Whether a host may be contacted at all (SSRF, private addresses, robots.txt) is
decided by the concrete adapters and the Backend Tool Broker ``network``
capability, not here.
"""

import re
import unicodedata
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from paw_backend.research.providers.contract import MAX_LOCATOR_CHARS
from paw_backend.research.providers.errors import InvalidLocatorError

# Query parameters that only track a visitor are removed. A parameter is dropped
# when its name, compared case-insensitively, starts with one of the prefixes or
# equals one of the names. Nothing else is dropped (``ref`` is NOT tracking: it
# selects a branch on GitHub).
TRACKING_PARAMETER_PREFIXES = ("utm_",)
TRACKING_PARAMETERS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
    }
)
# Query parameters that carry a credential are removed as well, so that a
# provider that hands out a credentialed URL cannot leak the credential into a
# stored or displayed source locator. Same comparison rules as above. This is a
# best-effort list: a credential inside the path cannot be recognised.
CREDENTIAL_PARAMETERS = frozenset(
    {
        "access_token",
        "id_token",
        "refresh_token",
        "token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "sig",
        "signature",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
    }
)
# A port equal to the default of its scheme is not part of the canonical form.
DEFAULT_PORTS = MappingProxyType({"http": 80, "https": 443})

# A host is at most 253 characters of dot-separated labels (RFC 1035).
MAX_HOST_CHARS = 253
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# One pass over a path or query: an existing ``%xx`` escape, or a run of
# non-ASCII characters. Scanning once means that a ``%`` never hides what
# follows it (``%%e6`` contains the escape ``%e6``).
_ESCAPE_OR_NON_ASCII = re.compile(r"%([0-9A-Fa-f]{2})|([^\x00-\x7f]+)")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs"})


def _escape(text: str) -> str:
    """Upper-case the hex digits of ``%xx`` escapes and percent-encode non-ASCII."""

    def replace(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return "%" + match.group(1).upper()
        return "".join(f"%{byte:02X}" for byte in match.group(2).encode("utf-8"))

    return _ESCAPE_OR_NON_ASCII.sub(replace, text)


def _has_forbidden_character(raw: str) -> bool:
    return any(
        char.isspace()
        or char == "\\"
        or unicodedata.category(char) in _FORBIDDEN_CATEGORIES
        for char in raw
    )


# A proxy in front of a server can decode a query name more than once, so a name
# is decoded until it stops changing, at most this many times.
_MAX_NAME_DECODINGS = 4


def _decoded_name(name: str) -> str:
    """The parameter name as a query parser (or a proxy before it) would read it."""
    for _ in range(_MAX_NAME_DECODINGS):
        decoded = unquote(name, errors="replace")
        if decoded == name:
            break
        name = decoded
    return name


def _is_dropped_parameter(name: str) -> bool:
    lowered = _decoded_name(name).lower()
    return (
        lowered.startswith(TRACKING_PARAMETER_PREFIXES)
        or lowered in TRACKING_PARAMETERS
        or lowered in CREDENTIAL_PARAMETERS
    )


def canonicalize_locator(raw: str) -> str:
    """Return the canonical form of ``raw`` or raise ``InvalidLocatorError``.

    ``raw`` that is not a ``str`` raises ``TypeError``. Every other problem
    raises ``paw_backend.research.providers.errors.InvalidLocatorError`` (fixed
    message, never containing ``raw``). The steps, in this order:

    1. Reject (``InvalidLocatorError``): the empty string; more than
       ``MAX_LOCATOR_CHARS`` (2048) characters; any whitespace character
       (``str.isspace()``) or control/surrogate character (Unicode category
       ``Cc`` / ``Cs``) anywhere, including leading and trailing; any
       backslash. Nothing is stripped or repaired.
    2. Split with ``urllib.parse.urlsplit``; a ``ValueError`` from it is
       ``InvalidLocatorError``. The scheme (compared case-insensitively) must be
       ``http`` or ``https``; anything else (``ftp:``, ``file:``, ``javascript:``,
       ``data:``, a scheme-less ``example.com/x``, ``//host/x``) is rejected.
    3. The authority must not contain ``@`` (user info, even an empty one such as
       ``https://@host/``); a ``@`` in the path or query is fine.
    4. The host is lower-cased and ONE trailing ``.`` is removed. It must be
       non-empty, ASCII, at most 253 characters and consist of dot-separated
       labels of 1 to 63 characters made of ``a-z``, ``0-9`` and ``-`` that
       neither start nor end with ``-``. So IPv6 literals, underscores,
       non-ASCII (IDN) hosts and empty labels (``a..b``) are rejected. IPv4-shaped
       hosts are accepted as they are.
    5. The port (``SplitResult.port``; a ``ValueError`` or port ``0`` is
       ``InvalidLocatorError``) is dropped when absent, empty (``host:``) or
       equal to ``DEFAULT_PORTS[scheme]``; otherwise it is written as a decimal
       number without leading zeros (``:08080`` becomes ``:8080``).
    6. An empty path becomes ``/``. Otherwise the path is kept as it is: no dot
       segment resolution, no trailing-slash change, case preserved.
    7. In the path and the query only: every ``%`` followed by two hex digits
       has its digits upper-cased (``%e6`` becomes ``%E6``), and every character
       with a code point of 0x80 or more is replaced by the ``%XX`` escapes
       (upper-case hex) of its UTF-8 bytes. Everything else is unchanged.
    8. The fragment, including ``#``, is removed.
    9. The query is split on ``&``; empty pieces are dropped; pieces whose name
       (the text before the first ``=``) is a tracking parameter (see
       ``TRACKING_PARAMETER_PREFIXES`` / ``TRACKING_PARAMETERS``) or a
       credential parameter (``CREDENTIAL_PARAMETERS``), compared
       case-insensitively after the name is percent-decoded (repeatedly, at most
       ``_MAX_NAME_DECODINGS`` times, as a proxy might), are dropped; the rest
       are sorted by ``(name, value)`` (``value`` is the text after the first
       ``=``, ``""`` if there is none; plain string comparison; a piece keeps
       its own spelling, so ``flag`` stays ``flag`` and ``a=`` stays ``a=``) and
       joined with ``&``. If nothing is left there is no ``?``.
    10. The result is ``<scheme>://<host>[:<port>]<path>[?<query>]`` and must
        have at most ``MAX_LOCATOR_CHARS`` characters (escaping can make it
        longer than the input), otherwise ``InvalidLocatorError``.

    ``http`` and ``https`` stay different; ``www.`` is not removed. The function
    is idempotent: ``canonicalize_locator(canonicalize_locator(x))`` equals
    ``canonicalize_locator(x)``.
    """
    if not isinstance(raw, str):
        raise TypeError("raw must be a str")
    # The length limit comes first: everything below is linear in the input and
    # must not run on an arbitrarily large string.
    if not raw or len(raw) > MAX_LOCATOR_CHARS or _has_forbidden_character(raw):
        raise InvalidLocatorError()

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        # ``from None``: the ValueError text can quote the input.
        raise InvalidLocatorError() from None
    if parts.scheme not in DEFAULT_PORTS:  # ``urlsplit`` lower-cases the scheme
        raise InvalidLocatorError()
    # A non-ASCII authority is rejected before ``hostname`` lower-cases it:
    # ``str.lower`` maps some non-ASCII characters (KELVIN SIGN) to ASCII.
    if "@" in parts.netloc or not parts.netloc.isascii():
        raise InvalidLocatorError()

    host = (parts.hostname or "").removesuffix(".")
    if (
        not host
        or len(host) > MAX_HOST_CHARS
        or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
    ):
        raise InvalidLocatorError()
    if port == 0:
        raise InvalidLocatorError()

    authority = host
    if port is not None and port != DEFAULT_PORTS[parts.scheme]:
        authority = f"{host}:{port}"

    pieces = []
    for piece in parts.query.split("&"):
        name, equals, value = piece.partition("=")
        if piece and not _is_dropped_parameter(name):
            pieces.append((_escape(name), _escape(value), equals))
    pieces.sort(key=lambda piece: (piece[0], piece[1]))
    query = "&".join(f"{name}{equals}{value}" for name, value, equals in pieces)

    path = _escape(parts.path) or "/"
    result = f"{parts.scheme}://{authority}{path}" + (f"?{query}" if query else "")
    if len(result) > MAX_LOCATOR_CHARS:
        raise InvalidLocatorError()
    return result
