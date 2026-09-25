"""Host and Origin validation helpers (pure functions, no framework imports)."""

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def host_from_header(header: str | None) -> str | None:
    """Return the lower-cased host of a ``Host`` header, without the port.

    IPv6 literals keep their brackets (``[::1]``) so that they compare equal
    to the entries of ``PAW_ALLOWED_HOSTS``.
    """
    if not header:
        return None
    header = header.strip().lower()
    if header.startswith("["):
        end = header.find("]")
        return header[: end + 1] if end != -1 else None
    host = header.partition(":")[0]
    return host or None


def normalize_origin(value: str) -> str | None:
    """Return ``scheme://host[:port]`` (default port dropped) or ``None``.

    Only ``http`` and ``https`` origins without credentials, path, query or
    fragment are valid. ``null`` (sent by sandboxed pages) is not an origin.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if (
        scheme not in _DEFAULT_PORTS
        or not host
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def origin_allowed(
    origin: str, host_header: str | None, allowed_origins: list[str]
) -> bool:
    """Whether a browser handshake from ``origin`` may proceed.

    An origin is accepted when it is listed in ``allowed_origins`` or when it
    is the origin the request was addressed to (same-origin: its authority
    equals the ``Host`` header). ``Host`` itself is validated separately.
    """
    normalized = normalize_origin(origin)
    if normalized is None:
        return False
    if normalized in allowed_origins:
        return True
    if not host_header:
        return False
    scheme, _, authority = normalized.partition("://")
    host = host_header.strip().lower()
    return host in (authority, f"{authority}:{_DEFAULT_PORTS[scheme]}")


def origin_matches_request(
    origin: str,
    host_header: str | None,
    scheme: object,
    allowed_origins: list[str],
) -> bool:
    """Whether ``origin`` is the origin of the request itself, or one that is listed.

    The request's origin is ``scheme://host[:port]`` where ``scheme`` is the
    **external** scheme of the request (``scope["scheme"]``: what the ASGI server
    made of the connection and, from a proxy it trusts, of
    ``X-Forwarded-Proto``; Uvicorn's ``FORWARDED_ALLOW_IPS`` is the trust
    setting) and ``host_header`` is the ``Host`` header. All three parts must be
    the same as the Origin's, default ports understood (``https://h`` is
    ``https://h:443``): ``Origin: http://h`` is not the origin of a request to
    ``https://h`` even though the authority is the same, or a page served over
    plain HTTP could post to the HTTPS site (login CSRF).

    A scheme that is not exactly ``http`` or ``https`` (missing, ``ws``, anything
    else) is *unknown*: the request's own origin cannot be established, so only a
    listed origin can match (default deny). A listed origin is one full origin
    (scheme, host, port), never just an authority.
    """
    normalized = normalize_origin(origin)
    if normalized is None:
        return False
    if normalized in allowed_origins:
        return True
    if scheme not in _DEFAULT_PORTS or not host_header:
        return False
    return normalize_origin(f"{scheme}://{host_header.strip()}") == normalized
