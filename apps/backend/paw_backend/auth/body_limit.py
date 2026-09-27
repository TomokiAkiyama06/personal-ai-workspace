"""A small cap on the request bodies of the authentication routes.

The login and the token routes are public: without a limit anybody could make the
server read (and parse) a body of any size before it says no. A body of these
routes is a handful of short strings (``AUTH_BODY_MAX_BYTES``); anything larger is
refused with 413 as soon as the limit is passed, and the part that was read is
never handed to the application.

Only ``/api/v1/auth/`` is limited (other routes have their own needs), and the
body is read here in full (at most the limit plus one chunk), then replayed to
the application.
"""

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from paw_backend.errors import error_response

PATH_PREFIX = "/api/v1/auth/"


class AuthBodyLimitMiddleware:
    """Pure ASGI middleware; see the module docstring."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("max_bytes must be an int")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PATH_PREFIX):
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and not (
            declared.isdigit() and int(declared) <= self.max_bytes
        ):
            await self._refuse(scope, receive, send)
            return
        chunks: list[Message] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # a disconnect: hand it on as is
                chunks.append(message)
                break
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                await self._refuse(scope, receive, send)
                return
            chunks.append(message)
            if not message.get("more_body", False):
                break
        replay = iter(chunks)

        async def replayed() -> Message:
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        await self.app(scope, replayed, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            413,
            "payload_too_large",
            "The request body is too large",
            request_id=scope.get("state", {}).get("request_id"),
        )
        await response(scope, receive, send)
