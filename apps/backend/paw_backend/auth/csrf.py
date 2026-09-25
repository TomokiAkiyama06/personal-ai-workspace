"""Refuse a cross-origin browser request that changes something (CSRF).

The session cookie is ``SameSite=Strict`` (a browser does not even send it on a
cross-site request), and this is the second, independent layer: a state-changing
request (``POST``, ``PUT``, ``PATCH``, ``DELETE``) that a browser sends from
another origin is refused before the application sees it, whether or not it
carries a cookie (a login is a CSRF target too: "login CSRF" signs the victim
in as the attacker).

* ``Origin`` present: it must be this server's own origin (its authority equals
  ``Host``, which ``HostValidationMiddleware`` already restricted) or one of
  ``PAW_ALLOWED_ORIGINS``. ``Origin: null`` (sandboxed pages, some redirects)
  is refused.
* ``Origin`` absent but ``Sec-Fetch-Site`` present (a browser that does not send
  ``Origin`` on this request): it must say ``same-origin`` or ``none``.
* Neither: not a browser that can be tricked into a cross-site request (a CLI, a
  script, a test client): let through. Such a client has to hold a session id to
  do anything, and one that has it could send the request itself.

Requests that only read are never refused here (``GET``, ``HEAD``, ``OPTIONS``);
the WebSocket handshake has its own Origin check (``require_allowed_origin``).
"""

from collections.abc import Sequence

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from paw_backend.errors import error_response
from paw_backend.security import origin_allowed

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAME_SITE_VALUES = frozenset({"same-origin", "none"})


class OriginCheckMiddleware:
    """Pure ASGI middleware; see the module docstring."""

    def __init__(self, app: ASGIApp, *, allowed_origins: Sequence[str]) -> None:
        if isinstance(allowed_origins, str) or not all(
            isinstance(origin, str) for origin in allowed_origins
        ):
            raise TypeError("allowed_origins must be a sequence of str")
        self.app = app
        self.allowed_origins = list(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"].upper() not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is not None:
            allowed = origin_allowed(origin, headers.get("host"), self.allowed_origins)
        else:
            fetch_site = headers.get("sec-fetch-site")
            allowed = fetch_site is None or fetch_site.lower() in _SAME_SITE_VALUES
        if allowed:
            await self.app(scope, receive, send)
            return
        response = error_response(
            403,
            "forbidden_origin",
            "Cross-origin request refused",
            request_id=scope.get("state", {}).get("request_id"),
        )
        await response(scope, receive, send)
