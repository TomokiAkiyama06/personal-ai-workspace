"""Values of the Memory Markdown Projection (PAW-045, Decision 0038).

``ProjectedMemory`` is one memory as the projection sees it: its **current
version** (the highest ``version_number``), read from PostgreSQL, the source of
truth. ``ProjectionPlan`` is the whole projection as bytes per file, made by the
pure renderer (``render.py``) and written by ``writer.py``; ``WriteReport`` says
what the writer changed on disk and ``ProjectionRunResult`` what one run did.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProjectedMemory:
    """The current version of one memory (the columns the Markdown shows)."""

    memory_id: UUID
    version_number: int
    scope: str
    owner_user_id: UUID | None
    project_id: UUID | None
    project_group_id: UUID | None
    repo_id: UUID | None
    memory_type: str
    title: str
    content: str
    importance: int
    pinned: bool
    status: str
    confirmation_state: str
    freshness_policy: str
    verified_at: datetime | None
    revalidate_after: timedelta | None
    revalidate_triggers: tuple[str, ...]
    expires_at: datetime | None
    commit_sha: str | None
    branch: str | None
    stale_since: datetime | None
    created_at: datetime


# A directory of the projection, relative to its root: ``("shared",)`` or a top
# directory and one id, for example ``("users", "<uuid>")``.
DirectoryKey = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectionPlan:
    """Every file of the projection: ``directories[key][file name] = bytes``.

    Only directories that hold at least one memory are in the plan. ``memories``
    is how many memories were rendered and ``redactions`` how many credentials
    were replaced in their titles and texts (``tools.credentials.redact_text``);
    ``truncations`` how many memories had a text too long to scan, so cut
    (``truncated: true`` in their front matter; the file is not the whole text).
    """

    directories: Mapping[DirectoryKey, Mapping[str, bytes]]
    memories: int
    redactions: int
    truncations: int = 0


@dataclass(frozen=True, slots=True)
class WriteReport:
    """What one ``LockedTarget.sync`` changed.

    ``written``: files created or replaced; ``unchanged``: files already exactly
    as planned (not touched, so their modification time stays); ``removed``:
    memory and index files of the projection that the plan no longer has;
    ``unmanaged``: entries in the projection's directories that are not the
    projection's (left alone, never read or deleted).
    """

    written: int = 0
    unchanged: int = 0
    removed: int = 0
    unmanaged: int = 0


class ProjectionStep(StrEnum):
    """The step a run failed at (``ProjectionRunResult.failed_step``)."""

    CHECK_TARGET = "check_target"
    READ_DATABASE = "read_database"
    RENDER = "render"
    WRITE_FILES = "write_files"


@dataclass(frozen=True, slots=True)
class ProjectionRunResult:
    """What one ``MemoryProjectionRunner.run`` did and whether it is healthy.

    ``failed_step`` / ``error``: ``None`` on success; otherwise the step and a
    closed code (a ``TargetProblem`` value, or the exception's class name, never
    its message). ``audited``: whether the outcome row reached ``audit_events``.
    A run is ``ok`` only when every step succeeded **and** that was recorded.
    """

    memories: int = 0
    redactions: int = 0
    truncations: int = 0
    report: WriteReport = field(default_factory=WriteReport)
    failed_step: ProjectionStep | None = None
    error: str | None = None
    audited: bool = False

    @property
    def ok(self) -> bool:
        return self.failed_step is None and self.audited


@dataclass(frozen=True, slots=True)
class ProjectionStatus:
    """The last run and the last successful run, from ``audit_events``.

    ``last_action`` is ``memory.projection.completed`` / ``...failed`` (``None``
    when no run was ever recorded), ``last_reason`` its ``reason`` (counts, or
    ``<step>:<code>``).
    """

    last_action: str | None
    last_run_at: datetime | None
    last_reason: str | None
    last_completed_at: datetime | None


__all__ = [
    "DirectoryKey",
    "ProjectedMemory",
    "ProjectionPlan",
    "ProjectionRunResult",
    "ProjectionStatus",
    "ProjectionStep",
    "WriteReport",
]
