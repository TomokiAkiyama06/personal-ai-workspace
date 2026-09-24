"""Pure ASGI middleware: request IDs, Host validation, security headers."""

import re
from collections.abc import Sequence
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from paw_backend.errors import error_response
from paw_backend.security import host_from_header

REQUEST_ID_HEADER = "X-Request-ID"
# Only accept an inbound ID that is safe to echo into headers and logs.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestIdMiddleware:
    """Attach a request ID to ``scope["state"]`` and to the response.

    A well-formed inbound ``X-Request-ID`` is kept so that a reverse proxy or
    client can correlate its logs; anything else is replaced by a random ID.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = inbound if _VALID_REQUEST_ID.fullmatch(inbound) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class HostValidationMiddleware:
    """Reject requests whose ``Host`` header is not on the allow-list.

    A browser tricked into resolving an attacker's name to this server (DNS
    rebinding) still sends the attacker's name as ``Host``. Answers HTTP 400
    in the standard error format; a WebSocket handshake is refused the same
    way. Health probes must send an allowed ``Host`` as well.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str]) -> None:
        self.app = app
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        host = host_from_header(Headers(scope=scope).get("host"))
        if host in self.allowed_hosts:
            await self.app(scope, receive, send)
            return

        response = error_response(
            400,
            "invalid_host",
            "Invalid Host header",
            request_id=scope.get("state", {}).get("request_id"),
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """Add defensive headers to every HTTP response.

    The API returns JSON and event streams only, so a locked-down
    Content-Security-Policy is safe. ``Strict-Transport-Security`` is sent
    only when the request arrived over HTTPS (``scope["scheme"]``) or Uvicorn
    itself terminates TLS (``tls_enabled``): browsers ignore the header on
    plain HTTP, and a plain-HTTP listener must not claim HTTPS. Behind a
    TLS-terminating reverse proxy the proxy has to send HSTS (or be trusted
    to forward ``X-Forwarded-Proto: https`` to Uvicorn). Headers a handler
    sets itself are not overwritten.
    """

    def __init__(
        self, app: ASGIApp, *, hsts_max_age_seconds: int, tls_enabled: bool
    ) -> None:
        self.app = app
        self.headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Cache-Control": "no-store",
        }
        self.hsts = (
            f"max-age={hsts_max_age_seconds}" if hsts_max_age_seconds > 0 else None
        )
        self.tls_enabled = tls_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_to_add = dict(self.headers)
        if self.hsts and (self.tls_enabled or scope.get("scheme") == "https"):
            headers_to_add["Strict-Transport-Security"] = self.hsts

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in headers_to_add.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
