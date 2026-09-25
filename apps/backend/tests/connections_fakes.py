"""Fakes for the shared connection tests: no database, no network, no process.

Every "credential" in these tests is a canary string built by concatenation (never a
literal that looks like a provider key: GitHub push protection rejects those).
"""

import asyncio
from datetime import UTC, datetime, timedelta

from paw_backend.connections import (
    AdapterRequest,
    AdapterResult,
    ConnectionKind,
    ConnectionStatus,
    Secret,
)
from paw_backend.tasks import TaskRun

# The plaintext credential of the tests. Built from pieces: it must not be a literal
# that a secret scanner (or the credential detectors of this code base) recognises,
# and it must not match one of the redaction formats either, so that a test that
# finds it in an output proves the *exact-value* scrub, not a pattern.
CANARY = "paw-canary-credential-" + "6c1f" + "0e77" + "-do-not-print"
T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
RUN = TaskRun(1, 0)


def handle(number: int = 1) -> str:
    """A well-formed credential handle (``cred_`` + 32 hex characters)."""
    return "cred_" + f"{number:032x}"


class FakeClock:
    """An injectable clock that only moves when the test says so."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeAdapter:
    """An in-memory adapter: no network, no process. Records what it was given.

    ``gate`` (an ``asyncio.Event``) makes ``run`` wait until the test sets it, so a
    test can hold a call in flight. ``error`` is raised by ``run`` after the gate;
    ``health_error`` by ``check_health``.
    """

    def __init__(
        self,
        kind: ConnectionKind,
        *,
        text: str = "an answer",
        input_tokens: int | None = 10,
        output_tokens: int | None = 5,
        error: BaseException | None = None,
        gate: asyncio.Event | None = None,
        health: ConnectionStatus = ConnectionStatus.CONNECTED,
        health_error: BaseException | None = None,
        result: object = None,
    ) -> None:
        self.kind = kind
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.error = error
        self.gate = gate
        self.health = health
        self.health_error = health_error
        self.result = result
        self.started = asyncio.Event()
        self.revealed: list[str] = []  # the plaintext each call authenticated with
        self.requests: list[AdapterRequest] = []
        self.health_checks = 0

    async def check_health(self, secret: Secret) -> ConnectionStatus:
        self.health_checks += 1
        self.revealed.append(secret.reveal())
        if self.health_error is not None:
            raise self.health_error
        return self.health

    async def run(self, secret: Secret, request: AdapterRequest) -> AdapterResult:
        self.revealed.append(secret.reveal())
        self.requests.append(request)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result  # type: ignore[return-value]
        return AdapterResult(self.text, self.input_tokens, self.output_tokens)


class FakeResolver:
    """The secret store of the tests: handle to plaintext, in memory."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {})
        self.resolved: list[str] = []
        self.error: BaseException | None = None
        self.result: object = None

    async def resolve(self, handle: str) -> Secret:
        self.resolved.append(handle)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result  # type: ignore[return-value]
        return Secret(self.secrets[handle])


__all__ = [
    "CANARY",
    "RUN",
    "T0",
    "FakeAdapter",
    "FakeClock",
    "FakeResolver",
    "handle",
]
