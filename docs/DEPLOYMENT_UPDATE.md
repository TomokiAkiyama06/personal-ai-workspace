# Deployment / Update / Rollback

更新日: 2026-09-20
Status: [FIXED DIRECTION]

## Principles

- Versioned releases
- Manual production update in V1
- Safe drain / checkpoint before normal update
- Recovery Projection refresh before risky migration
- Verified database restore point before schema/data migration
- Health check after update
- Rollback to known-good application version
- Model lifecycle independent from application lifecycle
- Backward-compatible DB migration preferred

## Update sequence

1. Update requested by Owner/Admin
2. Pre-update checks
3. Stop accepting new Tasks
4. Drain/checkpoint running Tasks
5. For schema/data migration, stop or block all database writers, including background workers and APIs
6. Refresh Recovery Repository projection
7. Migration compatibility check
8. For schema/data migration, create a consistent database restore point and verify restoration
9. Apply application/schema/data update
10. Start new version and check database/application health and compatibility
11. On failure, restore the compatible known-good database/application state and repeat health checks
12. Resume Queue/Tasks/database writers only after successful validation; otherwise remain in maintenance mode and notify Owner/Admin

## Pre-update checks

- PostgreSQL health
- Recovery Repository readiness
- Disk free space
- Running Task state
- Migration compatibility
- Current / target version
- Runtime dependency readiness

## Database migration gate

Before the first schema or data mutation, require a consistent database backup/snapshot
and a restore procedure verified in an isolated environment. Verify that the restore point
is readable and can restore the expected schema/data with the known-good application version.
Record the restore point, validation results, and migration outcome.

If backup creation, available capacity, or restoration validation fails, do not start the migration.
Keep database writes blocked through backup, migration, health checks, and any required rollback.
This includes Memory Workers, Journal ingestion, projection jobs, and API writes, in addition to Tasks.

Refreshing the Recovery Repository is not a database restore point: it excludes database dumps,
WAL, and database files. Store database backups outside Recovery Git with the same access controls
and User deletion requirements as the database. Local storage is sufficient; off-server PostgreSQL
backup remains optional in V1. Application/model updates that do not mutate the database do not
require this additional database restore point.

## Rollback

Application releases retain at least a previous known-good version.

Schema changes should use expand/migrate/contract style where practical:
- add new schema
- support old + new
- migrate data
- remove old schema only after validation

Transactional migration is useful but does not replace the restore point: a health check can fail
after COMMIT, and data migrations can span multiple steps.

On migration or post-update health-check failure, restore the database when necessary and start
the compatible known-good application. Before resuming writes, verify database/application health
and compatibility and apply the latest User deletion state so restored backups cannot resurrect
erased private data. If restoration or this validation fails, keep the system in maintenance mode
and notify Owner/Admin instead of resuming Queue/Tasks.

## Open implementation choice

The concrete packaging/runtime mechanism (Docker, systemd units, release directories, etc.)
is not required to be frozen in requirements, as long as the operational requirements above are met.
