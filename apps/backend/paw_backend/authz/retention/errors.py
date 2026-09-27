"""Typed errors of ``audit_events`` retention / partitioning (Issue #86)."""

from typing import ClassVar


class AuditRetentionError(Exception):
    """Base class of every error raised by the retention rules and service."""

    code: ClassVar[str] = "audit_retention_error"


class PartitionAlreadyExistsError(AuditRetentionError):
    """The bookkeeping table already has a partition of this name.

    Raised by the service, never by a rule function (``plan_missing_partitions``
    only ever plans names that are not already present); it is a guard against a
    caller creating the same partition twice (a race between two maintenance
    runs), not something normal operation triggers.
    """

    code = "partition_already_exists"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"partition already exists: {name}")


class PartitionNotLiveError(AuditRetentionError):
    """An archive or purge was asked for a partition not in the expected status."""

    code = "partition_not_live"

    def __init__(self, name: str, status: str) -> None:
        self.name = name
        self.status = status
        super().__init__(f"partition {name} is not live (status={status})")


class PartitionNotArchivedError(AuditRetentionError):
    """A purge was asked for a partition that is not archived."""

    code = "partition_not_archived"

    def __init__(self, name: str, status: str) -> None:
        self.name = name
        self.status = status
        super().__init__(f"partition {name} is not archived (status={status})")
