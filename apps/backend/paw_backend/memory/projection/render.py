"""The pure Markdown renderer of the Memory Projection (PAW-045, Decision 0038).

The output is **diff-friendly** and **deterministic**: the same memories give the
same bytes, whatever order they come in and whenever the run happens.

* One file per memory, ``<memory id>.md``, named by the id (a new title never
  renames the file), with a front matter of fixed keys in a fixed order, then the
  title as a heading and the text. Nothing in a file depends on the time of the
  run: a memory that did not change gives the file it gave before.
* One ``INDEX.md`` per directory: a table of the memories of that directory,
  sorted by status, type, title and id.
* LF line ends, UTF-8, exactly one newline at the end. Times are UTC
  (``...Z``). Front matter strings are JSON strings (JSON is valid YAML), so a
  title with quotes, colons or line breaks cannot break the block. The
  characters JSON leaves as they are but YAML rejects or reads as a line break
  (DEL, the C1 controls, U+2028, U+2029, U+FEFF, U+FFFE, U+FFFF) are written as
  ``\\uXXXX``, which both read back as the same character.

The **directory** of a memory is its audience (Decision 0038 2), from the scope
columns of its current version: ``users/<owner>``, ``projects/<project>``,
``project-groups/<group>``, ``repos/<repo>`` or ``shared``. Only ids name
directories (never a title or a login name), so no text of a memory can choose
where it is written.

Recognisable credentials in titles, texts and branch names are replaced by
``[REDACTED]`` (``tools.credentials.redact_text``) before they reach a file
(Decision 0038 5): the projection is later committed to the Recovery
Repository, which must not hold secrets. PostgreSQL keeps the text as it is. A
text longer than ``MAX_TEXT_CHARS`` cannot be scanned whole: its file holds the
first ``MAX_TEXT_CHARS`` characters and ``[TRUNCATED]``, the front matter says
``truncated: true`` and the run counts it apart from the redactions.
"""

import json
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from paw_backend.memory.models import MemoryScope, MemoryStatus
from paw_backend.memory.projection.records import (
    DirectoryKey,
    ProjectedMemory,
    ProjectionPlan,
)
from paw_backend.tools.credentials import MAX_TEXT_CHARS, redact_text

# Raised when the file format changes; written in every file and the marker.
FORMAT_VERSION = 1

INDEX_FILE = "INDEX.md"
MEMORY_SUFFIX = ".md"
SHARED_DIRECTORY = "shared"
# The top directory of each scope. ``shared`` has no id level below it.
TOP_DIRECTORIES: dict[str, str] = {
    MemoryScope.USER.value: "users",
    MemoryScope.PROJECT.value: "projects",
    MemoryScope.PROJECT_GROUP.value: "project-groups",
    MemoryScope.REPO.value: "repos",
    MemoryScope.SHARED.value: SHARED_DIRECTORY,
}
KEYED_TOP_DIRECTORIES = frozenset(
    name for name in TOP_DIRECTORIES.values() if name != SHARED_DIRECTORY
)

GENERATED_NOTICE = (
    "<!-- Generated from PostgreSQL (the source of truth) by Personal AI "
    "Workspace. Do not edit: edit the memory in the Memory UI. This file is "
    "overwritten. -->"
)

# The order the index lists statuses in: what is in use first.
_STATUS_ORDER = {
    MemoryStatus.ACTIVE.value: 0,
    MemoryStatus.DEPRECATED.value: 1,
    MemoryStatus.SUPERSEDED.value: 2,
    MemoryStatus.HISTORY.value: 3,
}


class ProjectionRenderError(ValueError):
    """A row the renderer cannot place (a scope without its id). A closed text."""


def directory_for(memory: ProjectedMemory) -> DirectoryKey:
    """The directory of ``memory``: its audience, from the scope columns."""
    scope = memory.scope
    ids = {
        MemoryScope.USER.value: memory.owner_user_id,
        MemoryScope.PROJECT.value: memory.project_id,
        MemoryScope.PROJECT_GROUP.value: memory.project_group_id,
        MemoryScope.REPO.value: memory.repo_id,
    }
    if scope == MemoryScope.SHARED.value:
        return (SHARED_DIRECTORY,)
    if scope not in ids or not isinstance(ids[scope], UUID):
        raise ProjectionRenderError("a memory version has no id for its scope")
    return (TOP_DIRECTORIES[scope], str(ids[scope]))


