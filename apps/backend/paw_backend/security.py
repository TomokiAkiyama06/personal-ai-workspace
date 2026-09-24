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
