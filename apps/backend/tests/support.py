"""Shared helpers for the backend tests (stdlib ``unittest`` only)."""

import asyncio
import os
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from paw_backend.config import Settings
from paw_backend.db import Database, DatabaseStatus


def paw_environment(**variables: str):
    """Patch the environment: drop every ``PAW_*`` variable, then set ``variables``."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PAW_")}
    environment.update(variables)
    return patch.dict(os.environ, environment, clear=True)


def make_settings(**overrides) -> Settings:
    """Build Settings from ``overrides`` only, ignoring any ``PAW_*`` in the env."""
    with paw_environment():
        return Settings(**overrides)


# TestClient always dials `ws://testserver` for relative WebSocket paths, so the
# tests spell the URL out to send a Host header that the default settings allow.
WEBSOCKET_URL = "ws://localhost/api/v1/events/ws"


def make_client(app: FastAPI, **kwargs) -> TestClient:
    """A TestClient that talks to ``localhost``, a Host the default settings allow."""
    kwargs.setdefault("base_url", "http://localhost")
    return TestClient(app, **kwargs)


class FakeDatabase(Database):
    """A Database whose readiness result is fixed and that never connects."""

    def __init__(self, status: DatabaseStatus = DatabaseStatus.OK) -> None:
        super().__init__(make_settings())
        self.status = status
        self.disposed = False

    async def check(self) -> DatabaseStatus:
        return self.status

    async def dispose(self) -> None:
        self.disposed = True


async def read_sse(app, path: str, chunks: int, limit: float = 5.0):
    """Drive ``app`` with a raw ASGI GET and return (start message, body chunks).

    ``TestClient`` waits for a response to finish, which an endless event
    stream never does. This helper collects ``chunks`` body messages, then
    reports a client disconnect, exactly as a browser closing an EventSource.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 80),
    }
    disconnected = asyncio.Event()
    start = None
    bodies: list[bytes] = []

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal start
        if message["type"] == "http.response.start":
            start = message
        elif message["type"] == "http.response.body" and message.get("body"):
            bodies.append(message["body"])
            if len(bodies) >= chunks:
                disconnected.set()

    async with asyncio.timeout(limit):
        await app(scope, receive, send)
    return start, bodies


async def wait_until(predicate, limit: float = 2.0) -> bool:
    """Poll ``predicate`` until it is true or ``limit`` seconds elapse."""
    try:
        async with asyncio.timeout(limit):
            # Polls a plain counter; there is no event to wait on.
            while not predicate():  # noqa: ASYNC110
                await asyncio.sleep(0.01)
    except TimeoutError:
        return False
    return True