def memory_file_name(memory_id: UUID) -> str:
    return f"{memory_id}{MEMORY_SUFFIX}"


def is_memory_file_name(name: str) -> bool:
    """``<canonical uuid>.md``: a file name the renderer can produce."""
    return name.endswith(MEMORY_SUFFIX) and is_uuid_name(name[: -len(MEMORY_SUFFIX)])


def is_uuid_name(name: str) -> bool:
    """The canonical (lower-case, hyphenated) spelling of a UUID, nothing else."""
    try:
        return str(UUID(name)) == name
    except ValueError:
        return False


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seconds(value: timedelta) -> int | float:
    seconds = value.total_seconds()
    return int(seconds) if seconds == int(seconds) else seconds


def _yaml_unsafe(character: str) -> bool:
    code = ord(character)
    return 0x7F <= code <= 0x9F or character in "\u2028\u2029\ufeff\ufffe\uffff"


def _json(value: object) -> str:
    """``value`` as JSON that YAML reads back unchanged (see the module docstring).

    Readable characters (Japanese, for example) stay as they are. The escaped
    characters can only be inside strings: JSON writes none of them elsewhere.
    """
    text = json.dumps(value, ensure_ascii=False)
    if not any(_yaml_unsafe(character) for character in text):
        return text
    return "".join(
        f"\\u{ord(character):04x}" if _yaml_unsafe(character) else character
        for character in text
    )


def single_line(text: str) -> str:
    """``text`` on one line: every space, line break or control character is a space."""
    out = []
    for character in text:
        if character.isspace() or unicodedata.category(character) in (
            "Cc",
            "Cf",
            "Zl",
            "Zp",
        ):
            out.append(" ")
        else:
            out.append(character)
    return " ".join("".join(out).split())


def _table_cell(text: str) -> str:
    return single_line(text).replace("\\", "\\\\").replace("|", "\\|")


def _heading(title: str) -> str:
    return single_line(title) or "(untitled)"


def _body(content: str) -> str:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def _redact(text: str) -> tuple[str, int, bool]:
    """``redact_text``, with its cut of a too long text counted apart."""
    truncated = len(text) > MAX_TEXT_CHARS
    result, count = redact_text(text)
    return result, count - truncated, truncated


def render_memory_file(memory: ProjectedMemory) -> tuple[bytes, int, bool]:
    """The file of one memory, how many credentials were replaced in it, and
    whether a text was too long to scan and so was cut (``truncated: true``)."""
    title, title_redactions, title_truncated = _redact(memory.title)
    content, content_redactions, content_truncated = _redact(memory.content)
    branch, branch_redactions = (
        (None, 0) if memory.branch is None else redact_text(memory.branch)
    )
    redactions = title_redactions + content_redactions + branch_redactions
    truncated = title_truncated or content_truncated
    scope_key = {
        MemoryScope.USER.value: ("owner_user_id", memory.owner_user_id),
        MemoryScope.PROJECT.value: ("project_id", memory.project_id),
        MemoryScope.PROJECT_GROUP.value: ("project_group_id", memory.project_group_id),
        MemoryScope.REPO.value: ("repo_id", memory.repo_id),
    }.get(memory.scope)
    fields: list[tuple[str, object]] = [
        ("projection_format", FORMAT_VERSION),
        ("memory_id", str(memory.memory_id)),
        ("version", memory.version_number),
        ("scope", memory.scope),
    ]
    if scope_key is not None:
        fields.append((scope_key[0], str(scope_key[1])))
    fields += [
        ("title", title),
        ("memory_type", memory.memory_type),
        ("status", memory.status),
        ("confirmation_state", memory.confirmation_state),
        ("importance", memory.importance),
        ("pinned", memory.pinned),
        ("freshness_policy", memory.freshness_policy),
    ]
    optional: list[tuple[str, object]] = [
        ("verified_at", memory.verified_at and _time(memory.verified_at)),
        (
            "revalidate_after_seconds",
            None
            if memory.revalidate_after is None
            else _seconds(memory.revalidate_after),
        ),
        ("revalidate_triggers", sorted(memory.revalidate_triggers) or None),
        ("expires_at", memory.expires_at and _time(memory.expires_at)),
        ("commit_sha", memory.commit_sha),
        ("branch", branch),
        ("stale_since", memory.stale_since and _time(memory.stale_since)),
    ]
    fields += [(key, value) for key, value in optional if value is not None]
    fields.append(("version_created_at", _time(memory.created_at)))
    if redactions:
        fields.append(("redactions", redactions))
    if truncated:
        fields.append(("truncated", True))
    lines = ["---", *(f"{key}: {_json(value)}" for key, value in fields), "---"]
    text = (
        "\n".join(lines)
        + "\n"
        + GENERATED_NOTICE
        + "\n\n# "
        + _heading(title)
        + "\n\n"
        + _body(content)
    )
    return text.encode("utf-8"), redactions, truncated


