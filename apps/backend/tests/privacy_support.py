"""Shared helpers for the research privacy tests (stdlib ``unittest`` only)."""

import asyncio
import uuid

from paw_backend.research.privacy import (
    ContextLabel,
    ContextPiece,
    InMemoryExternalSendAudit,
    PrivacyGate,
)

from .research_support import GUARD_SECONDS, NOW

__all__ = [
    "GUARD_SECONDS",
    "NOW",
    "OTHER_PROJECT_ID",
    "PROJECT_ID",
    "guarded",
    "make_gate",
    "memory",
    "piece",
    "private_source",
    "public",
    "raw_conversation",
    "secret",
    "wait_for_event_or_task_error",
]

PROJECT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER_PROJECT_ID = uuid.UUID("99999999-8888-7777-6666-555555555555")


def piece(label: ContextLabel, text: str) -> ContextPiece:
    return ContextPiece(label, text)


def private_source(text: str) -> ContextPiece:
    return ContextPiece(ContextLabel.PRIVATE_SOURCE, text)


def memory(text: str) -> ContextPiece:
    return ContextPiece(ContextLabel.PRIVATE_MEMORY, text)


def raw_conversation(text: str) -> ContextPiece:
    return ContextPiece(ContextLabel.RAW_CONVERSATION, text)


def secret(text: str) -> ContextPiece:
    return ContextPiece(ContextLabel.SECRET, text)


def public(text: str) -> ContextPiece:
    return ContextPiece(ContextLabel.PUBLIC, text)


def make_gate(
    audit: InMemoryExternalSendAudit | None = None, **kwargs
) -> tuple[PrivacyGate, InMemoryExternalSendAudit]:
    """A gate with an in-memory audit sink and a fixed clock (``NOW``)."""
    sink = audit if audit is not None else InMemoryExternalSendAudit()
    kwargs.setdefault("clock", lambda: NOW)
    return PrivacyGate(sink, **kwargs), sink


async def guarded(awaitable):
    """Await ``awaitable`` but fail (instead of hanging CI) after GUARD_SECONDS."""
    async with asyncio.timeout(GUARD_SECONDS):
        return await awaitable


async def wait_for_event_or_task_error(
    event: asyncio.Event, task: asyncio.Task
) -> None:
    """Wait until ``event`` is set; if ``task`` ends first, re-raise its outcome.

    A task that fails before it ever reaches the point where it sets the event
    must fail the test at once with its own error, not after a long guard time.
    """
    waiter = asyncio.ensure_future(event.wait())
    try:
        await guarded(asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED))
        if task.done():
            task.result()  # raises its error; a task that returned early is a bug
            raise AssertionError("the task ended before it reached the sink")
    finally:
        waiter.cancel()
