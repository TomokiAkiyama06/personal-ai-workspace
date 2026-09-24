"""Pure ASGI middleware: request IDs and security response headers."""

import re
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


class SecurityHeadersMiddleware:
    """Add defensive headers to every HTTP response.

    The API returns JSON and event streams only, so a locked-down
    Content-Security-Policy is safe. ``Strict-Transport-Security`` is ignored
    by browsers on plain HTTP and takes effect once the client reached the
    service over HTTPS. Headers a handler sets itself are not overwritten.
    """

    def __init__(self, app: ASGIApp, *, hsts_max_age_seconds: int) -> None:
        self.app = app
        self.headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Cache-Control": "no-store",
        }
        if hsts_max_age_seconds > 0:
            self.headers["Strict-Transport-Security"] = (
                f"max-age={hsts_max_age_seconds}"
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