def render_memory(memory: ProjectedMemory) -> tuple[bytes, int]:
    """The file of one memory, and how many credentials were replaced in it."""
    data, redactions, _ = render_memory_file(memory)
    return data, redactions


def _status_label(memory: ProjectedMemory) -> str:
    if memory.stale_since is not None and memory.status == MemoryStatus.ACTIVE.value:
        return f"{memory.status} (stale)"
    return memory.status


def render_index(key: DirectoryKey, memories: Iterable[ProjectedMemory]) -> bytes:
    """``INDEX.md`` of one directory: its memories in a stable order."""
    rows = []
    for memory in memories:
        title, _ = redact_text(memory.title)
        rows.append(
            (
                _STATUS_ORDER.get(memory.status, len(_STATUS_ORDER)),
                memory.status,
                memory.memory_type,
                _heading(title).casefold(),
                str(memory.memory_id),
                memory,
                title,
            )
        )
    rows.sort(key=lambda row: row[:5])
    lines = [
        f"# Memory index: {'/'.join(key)}",
        "",
        GENERATED_NOTICE,
        "",
        "| Status | Type | Title | Version | File |",
        "| --- | --- | --- | --- | --- |",
    ]
    for *_, memory, title in rows:
        name = memory_file_name(memory.memory_id)
        lines.append(
            f"| {_table_cell(_status_label(memory))} | "
            f"{_table_cell(memory.memory_type)} | {_table_cell(_heading(title))} | "
            f"{memory.version_number} | [{name}]({name}) |"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_projection(memories: Iterable[ProjectedMemory]) -> ProjectionPlan:
    """Every file of the projection of ``memories`` (current versions)."""
    grouped: dict[DirectoryKey, list[ProjectedMemory]] = {}
    seen: set[UUID] = set()
    for memory in memories:
        if memory.memory_id in seen:
            raise ProjectionRenderError("a memory appears twice")
        seen.add(memory.memory_id)
        grouped.setdefault(directory_for(memory), []).append(memory)
    directories: dict[DirectoryKey, dict[str, bytes]] = {}
    redactions = truncations = 0
    for key in sorted(grouped):
        files: dict[str, bytes] = {}
        for memory in grouped[key]:
            data, count, truncated = render_memory_file(memory)
            files[memory_file_name(memory.memory_id)] = data
            redactions += count
            truncations += truncated
        files[INDEX_FILE] = render_index(key, grouped[key])
        directories[key] = files
    return ProjectionPlan(
        directories=directories,
        memories=len(seen),
        redactions=redactions,
        truncations=truncations,
    )


__all__ = [
    "FORMAT_VERSION",
    "GENERATED_NOTICE",
    "INDEX_FILE",
    "KEYED_TOP_DIRECTORIES",
    "SHARED_DIRECTORY",
    "TOP_DIRECTORIES",
    "ProjectionRenderError",
    "directory_for",
    "is_memory_file_name",
    "is_uuid_name",
    "memory_file_name",
    "render_index",
    "render_memory",
    "render_memory_file",
    "render_projection",
    "single_line",
]
