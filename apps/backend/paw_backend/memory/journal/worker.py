"""The Memory Worker contract and the validation of what a worker returns (PAW-041).

A Memory Worker turns the text of a Pending Observation into memory candidates.
The contract is the one the Memory Worker benchmark (PAW-018) judges, so a worker
that scored well there can be plugged in unchanged:

* ``benchmarks/memory_worker_runner.py``: ``extract(input_text) -> str`` (the text
  of a JSON document). Here the method is ``async`` (the real worker is a GPU
  service behind a network call, and an ``async`` method cannot block the event
  loop); a synchronous benchmark worker is wrapped by its adapter.
* ``benchmarks/schemas/memory-worker-output-v1.schema.json``: an object with one
  member ``memories``, a list of objects with ``key`` (non-blank text), ``scope``
  (``user`` / ``project`` / ``repo`` / ``shared``), ``state`` (``confirmed`` /
  ``inferred``), ``supersedes`` (the key of the memory it replaces, or ``null``;
  required), optional ``content`` (non-blank text) and optional ``conflicts_with``
  (unique, non-blank keys). No other member is allowed anywhere.
  ``tests/test_journal_worker_contract.py`` compares this module with that file.

What a worker returns is text derived from a raw conversation and produced by a
model, so it is untrusted input: :func:`parse_worker_output` validates the WHOLE
output before anything is written, and one violation drops all of it (the
benchmark's ``schema_adherence`` does the same: a case with any violation loses
every prediction). It adds bounds the schema does not have (counts, lengths,
control characters in a key) because a memory version has column limits and the
output is stored. A rejected output raises :class:`WorkerOutputError` whose
message is a closed code, never the output.

The worker's ``scope`` and ``state`` are **claims**, not decisions. What the
consolidator does with them is in ``rules.py``: the backend, not the worker,
decides who may read the memory and whether it is confirmed.
"""

import inspect
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from paw_backend.memory.journal import limits
from paw_backend.memory.journal.domain import WorkerScope, WorkerState
from paw_backend.memory.journal.errors import OutputProblem, WorkerOutputError
from paw_backend.memory.journal.validation import has_forbidden_characters

CONTRACT = "memory-worker-output-v1"

_TOP_FIELDS = frozenset({"memories"})
_MEMORY_FIELDS = frozenset(
    {"key", "scope", "state", "supersedes", "content", "conflicts_with"}
)
_REQUIRED_MEMORY_FIELDS = ("key", "scope", "state", "supersedes")
_NON_BLANK = re.compile(r"\S")


@runtime_checkable
class MemoryWorker(Protocol):
    """Extracts memory candidates from the text of one observation.

    ``extract`` returns the text of a JSON document that satisfies
    ``memory-worker-output-v1`` (an empty ``memories`` list is a valid answer:
    nothing worth remembering). It raises :class:`WorkerUnavailableError` (or
    ``ConnectionError``) when the worker cannot be reached: the consolidator then
    keeps the job and tries again later. Any other exception is a failed attempt.

    It is given the text of the observation and nothing else: no user id, no
    conversation id, no other user's memory. What it returns is validated and
    then applied under the identity of the conversation's owner, taken from the
    database; the worker cannot name another owner.
    """

    async def extract(self, input_text: str) -> str: ...


def check_worker(worker: object) -> None:
    """Raise ``TypeError`` unless ``worker.extract`` is an ``async`` callable.

    A synchronous ``extract`` would block the event loop for the length of an
    inference; it is refused when the consolidator is built, not at the first job.
    """
    extract = getattr(worker, "extract", None)
    if not callable(extract) or not inspect.iscoroutinefunction(extract):
        raise TypeError("worker.extract must be an async function")


@dataclass(frozen=True, slots=True)
class WorkerMemory:
    """One validated memory of a worker's output.

    The text fields are derived from a raw conversation, so they are left out of
    ``repr``.
    """

    key: str = field(repr=False)
    scope: WorkerScope
    state: WorkerState
    supersedes: str | None = field(repr=False)
    content: str | None = field(repr=False)
    conflicts_with: tuple[str, ...] = field(repr=False, default=())


