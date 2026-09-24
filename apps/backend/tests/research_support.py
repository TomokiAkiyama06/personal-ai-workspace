"""Shared helpers for the research provider tests (stdlib ``unittest`` only)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from paw_backend.research.providers import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ProviderDocument,
    ProviderHit,
    ProviderKind,
    ProviderRegistry,
    ResearchBroker,
    SourceType,
    StaticProvider,
)

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
# A generous deadline that only a hung implementation can hit.
GUARD_SECONDS = 30.0
# Timeout used to cut off a provider that hangs on purpose. The providers that
# must succeed answer immediately, so this leaves them a very wide margin.
SHORT_TIMEOUT = 0.3
SECRET = "SECRET-TOKEN-4f9a1c"


def fixed_clock(value: datetime = NOW) -> Callable[[], datetime]:
    return lambda: value


def hit(
    locator: str = "https://example.com/a",
    *,
    title: str = "Title",
    text: str = "Excerpt",
    published_at: datetime | None = None,
    source_type: SourceType = SourceType.UNKNOWN,
    private_source: bool = False,
) -> ProviderHit:
    return ProviderHit(
        locator,
        title,
        text,
        published_at,
        source_type,
        private_source=private_source,
    )


def document(
    *,
    title: str = "Doc",
    text: str = "Document text",
    published_at: datetime | None = None,
    source_type: SourceType = SourceType.UNKNOWN,
    private_source: bool = False,
) -> ProviderDocument:
    return ProviderDocument(
        title, text, published_at, source_type, private_source=private_source
    )


def registry_of(
    *providers: StaticProvider,
    timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
) -> ProviderRegistry:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider, timeout_seconds=timeout_seconds)
    return registry


def broker_of(
    *providers: StaticProvider,
    clock: Callable[[], datetime] | None = None,
    timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
) -> ResearchBroker:
    # The PAW-051 tests send fixed, harmless queries and have no privacy gate, so
    # they use the explicit opt-out. A broker without a pre-flight and without
    # ``unfiltered=True`` refuses to search (PAW-053).
    return ResearchBroker(
        registry_of(*providers, timeout_seconds=timeout_seconds),
        clock=clock or fixed_clock(),
        unfiltered=True,
    )


def web(name: str = "web-a", **kwargs) -> StaticProvider:
    return StaticProvider(name, ProviderKind.WEB, **kwargs)


def docs(name: str = "docs-a", **kwargs) -> StaticProvider:
    return StaticProvider(name, ProviderKind.DOCS, **kwargs)


def github(name: str = "github-a", **kwargs) -> StaticProvider:
    return StaticProvider(name, ProviderKind.GITHUB, **kwargs)


async def guarded(awaitable: Awaitable):
    """Await ``awaitable`` but fail (instead of hanging CI) after GUARD_SECONDS."""
    async with asyncio.timeout(GUARD_SECONDS):
        return await awaitable


def other_tasks() -> set[asyncio.Task]:
    """Every task of the running loop except the one running the test."""
    return asyncio.all_tasks() - {asyncio.current_task()}
