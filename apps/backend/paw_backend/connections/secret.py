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
returns is scrubbed of the value and of every recognisable credential format before a
user or an agent sees it (``Secret.scrub``, ``tools.credentials``).

What "the value" means in a text (``Secret.scrub`` and ``Secret.visible_in``)
----------------------------------------------------------------------------
Not only the exact characters. ``tools.credentials.redact_text`` normalises the WHOLE
text (it removes format characters such as the zero-width space and applies NFKC)
whenever it finds a credential pattern, so a value written in full-width characters
(``ＡＢＣ１２３``) or with a zero-width space inside (``AB<ZWSP>C123``) can BECOME the
exact value in that step. A model that reads the
text also reads through those forms. The scrub therefore finds the value in the
normalised, case-folded view of the text (:func:`fold`: format characters removed, NFKC,
case-folded, NFKC again) and removes the span of the ORIGINAL text that produced it, and
the service applies it before and after the pattern redaction. :meth:`Secret.visible_in`
is the check that nothing is left, in any of those views; when it still finds the value
(the marker itself contains it, a composition across characters that the scrub does not
follow), the service refuses the answer instead of returning it.

Not covered, and said so: look-alike characters of other scripts (Cyrillic ``a``),
characters separated by spaces or line breaks, encodings (base64, reversed, escaped
text). A credential written that way is not recognised as the value.
"""

import functools
import unicodedata
from bisect import bisect_right
from typing import Final, Protocol

from paw_backend.connections.errors import InputProblem, InvalidConnectionInputError
from paw_backend.connections.limits import MAX_SECRET_CHARS

REDACTED_SECRET: Final = "[REDACTED]"

# The code points of the Unicode category Cf (format characters: zero-width space and
# joiners, soft hyphen, byte order mark, bidirectional controls, tags). They are in
# planes 0, 1 and 14 only; ``tests/test_connections_secret_scrub.py`` compares this
# with a scan of every code point.
_FORMAT_RANGES: Final = (range(0x30000), range(0xE0000, 0xF0000))


@functools.cache
def _format_table() -> dict[int, None]:
    """``str.translate`` table that deletes the format characters (built once)."""
    return {
        cp: None
        for planes in _FORMAT_RANGES
        for cp in planes
        if unicodedata.category(chr(cp)) == "Cf"
    }


def format_characters() -> frozenset[int]:
    """Every code point of the category Cf."""
    return frozenset(_format_table())


def fold(text: str) -> str:
    """``text`` as a normalisation and a case change would make it: format characters
    removed, NFKC, case-folded, NFKC again (idempotent). ``ＡＢ\u200bC１`` and ``abc1``
    have the same fold."""
    stripped = text.translate(_format_table())
    normalised = unicodedata.normalize("NFKC", stripped)
    return unicodedata.normalize("NFKC", normalised.casefold())


def _occurrences(text: str, needle: str) -> list[int]:
    """Every start of ``needle`` in ``text``, overlapping ones included."""
    starts = []
    index = text.find(needle)
    while index != -1:
        starts.append(index)
        index = text.find(needle, index + 1)
    return starts


def _folded_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """The spans of ``text`` whose fold holds ``needle``.

    The text is cut into clusters (a character with the combining marks and format
    characters that follow it), each cluster is folded on its own, and a match in the
    joined folds is mapped back to the clusters it touches. A match that ends inside a
    cluster takes the whole cluster (more is removed, never less). ASCII text is its
    own view (the fold of ASCII is ``lower()``).
    """
    if text.isascii():
        return [(i, i + len(needle)) for i in _occurrences(text.lower(), needle)]
    table = _format_table()
    bounds: list[tuple[int, int]] = []
    offsets: list[int] = []
    parts: list[str] = []
    total = 0
    index, size = 0, len(text)
    while index < size:
        end = index + 1
        while end < size and (
            unicodedata.combining(text[end]) or ord(text[end]) in table
        ):
            end += 1
        piece = fold(text[index:end])
        bounds.append((index, end))
        offsets.append(total)
        parts.append(piece)
        total += len(piece)
        index = end
    spans = []
    for start in _occurrences("".join(parts), needle):
        first = bisect_right(offsets, start) - 1
        last = bisect_right(offsets, start + len(needle) - 1) - 1
        spans.append((bounds[first][0], bounds[last][1]))
    return spans


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:  # overlapping (adjacent stay two)
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


_TEXT: Final = "Secret(<redacted>)"


class Secret:
    """A credential value that cannot be printed, copied or pickled by accident."""

    __slots__ = ("_value", "_needle")

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
        # The value as it looks in the folded view of a text (may be empty for a value
        # of format characters only: then only the exact value is looked for).
        object.__setattr__(self, "_needle", fold(value))

    def reveal(self) -> str:
        """The value. Only an adapter (backend code) authenticating calls this."""
        return object.__getattribute__(self, "_value")

    def scrub(self, text: str) -> str:
        """``text`` with the value replaced by ``[REDACTED]``: the exact value, and
        every span that a normalisation or a case change would turn into it (see the
        module docstring). Idempotent, except when the marker itself contains the value
        (``visible_in`` then says so). Text without the value is returned unchanged.
        """
        value = self.reveal()
        needle = object.__getattribute__(self, "_needle")
        spans = [(i, i + len(value)) for i in _occurrences(text, value)]
        if needle and needle in fold(text):
            spans.extend(_folded_spans(text, needle))
        if not spans:
            return text
        pieces = []
        position = 0
        for start, end in _merge(spans):
            pieces.append(text[position:start])
            pieces.append(REDACTED_SECRET)
            position = end
        pieces.append(text[position:])
        return "".join(pieces)

    def visible_in(self, text: str) -> bool:
        """Whether the value can still be read in ``text``: exactly, or in its folded
        view (the check that the service runs on what it is about to return)."""
        needle = object.__getattribute__(self, "_needle")
        return self.reveal() in text or (bool(needle) and needle in fold(text))

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