def _fail(problem: OutputProblem) -> WorkerOutputError:
    return WorkerOutputError(problem)


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json`` keeps the last of two members with one name; the contract refuses."""
    members: dict[str, Any] = {}
    for name, value in pairs:
        if name in members:
            raise ValueError("duplicate member name")
        members[name] = value
    return members


def _reject_constant(name: str) -> Any:
    raise ValueError("non-standard constant")


def _decode(raw: str) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_members,
            parse_constant=_reject_constant,
        )
    # ``RecursionError``: a document nested a thousand levels deep. ``ValueError``:
    # not JSON, a duplicate member name, ``NaN`` / ``Infinity``, a number with too
    # many digits.
    except (ValueError, RecursionError):
        raise _fail(OutputProblem.NOT_JSON) from None


def _key(value: object, *, allow_none: bool = False) -> str | None:
    """A key or a key reference: non-blank, bounded, no control character."""
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise _fail(OutputProblem.WRONG_TYPE)
    if (
        _NON_BLANK.search(value) is None
        or len(value) > limits.MAX_KEY_CHARS
        or has_forbidden_characters(value)
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise _fail(OutputProblem.INVALID_VALUE)
    return value


def _content(value: object) -> str:
    if not isinstance(value, str):
        raise _fail(OutputProblem.WRONG_TYPE)
    if (
        _NON_BLANK.search(value) is None
        or len(value) > limits.MAX_CONTENT_CHARS
        or has_forbidden_characters(value)
    ):
        raise _fail(OutputProblem.INVALID_VALUE)
    return value


def _member[E: WorkerScope | WorkerState](value: object, kind: type[E]) -> E:
    if not isinstance(value, str):
        raise _fail(OutputProblem.WRONG_TYPE)
    try:
        return kind(value)
    except ValueError:
        raise _fail(OutputProblem.INVALID_VALUE) from None


def _conflicts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail(OutputProblem.WRONG_TYPE)
    if len(value) > limits.MAX_CONFLICTS_PER_MEMORY:
        raise _fail(OutputProblem.TOO_MANY)
    keys = tuple(_key(item) for item in value)
    if len(set(keys)) != len(keys):
        raise _fail(OutputProblem.DUPLICATE)
    return keys  # type: ignore[return-value]  # every element is a str here


def _memory(item: object) -> WorkerMemory:
    if not isinstance(item, Mapping):
        raise _fail(OutputProblem.NOT_AN_OBJECT)
    if not item.keys() <= _MEMORY_FIELDS:
        raise _fail(OutputProblem.UNKNOWN_FIELD)
    if any(name not in item for name in _REQUIRED_MEMORY_FIELDS):
        raise _fail(OutputProblem.MISSING_FIELD)
    return WorkerMemory(
        key=_key(item["key"]),  # type: ignore[arg-type]
        scope=_member(item["scope"], WorkerScope),
        state=_member(item["state"], WorkerState),
        supersedes=_key(item["supersedes"], allow_none=True),
        content=_content(item["content"]) if "content" in item else None,
        conflicts_with=_conflicts(item["conflicts_with"])
        if "conflicts_with" in item
        else (),
    )


def parse_worker_output(raw: object) -> tuple[WorkerMemory, ...]:
    """The memories of a worker's output, or :class:`WorkerOutputError`.

    ``raw`` must be a ``str`` (bytes and parsed objects are not accepted: the
    contract is the text the worker returns). The whole document is checked
    before the first memory is returned; an empty list is valid.
    """
    if not isinstance(raw, str):
        raise _fail(OutputProblem.NOT_TEXT)
    if len(raw) > limits.MAX_RAW_OUTPUT_CHARS:
        raise _fail(OutputProblem.TOO_LARGE)
    document = _decode(raw)
    if not isinstance(document, dict):
        raise _fail(OutputProblem.NOT_AN_OBJECT)
    if not document.keys() <= _TOP_FIELDS:
        raise _fail(OutputProblem.UNKNOWN_FIELD)
    if "memories" not in document:
        raise _fail(OutputProblem.MISSING_FIELD)
    memories = document["memories"]
    if not isinstance(memories, list):
        raise _fail(OutputProblem.WRONG_TYPE)
    if len(memories) > limits.MAX_MEMORIES_PER_OUTPUT:
        raise _fail(OutputProblem.TOO_MANY)
    return tuple(_memory(item) for item in memories)
