"""Canonical, sanitised source locators (PAW-051).

Every ``SourceMetadata.locator`` is produced by ``canonicalize_locator``, so two
providers that found the same page produce the same string and the broker can
deduplicate by it. The function is pure: no DNS lookup, no network access.
Whether a host may be contacted at all (SSRF, private addresses, robots.txt) is
decided by the concrete adapters and the Backend Tool Broker ``network``
capability, not here.
"""

from types import MappingProxyType

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
       case-insensitively, are dropped; the rest are sorted by ``(name, value)``
       (``value`` is the text after the first ``=``, ``""`` if there is none;
       plain string comparison; a piece keeps its own spelling, so ``flag`` stays
       ``flag`` and ``a=`` stays ``a=``) and joined with ``&``. If nothing is
       left there is no ``?``.
    10. The result is ``<scheme>://<host>[:<port>]<path>[?<query>]`` and must
        have at most ``MAX_LOCATOR_CHARS`` characters (escaping can make it
        longer than the input), otherwise ``InvalidLocatorError``.

    ``http`` and ``https`` stay different; ``www.`` is not removed. The function
    is idempotent: ``canonicalize_locator(canonicalize_locator(x))`` equals
    ``canonicalize_locator(x)``.
    """
    raise NotImplementedError("PAW-051 stub")
