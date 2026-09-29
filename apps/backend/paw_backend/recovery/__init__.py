"""Recovery Repository projection and restore (PAW-047, Decision 0054 Proposed).

PostgreSQL is the operational source of truth; the Dedicated Recovery
Repository is the disaster-recovery source (REQUIREMENTS.md "Dedicated Recovery
Repository"). This package writes it and reads it back:

* ``format``: the layout, the deterministic JSON, the manifest and checksums.
* ``source``: one snapshot of what the repository holds (an allow-list of
  columns: never a credential, a dump or runtime state).
* ``render``: the pure renderer (users in deletion keep only a deletion record;
  credentials in free text are redacted).
* ``files``: the checkout (marker, lock, ``0700`` / ``0600``, no link followed)
  and the reader of the Memory Markdown Projection (Decision 0038 9).
* ``git``: stage, commit, fast-forward push to the configured upstream only.
* ``backup``: one run (``recovery-backup-run``, every 30 minutes).
* ``records`` / ``restore``: the verified, dry-run-first, all-or-nothing restore
  into an empty workspace (``recovery-restore``).
* ``audit``: the ``audit_events`` rows of both.

See ``apps/backend/README.md`` ("Recovery Repository").
"""

from paw_backend.recovery.audit import (
    BACKUP_RESOURCE_KIND,
    RESTORE_RESOURCE_KIND,
    BackupStatus,
    RecoveryAction,
    backup_status,
)
from paw_backend.recovery.backup import (
    BackupRunResult,
    BackupStep,
    RecoveryBackupRunner,
)
from paw_backend.recovery.files import (
    CheckoutProblem,
    RecoveryBusyError,
    RecoveryFilesError,
)
from paw_backend.recovery.format import (
    MARKER_NAME,
    RECOVERY_FORMAT_VERSION,
)
from paw_backend.recovery.git import GitProblem, RecoveryGitError
from paw_backend.recovery.render import RecoveryPlan, render_recovery
from paw_backend.recovery.restore import (
    RecoveryRestoreError,
    RecoveryRestorer,
    RestoreProblem,
    RestoreResult,
)
from paw_backend.recovery.source import (
    RecoveryDatabaseError,
    RecoverySnapshot,
    RecoverySource,
)

__all__ = [
    "BACKUP_RESOURCE_KIND",
    "MARKER_NAME",
    "RECOVERY_FORMAT_VERSION",
    "RESTORE_RESOURCE_KIND",
    "BackupRunResult",
    "BackupStatus",
    "BackupStep",
    "CheckoutProblem",
    "GitProblem",
    "RecoveryAction",
    "RecoveryBackupRunner",
    "RecoveryBusyError",
    "RecoveryDatabaseError",
    "RecoveryFilesError",
    "RecoveryGitError",
    "RecoveryPlan",
    "RecoveryRestoreError",
    "RecoveryRestorer",
    "RecoverySnapshot",
    "RecoverySource",
    "RestoreProblem",
    "RestoreResult",
    "backup_status",
    "render_recovery",
]
