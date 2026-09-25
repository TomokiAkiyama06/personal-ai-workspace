"""The credential as an adapter sees it, and the resolver that produces it.

Where a plaintext credential may exist
--------------------------------------
* In the **secret store** behind :class:`SecretResolver` (a vault, a file the
  backend alone can read, ...). Which product it is stays an implementation choice
  (``REQUIREMENTS.md``); this package ships **no** store, so no plaintext is ever
  written by this package, in any form.
* In a :class:`Secret`, for the duration of one adapter call. The adapter runs in
  the backend (trusted) and calls :meth:`Secret.reveal` to authenticate to the
  provider. Nothing else may.

Everywhere else there is only the **handle** (``cred_`` + 32 hex characters, see
``paw_backend.tools.credentials``): the database row, the audit log, the errors,
the logs and every object handed to a user or an agent name a connection by its
kind and its handle, never by its value. A :class:`Secret` therefore refuses to be
turned into text in any of the usual ways (``repr``, ``str``, ``format``, ``%``,
pickle, ``copy``, ``vars``), so a careless log line or a debugging dump prints
``Secret(<redacted>)``.

This is defence in depth, not a sandbox: an adapter that calls ``reveal()`` holds
the value and can misuse it. What the service adds is that the result an adapter
returns is scrubbed of the exact value and of every recognisable credential format
before a user or an agent sees it (``Secret.scrub``, ``tools.credentials``).
"""

import unicodedata
from typing import Final, Protocol

from paw_backend.connections.errors import InputProblem, InvalidConnectionInputError
from paw_backend.connections.limits import MAX_SECRET_CHARS

REDACTED_SECRET: Final = "[REDACTED]"
_TEXT: Final = "Secret(<redacted>)"


class Secret:
    """A credential value that cannot be printed, copied or pickled by accident."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if type(value) is not str:  # a str subclass could override every method
            raise InvalidConnectionInputError("secret", InputProblem.NOT_A_STRING)
        if not value:
            raise InvalidConnectionInputError("secret", InputProblem.EMPTY)
        if len(value) > MAX_SECRET_CHARS:
            raise InvalidConnectionInputError("secret", InputProblem.TOO_LONG)
        if any(char == "\x00" or unicodedata.category(char) == "Cs" for char in value):
            raise InvalidConnectionInputError("secret", InputProblem.INVALID_CHARACTERS)
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The value. Only an adapter (backend code) authenticating calls this."""
        return object.__getattribute__(self, "_value")

    def scrub(self, text: str) -> str:
        """``text`` with every exact occurrence of the value replaced."""
        return text.replace(self.reveal(), REDACTED_SECRET)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a Secret cannot be changed")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a Secret cannot be changed")

    def __repr__(self) -> str:
        return _TEXT

    def __str__(self) -> str:
        return _TEXT

    def __format__(self, format_spec: str) -> str:
        return _TEXT

    def __bytes__(self) -> bytes:
        return _TEXT.encode()

    def __reduce__(self):
        raise TypeError("a Secret cannot be pickled")

    def __reduce_ex__(self, protocol: object):
        raise TypeError("a Secret cannot be pickled")

    def __copy__(self):
        raise TypeError("a Secret cannot be copied")

    def __deepcopy__(self, memo: object):
        raise TypeError("a Secret cannot be copied")

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other  # never compare values: no oracle for guessing


class SecretResolver(Protocol):
    """Turns a handle into the credential. Implemented by the secret store.

    Only the service calls it, on the path that runs an adapter, and hands the
    result to that adapter alone. It raises (any exception) for a handle it does
    not know or cannot read; the service records ``FailureCode.UNAVAILABLE`` and
    logs only the exception's type.
    """

    async def resolve(self, handle: str) -> Secret: ...
