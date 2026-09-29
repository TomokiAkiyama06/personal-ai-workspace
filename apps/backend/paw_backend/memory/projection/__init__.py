"""Memory Markdown Projection (PAW-045, Decision 0038 Approved).

PostgreSQL is the source of truth of Memory; this package writes a human-readable
Markdown view of it into a dedicated directory (``PAW_MEMORY_PROJECTION_DIR``, on
the HDD), one audience per directory:

```text
<root>/
├── .paw-memory-projection      # marker: this directory is the projection's
├── users/<user id>/            # a user's private memory
├── projects/<project id>/
├── project-groups/<group id>/
├── repos/<repo id>/
└── shared/
    each: <memory id>.md (one per memory) and INDEX.md
```

* ``render``: the pure, deterministic renderer (diff-friendly Markdown).
* ``source``: the current versions of every memory, in one snapshot.
* ``writer``: the safe writer (never into a git work tree or a home directory,
  ``0700`` / ``0600``, no symbolic link followed, only its own files, one run at a
  time).
* ``runner``: one run, recorded as ``memory.projection.completed`` / ``failed``
  in ``audit_events`` (``audit``).

``python -m paw_backend.cli memory-projection-run`` runs it (a systemd timer in
the deployment, ``apps/backend/deploy/systemd``); ``memory-projection-check`` is
the read-only monitor. See ``apps/backend/README.md`` ("Memory Markdown
Projection").
"""

from paw_backend.memory.projection.audit import (
    RESOURCE_KIND,
    ProjectionAction,
    projection_status,
    record_projection_outcome,
)
from paw_backend.memory.projection.records import (
    ProjectedMemory,
    ProjectionPlan,
    ProjectionRunResult,
    ProjectionStatus,
    ProjectionStep,
    WriteReport,
)
from paw_backend.memory.projection.render import (
    FORMAT_VERSION,
    INDEX_FILE,
    ProjectionRenderError,
    directory_for,
    render_index,
    render_memory,
    render_projection,
)
from paw_backend.memory.projection.runner import MemoryProjectionRunner
from paw_backend.memory.projection.source import (
    MemoryProjectionSource,
    ProjectionDatabaseError,
)
from paw_backend.memory.projection.writer import (
    MARKER_NAME,
    LockedTarget,
    ProjectionBusyError,
    ProjectionTargetError,
    TargetProblem,
    check_root_path,
    open_target,
    system_home_directories,
)

__all__ = [
    "FORMAT_VERSION",
    "INDEX_FILE",
    "MARKER_NAME",
    "RESOURCE_KIND",
    "LockedTarget",
    "MemoryProjectionRunner",
    "MemoryProjectionSource",
    "ProjectedMemory",
    "ProjectionAction",
    "ProjectionBusyError",
    "ProjectionDatabaseError",
    "ProjectionPlan",
    "ProjectionRenderError",
    "ProjectionRunResult",
    "ProjectionStatus",
    "ProjectionStep",
    "ProjectionTargetError",
    "TargetProblem",
    "WriteReport",
    "check_root_path",
    "directory_for",
    "open_target",
    "projection_status",
    "record_projection_outcome",
    "render_index",
    "render_memory",
    "render_projection",
    "system_home_directories",
]
