"""Shared helpers for the research provider tests (stdlib ``unittest`` only)."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

from paw_backend.research.providers import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    MAX_DOCUMENT_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_LOCATOR_CHARS,
    MAX_TITLE_CHARS,
    ProviderDocument,
    ProviderFailure,
    ProviderHit,
    ProviderKind,
    ProviderRegistry,
    ResearchBroker,
    ResearchErrorCode,
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
# A marker for text that must not reach a log line (a forged record, a name).
FORGED = "FORGED-RECORD-77d2"


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


class Tripwire(list):
    """The names of the hooks that ran while it was ``armed()`` (should be none).

    The hostile objects below misbehave only inside ``with tripwire.armed():``, so
    that the test runner can still print them when a test fails.
    """

    active = False

    @contextmanager
    def armed(self) -> Iterator[None]:
        self.active = True
        try:
            yield
        finally:
            self.active = False


def _hook(tripwire: Tripwire, base: type, name: str, behave):
    """A hook ``name`` that is hostile while armed and the plain one otherwise."""

    def hook(self, *args, **kwargs):
        if not tripwire.active:
            return getattr(base, name)(self, *args, **kwargs)
        tripwire.append(name)
        return behave()

    return hook


def _raise(name: str):
    def behave():
        raise RuntimeError(name)

    return behave


def hostile_containers(
    hits: list[ProviderHit], tripwire: Tripwire
) -> dict[str, object]:
    """Label -> a list/tuple subclass or look-alike that holds ``hits``, or lies."""
    out: dict[str, object] = {}
    for base in (list, tuple):
        for label, (name, behave) in {
            "a __len__ that raises": ("__len__", _raise("__len__")),
            "an __iter__ that raises": ("__iter__", _raise("__iter__")),
            "a __getitem__ that raises": ("__getitem__", _raise("__getitem__")),
            "a __len__ that says 0": ("__len__", lambda: 0),
            "a __len__ beyond sys.maxsize": ("__len__", lambda: 2**70),
            "an __iter__ that yields nothing": ("__iter__", lambda: iter(())),
        }.items():
            cls = type(
                f"Hostile{base.__name__.title()}",
                (base,),
                {name: _hook(tripwire, base, name, behave)},
            )
            out[f"{base.__name__} with {label}"] = cls(hits)
        out[f"{base.__name__} without any hook"] = type("Plain", (base,), {})(hits)

    def claiming(claim):
        class Impostor:
            @property
            def __class__(self):
                if not tripwire.active:
                    return type(self)
                tripwire.append("__class__")
                if isinstance(claim, Exception):
                    raise claim
                return claim

        return Impostor()

    out["an object whose __class__ says list"] = claiming(list)
    out["an object whose __class__ says tuple"] = claiming(tuple)
    out["an object whose __class__ raises"] = claiming(RuntimeError("__class__"))
    return out


def hostile_failures(
    tripwire: Tripwire,
) -> dict[str, tuple[BaseException, ResearchErrorCode]]:
    """Label -> ``(exception, expected classification)``.

    Exceptions a provider could raise whose hooks raise or lie. Where the
    constructor of ``ProviderFailure`` ran, its (validated) code is the
    classification; where it did not, or the code was forged, or the exception
    is no ``ProviderFailure``, it is ``INTERNAL_ERROR``.
    """
    Code = ResearchErrorCode

    class RaisingCode(ProviderFailure):
        @property
        def code(self):
            if not tripwire.active:
                return ProviderFailure.code.__get__(self)
            tripwire.append("code")
            raise RuntimeError("code")

        @code.setter
        def code(self, value):  # ignore the constructor's assignment
            pass

    class ChangingCode(ProviderFailure):
        @property
        def code(self):
            if not tripwire.active:
                return ProviderFailure.code.__get__(self)
            tripwire.append("code")
            return Code.NOT_FOUND if len(tripwire) == 1 else "not a code"

        @code.setter
        def code(self, value):
            pass

    class RaisingGetattribute(ProviderFailure):
        def __getattribute__(self, name):
            if name == "code" and tripwire.active:
                tripwire.append(name)
                raise RuntimeError(name)
            return super().__getattribute__(name)

    class NeverInitialised(ProviderFailure):
        def __init__(self) -> None:  # no slot is set
            pass

    forged = ProviderFailure(Code.RATE_LIMITED)
    forged.code = "not a code"

    class RaisingClass(RuntimeError):
        @property
        def __class__(self):
            if not tripwire.active:
                return type(self)
            tripwire.append("__class__")
            raise RuntimeError("__class__")

    class ClaimsTimeout(RuntimeError):
        @property
        def __class__(self):
            if not tripwire.active:
                return type(self)
            tripwire.append("__class__")
            return TimeoutError

    class RaisingName(type):
        @property
        def __name__(cls):
            if not tripwire.active:
                return type.__dict__["__name__"].__get__(cls)
            tripwire.append("__name__")
            raise RuntimeError("__name__")

    class RaisingMetaclass(Exception, metaclass=RaisingName):
        pass

    return {
        "a code property that raises": (
            RaisingCode(Code.RATE_LIMITED),
            Code.RATE_LIMITED,
        ),
        "a code property that changes between reads": (
            ChangingCode(Code.UNAVAILABLE),
            Code.UNAVAILABLE,
        ),
        "a __getattribute__ that raises": (
            RaisingGetattribute(Code.NOT_FOUND),
            Code.NOT_FOUND,
        ),
        "a constructor that never sets the code": (
            NeverInitialised(),
            Code.INTERNAL_ERROR,
        ),
        "a code overwritten after construction": (forged, Code.INTERNAL_ERROR),
        "a __class__ that raises": (RaisingClass(), Code.INTERNAL_ERROR),
        "a __class__ that claims to be a TimeoutError": (
            ClaimsTimeout(),
            Code.INTERNAL_ERROR,
        ),
        "a metaclass whose __name__ raises": (
            RaisingMetaclass(),
            Code.INTERNAL_ERROR,
        ),
    }


def hostile_named_exceptions(tripwire: Tripwire) -> dict[str, BaseException]:
    """Label -> exception whose class is named (or looks named) by the adapter.

    None of these has a name that may reach a log line. Every name carries
    ``SECRET`` or a marker that must not appear either (``FORGED``): text after a
    newline that looks like another log record, control characters, a name of
    100,000 characters, non-ASCII / bidi text, format directives, and names that
    imitate an exception of the allowlist. Where a metaclass hook is defined it
    records itself in ``tripwire`` while armed.
    """

    def named(name: str, bases=(Exception,), **namespace) -> BaseException:
        cls = type(name, bases, namespace)
        if issubclass(cls, ProviderFailure):
            return cls(ResearchErrorCode.UNAVAILABLE)
        return cls()

    renamed = type("Innocent", (Exception,), {})
    renamed.__name__ = f"access_token={SECRET}\n{FORGED}"

    class HookedMeta(type):
        def __getattribute__(cls, name):
            if tripwire.active:
                tripwire.append(f"meta.{name}")
                raise RuntimeError(name)
            return super().__getattribute__(name)

        def __hash__(cls):
            if tripwire.active:
                tripwire.append("meta.__hash__")
                raise RuntimeError("__hash__")
            return super().__hash__()

        def __eq__(cls, other):
            if tripwire.active:
                tripwire.append("meta.__eq__")
                raise RuntimeError("__eq__")
            return super().__eq__(other)

        def __repr__(cls):
            if tripwire.active:
                tripwire.append("meta.__repr__")
                return f"{SECRET}\n{FORGED}"
            return super().__repr__()

        def __str__(cls):
            if tripwire.active:
                tripwire.append("meta.__str__")
                return f"{SECRET}\n{FORGED}"
            return super().__str__()

    class LyingName(type):
        @property
        def __name__(cls):
            if not tripwire.active:
                return type.__dict__["__name__"].__get__(cls)
            tripwire.append("meta.__name__")
            return f"{SECRET}\n{FORGED}"

    class HookedError(Exception, metaclass=HookedMeta):
        pass

    class LyingError(Exception, metaclass=LyingName):
        pass

    return {
        "a secret in the name": named(f"access_token={SECRET}"),
        "a forged log line in the name": named(
            f"x\n2026-09-25 12:00:00 ERROR paw: {FORGED} {SECRET}"
        ),
        "CR LF and an escape sequence": named(f"a\r\n\x1b[2J\x07{FORGED}{SECRET}"),
        "a name of 100,000 characters": named("E" * 100_000 + SECRET),
        "a non-ASCII name": named(f"例外\u202e\u200b{FORGED}{SECRET}"),
        "a homoglyph of an allowed name": named("Runtim\u0435Error"),
        "format directives in the name": named("%s %(x)s %d {0} {SECRET}"),
        "a class named like a builtin": named("RuntimeError"),
        "a class named like the package's error": named("InvalidProviderResponseError"),
        "a class named ProviderFailure": named("ProviderFailure"),
        "a subclass of a builtin with the same name": named(
            "RuntimeError", (RuntimeError,)
        ),
        "a harmless-looking name": named("Innocent"),
        "a __qualname__ override": named(
            "Innocent", __qualname__=f"{SECRET}\n{FORGED}"
        ),
        "a __module__ override": named("Innocent", __module__=f"{SECRET}\n{FORGED}"),
        "a __name__ in the class body": named(
            "Innocent", __name__=f"{SECRET}\n{FORGED}"
        ),
        "a name assigned after creation": renamed(),
        "a subclass of ProviderFailure": named(f"{SECRET}Failure", (ProviderFailure,)),
        "a metaclass with lookup, hash, eq, repr and str hooks": HookedError(),
        "a metaclass whose __name__ lies": LyingError(),
    }


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
