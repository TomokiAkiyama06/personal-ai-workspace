# Deployment / Update / Rollback

更新日: 2026-09-20
Status: [FIXED DIRECTION]

## Principles

- Versioned releases
- Manual production update in V1
- Safe drain / checkpoint before normal update
- Recovery Projection refresh before risky migration
- Health check after update
- Rollback to known-good application version
- Model lifecycle independent from application lifecycle
- Backward-compatible DB migration preferred

## Update sequence

1. Update requested by Owner/Admin
2. Pre-update checks
3. Stop accepting new Tasks
4. Drain/checkpoint running Tasks
5. Refresh Recovery Repository projection
6. Migration compatibility check
7. Apply application/schema update
8. Start new version
9. Health check
10. Resume Queue/Tasks
11. Roll back if health check fails

## Pre-update checks

- PostgreSQL health
- Recovery Repository readiness
- Disk free space
- Running Task state
- Migration compatibility
- Current / target version
- Runtime dependency readiness

## Rollback

Application releases retain at least a previous known-good version.

Schema changes should use expand/migrate/contract style where practical:
- add new schema
- support old + new
- migrate data
- remove old schema only after validation

## Open implementation choice

The concrete packaging/runtime mechanism (Docker, systemd units, release directories, etc.)
is not required to be frozen in requirements, as long as the operational requirements above are met.
