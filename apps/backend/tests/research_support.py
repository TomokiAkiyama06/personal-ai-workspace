"""Shared helpers for the research provider tests (stdlib ``unittest`` only)."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.research.providers import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    MAX_DOCUMENT_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_LOCATOR_CHARS,
    MAX_TITLE_CHARS,
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


def _with(target, **fields):
    """``target`` with fields set around its frozen constructor (no validation)."""
    for name, value in fields.items():
        object.__setattr__(target, name, value)
    return target


def _malformed(build, *, long_text: int, locator: bool) -> dict[str, object]:
    """Typed objects that the constructor refuses or could never have produced.

    ``build()`` returns a valid object. Each entry is a label and an object that
    was built around the constructor, so that ``__post_init__`` never ran on it.
    """
    far = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
    fields = {
        "title": {
            "title is not a str": [None, 5, b"t"],
            "title is too long": ["t" * (MAX_TITLE_CHARS + 1)],
        },
        "text": {
            "text is not a str": [None, 5, b"x", ["x"]],
            "text is too long": ["x" * (long_text + 1), "x" * 5_000_000],
            "text has a lone surrogate": ["\ud800"],
        },
        "published_at": {
            "published_at is not a datetime": ["2026-09-01T00:00:00Z", 5],
            "published_at is naive": [datetime(2026, 9, 1)],
            "published_at is beyond UTC": [far],
        },
        "source_type": {
            "source_type is not a SourceType": ["official_docs", None, 0],
        },
        "private_source": {
            "private_source is not a bool": [0, 1, "false", None],
        },
    }
    if locator:
        fields["locator"] = {
            "locator is not a str": [None, 5, b"https://a.example/"],
            "locator is empty": [""],
            "locator is too long": ["https://a.example/" + "x" * MAX_LOCATOR_CHARS],
        }
    out: dict[str, object] = {}
    for field_name, cases in fields.items():
        for label, values in cases.items():
            for index, value in enumerate(values):
                out[f"{label} #{index}"] = _with(build(), **{field_name: value})
        # A slot that the constructor never set.
        missing = build()
        object.__delattr__(missing, field_name)
        out[f"{field_name} was never set"] = missing
    return out


def malformed_hits() -> dict[str, object]:
    """Label -> a ``ProviderHit`` that is malformed in exactly one way."""
    hits = _malformed(hit, long_text=MAX_EXCERPT_CHARS, locator=True)

    class NeverInitialised(ProviderHit):
        def __init__(self) -> None:  # the reviewer's case: no slot is set
            pass

    hits["a subclass whose constructor sets nothing"] = NeverInitialised()
    return hits


def malformed_documents() -> dict[str, object]:
    """Label -> a ``ProviderDocument`` that is malformed in exactly one way."""
    documents = _malformed(document, long_text=MAX_DOCUMENT_CHARS, locator=False)

    class NeverInitialised(ProviderDocument):
        def __init__(self) -> None:
            pass

    documents["a subclass whose constructor sets nothing"] = NeverInitialised()
    return documents


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
    return ResearchBroker(
        registry_of(*providers, timeout_seconds=timeout_seconds),
        clock=clock or fixed_clock(),
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
