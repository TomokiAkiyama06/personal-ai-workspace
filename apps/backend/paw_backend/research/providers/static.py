"""``StaticProvider``: an in-memory ``ResearchProvider`` for tests and demos.

It never touches the network. Besides canned hits and documents it can be told
to raise, to hang until cancelled, to return a malformed response, to ignore the
requested limit, or to run a hook before answering, so tests can exercise every
failure path of the broker with a real provider object.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

from paw_backend.research.providers.contract import (
    ProviderDocument,
    ProviderHit,
    ProviderKind,
)
from paw_backend.research.providers.errors import ProviderFailure, ResearchErrorCode

_UNSET: object = object()


class StaticProvider:
    """A configurable fake provider.

    ``name`` and ``kind`` are plain attributes (a test may change them after
    registration). ``search`` answers ``hits[:limit]`` (all of ``hits`` when
    ``ignore_limit`` is set). ``fetch`` answers ``documents[locator]`` or raises
    ``ProviderFailure(NOT_FOUND)``. Behaviour switches, applied in this order:
    ``before_search`` hook (awaited), ``hang`` (waits forever), ``search_error``
    (raised), ``raw_search_response`` (returned as it is, even if malformed).
    ``fetch`` has ``hang_fetch``, ``fetch_error`` and ``raw_fetch_response``
    likewise (no hook).

    ``search_calls`` records ``(query, limit)``, ``fetch_calls`` the locators;
    ``cancelled`` becomes True if a call was cancelled.
    """

    def __init__(
        self,
        name: str,
        kind: ProviderKind = ProviderKind.WEB,
        *,
        hits: Sequence[ProviderHit] = (),
        documents: Mapping[str, ProviderDocument] | None = None,
        search_error: BaseException | None = None,
        fetch_error: BaseException | None = None,
        raw_search_response: object = _UNSET,
        raw_fetch_response: object = _UNSET,
        hang: bool = False,
        hang_fetch: bool = False,
        ignore_limit: bool = False,
        before_search: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.hits = tuple(hits)
        self.documents = dict(documents or {})
        self.search_error = search_error
        self.fetch_error = fetch_error
        self.raw_search_response = raw_search_response
        self.raw_fetch_response = raw_fetch_response
        self.hang = hang
        self.hang_fetch = hang_fetch
        self.ignore_limit = ignore_limit
        self.before_search = before_search
        self.search_calls: list[tuple[str, int]] = []
        self.fetch_calls: list[str] = []
        self.cancelled = False

    async def search(self, query: str, *, limit: int) -> Sequence[ProviderHit]:
        self.search_calls.append((query, limit))
        try:
            if self.before_search is not None:
                await self.before_search()
            if self.hang:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.search_error is not None:
            raise self.search_error
        if self.raw_search_response is not _UNSET:
            return self.raw_search_response  # type: ignore[return-value]
        return self.hits if self.ignore_limit else self.hits[:limit]

    async def fetch(self, locator: str) -> ProviderDocument:
        self.fetch_calls.append(locator)
        if self.hang_fetch:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.raw_fetch_response is not _UNSET:
            return self.raw_fetch_response  # type: ignore[return-value]
        try:
            return self.documents[locator]
        except KeyError:
            raise ProviderFailure(ResearchErrorCode.NOT_FOUND) from None
