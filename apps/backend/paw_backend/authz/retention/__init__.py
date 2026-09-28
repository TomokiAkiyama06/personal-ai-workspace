"""``audit_events`` retention, partitioning and archiving (Issue #86, Decision 0027).

Public surface:

* ``records``: ``PartitionStatus``, ``PartitionWindow``, ``RetentionPolicy``,
  ``default_policy``, ``MaintenanceReport`` — frozen value objects, no database.
* ``rules``: pure decision functions (``plan_missing_partitions``,
  ``partitions_due_for_archive``, ``partitions_due_for_purge``) and the month /
  naming helpers they are built from.
* ``audit``: ``RetentionAction``, ``RetentionActor`` and the row writer,
  reusing the existing ``audit_events`` trail (no schema change of its own).
* ``models``: the ORM model of the ``audit_retention_partitions`` bookkeeping
  table (Migration 0086).
* ``service``: ``AuditRetentionService``, the Store — the only place that runs
  the ``CREATE TABLE ... PARTITION OF`` / ``ATTACH`` / ``DETACH`` / ``DROP TABLE``
  SQL. Needs a privileged (migration-role) connection; see its docstring.
* ``runner``: ``run_scheduled_maintenance`` (Issue #117) — one scheduled run:
  the lock, the three steps, the coverage check and the run's own audit row.
  ``python -m paw_backend.cli audit-retention-run`` calls it.
"""

from paw_backend.authz.retention.audit import RetentionAction, RetentionActor
from paw_backend.authz.retention.errors import (
    AuditRetentionError,
    MaintenanceAlreadyRunningError,
    PartitionAlreadyExistsError,
    PartitionNotArchivedError,
    PartitionNotLiveError,
)
from paw_backend.authz.retention.records import (
    MaintenanceReport,
    PartitionStatus,
    PartitionWindow,
    RetentionPolicy,
    default_policy,
)
from paw_backend.authz.retention.rules import (
    ARCHIVE_PARENT_TABLE,
    BOOKKEEPING_TABLE,
    LEGACY_PARTITION_NAME,
    LIVE_PARENT_TABLE,
    first_uncovered_moment,
    month_start,
    next_month_start,
    partition_name,
    partitions_due_for_archive,
    partitions_due_for_purge,
    plan_missing_partitions,
)
from paw_backend.authz.retention.runner import (
    MaintenanceRunResult,
    MaintenanceStep,
    run_scheduled_maintenance,
)
from paw_backend.authz.retention.service import AuditRetentionService

__all__ = [
    "ARCHIVE_PARENT_TABLE",
    "BOOKKEEPING_TABLE",
    "LEGACY_PARTITION_NAME",
    "LIVE_PARENT_TABLE",
    "AuditRetentionError",
    "AuditRetentionService",
    "MaintenanceAlreadyRunningError",
    "MaintenanceReport",
    "MaintenanceRunResult",
    "MaintenanceStep",
    "PartitionAlreadyExistsError",
    "PartitionNotArchivedError",
    "PartitionNotLiveError",
    "PartitionStatus",
    "PartitionWindow",
    "RetentionAction",
    "RetentionActor",
    "RetentionPolicy",
    "default_policy",
    "first_uncovered_moment",
    "month_start",
    "next_month_start",
    "partition_name",
    "partitions_due_for_archive",
    "partitions_due_for_purge",
    "plan_missing_partitions",
    "run_scheduled_maintenance",
]
