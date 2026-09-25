"""The adapter interface of the shared connections and its registry (PAW-030).

An adapter is the only code that talks to Codex or Claude. It is **backend code**
(never a user's or an agent's) and gets a credential only as a
:class:`~paw_backend.connections.secret.Secret`, resolved by the service from the
connection's handle for the duration of one call. No adapter ships in this package:
a real one needs the provider's terms of use for the credential (Decision 0016) and
a network policy; tests use in-memory doubles.

The interface (``ConnectionAdapter``)
-------------------------------------
* ``kind``: a :class:`ConnectionKind` member (exactly; the string ``"codex"`` is not
  accepted).
* ``async check_health(secret) -> ConnectionStatus``: is the credential good? A
  member of :class:`ConnectionStatus`. ``AdapterFailure(EXPIRED)`` means the
  provider no longer accepts it; any other exception counts as ``UNAVAILABLE``.
* ``async run(secret, request) -> AdapterResult``: one call. ``AdapterFailure(code)``
  reports a classified failure; any other exception is an internal error. Neither
  message is ever kept.

Validation up front (as ``ProviderRegistry`` does for research providers)
------------------------------------------------------------------------
:meth:`AdapterRegistry.register` refuses an object that does not fit, so a wrong
adapter fails when the backend starts, not as an "everything failed" outcome later.
Reading a member and inspecting it runs the adapter's own code (a property,
``__getattr__``, ``__signature__``): whatever that raises (any ``BaseException``), and
a request it makes to cancel the running task, becomes the fixed
:class:`AdapterInterfaceError` of that member, with nothing of the adapter in it.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from paw_backend.connections.domain import ConnectionKind, ConnectionStatus, FailureCode
from paw_backend.connections.errors import (
    AdapterInterfaceError,
    DuplicateAdapterError,
    UnknownAdapterError,
)
from paw_backend.connections.limits import (
    MAX_CALL_TIMEOUT_SECONDS,
    MAX_RESULT_CHARS,
    MAX_TOKENS_PER_CALL,
)
from paw_backend.connections.secret import Secret
from paw_backend.connections.validation import (
    validate_model,
    validate_optional_tokens,
    validate_prompt,
    validate_result_text,
    validate_seconds,
)
from paw_backend.research.providers.guard import CancelGuard


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """What an adapter is asked to do. ``prompt`` is content: it is not part of the
    ``repr`` and is stored and logged nowhere."""

    model: str
    prompt: str = field(repr=False)
    timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", validate_model("model", self.model))
        object.__setattr__(self, "prompt", validate_prompt("prompt", self.prompt))
        object.__setattr__(
            self,
            "timeout_seconds",
            validate_seconds(
                "timeout_seconds", self.timeout_seconds, MAX_CALL_TIMEOUT_SECONDS
            ),
        )


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """What one call returned. Token counts are ``None`` when the provider does not
    report them ("as far as obtainable", ``REQUIREMENTS.md``)."""

    text: str = field(repr=False)
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        validate_adapter_result(self)


def validate_adapter_result(result: "AdapterResult") -> None:
    """Check the fields of an ``AdapterResult`` (it is frozen, but a field can be
    forced with ``object.__setattr__``): the service calls this on what an adapter
    returned, whatever the constructor did."""
    validate_result_text("text", result.text, MAX_RESULT_CHARS)
    validate_optional_tokens("input_tokens", result.input_tokens, MAX_TOKENS_PER_CALL)
    validate_optional_tokens("output_tokens", result.output_tokens, MAX_TOKENS_PER_CALL)


class AdapterFailure(Exception):
    """Raised BY AN ADAPTER to report a classified failure (no message: the
    adapter's own text is discarded on purpose).

    ``code`` is a slot of THIS class, read through ``AdapterFailure.code`` and never
    through the instance, so a subclass's property cannot raise or lie while a
    failure is classified.
    """

    __slots__ = ("code",)

    def __init__(self, code: FailureCode) -> None:
        if type(code) is not FailureCode:
            raise TypeError("code must be a FailureCode")
        AdapterFailure.code.__set__(self, code)
        super().__init__(code.value)


class ConnectionAdapter(Protocol):
    """One adapter per connection kind. See the module docstring."""

    kind: ConnectionKind

    async def check_health(self, secret: Secret) -> ConnectionStatus: ...

    async def run(self, secret: Secret, request: AdapterRequest) -> AdapterResult: ...


def _guarded[T](member: str, read: Callable[[], T]) -> T:
    """Run ``read`` (adapter code that runs synchronously) as one guarded window."""
    failed = False
    with CancelGuard() as guard:
        try:
            result = read()
        except BaseException:  # adapter code, synchronous: any failure is its fault
            failed = True
    if failed or guard.retracted:
        # Raised outside the ``except``: nothing of the adapter is chained to it.
        raise AdapterInterfaceError(member)
    return result


def _accepts_call(method: object, *args: object) -> bool:
    """A coroutine function whose signature accepts this positional call."""
    if not callable(method) or not inspect.iscoroutinefunction(method):
        return False
    try:
        inspect.signature(method).bind(*args)
    except (TypeError, ValueError):
        return False
    return True


def validate_adapter(adapter: object) -> ConnectionKind:
    """The ``kind`` of ``adapter`` if it satisfies ``ConnectionAdapter``.

    Members are checked in this order and the first failure is reported in
    ``AdapterInterfaceError.member``: ``kind`` (a ``ConnectionKind`` member itself,
    read once; ``type()`` is used, so a look-alike object is refused), then
    ``check_health`` (a coroutine function accepting ``(secret)``), then ``run``
    (accepting ``(secret, request)``). Nothing is called on the adapter except
    reading these attributes.
    """
    kind = _guarded("kind", lambda: getattr(adapter, "kind", None))
    if type(kind) is not ConnectionKind:
        raise AdapterInterfaceError("kind")
    if not _guarded(
        "check_health",
        lambda: _accepts_call(getattr(adapter, "check_health", None), object()),
    ):
        raise AdapterInterfaceError("check_health")
    if not _guarded(
        "run",
        lambda: _accepts_call(getattr(adapter, "run", None), object(), object()),
    ):
        raise AdapterInterfaceError("run")
    return kind


class AdapterRegistry:
    """The adapters of a backend, at most one per :class:`ConnectionKind`.

    Populated once at start-up and then only read (it is not thread-safe).
    """

    def __init__(self) -> None:
        self._adapters: dict[ConnectionKind, ConnectionAdapter] = {}

    def register(self, adapter: ConnectionAdapter) -> ConnectionKind:
        """Validate and register ``adapter``; return its kind.

        ``AdapterInterfaceError`` for an object that does not fit,
        ``DuplicateAdapterError`` if the kind has an adapter already (nothing is
        replaced: a wrong wiring must fail, not silently switch provider).
        """
        kind = validate_adapter(adapter)
        if kind in self._adapters:
            raise DuplicateAdapterError(kind)
        self._adapters[kind] = adapter
        return kind

    def get(self, kind: ConnectionKind) -> ConnectionAdapter:
        """The adapter of ``kind``.

        ``TypeError`` for a non-member, ``UnknownAdapterError`` when none is registered.
        """
        if type(kind) is not ConnectionKind:
            raise TypeError("kind must be a ConnectionKind")
        try:
            return self._adapters[kind]
        except KeyError:
            raise UnknownAdapterError(kind) from None

    def find(self, kind: ConnectionKind) -> ConnectionAdapter | None:
        return self._adapters.get(kind) if type(kind) is ConnectionKind else None

    def kinds(self) -> tuple[ConnectionKind, ...]:
        """The registered kinds in ``ConnectionKind`` declaration order."""
        return tuple(kind for kind in ConnectionKind if kind in self._adapters)

    def __len__(self) -> int:
        return len(self._adapters)

    def __contains__(self, kind: object) -> bool:
        return type(kind) is ConnectionKind and kind in self._adapters
