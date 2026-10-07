# Deployment / Update / Rollback

更新日: 2026-10-07
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
- Host available memory before a model runtime starts (see "Model runtime start")

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

## Model runtime start

Issue #182, [Decision 0039](decisions/0039-compute-scheduler-calibration.md) 4 (Approved);
the values are [Decision 0072](decisions/0072-runtime-jit-host-memory-guard.md) (Approved).
On 2026-09-30 a vLLM first load built FlashInfer JIT kernels with `ninja`'s default
parallelism (CPU count + 2): 27 `cicc` processes took about 75 GiB of host RAM, the host
ran out of memory, desktop processes were killed and the machine rebooted.
Every start of a model runtime (by the Compute Resource Scheduler or by hand) therefore:

1. **Caps the JIT build**: `MAX_JOBS=4` and `FLASHINFER_NVCC_THREADS=1`.
   `CommandModelControl` runs its commands with them; a systemd unit does not inherit the
   backend's environment and sets them with `Environment=`.
2. **Checks the host's memory first**: no GPU runtime starts while `MemAvailable`
   (`/proc/meminfo`) is below 40 GiB or cannot be read. `CommandModelControl` then runs no
   command, logs a warning and fails with `host_memory_low` (the scheduler marks the model
   `FAILED` and tries again after 60 s); the unit's `ExecStartPre` refuses the same for a
   manual start. Unloads and the CPU copies of Embedding / Reranker models are not checked.
3. **Caps the runtime below that minimum**: `MemoryMax=32G` on the runtime's unit; the
   40 GiB minimum is this cap plus 8 GiB the host keeps for the kernel, the backend and the
   desktop. So the runtime and its JIT build cannot by themselves exhaust the host; past
   the cap the kernel reclaims the unit's page cache (the weights it read) and then stops
   the runtime only. A cap relative to the host's total RAM would not protect the host
   (other processes may already hold most of it), nor would a minimum equal to the cap.
   Raise the cap and the minimum together if a runtime needs more.
4. **Waits up to 900 s for the load** (Decision 0039 2: the longest measured first load
   from the HDD was 412 s): the scheduler's load timeout and the unit's `TimeoutStartSec`.

pip CUDA wheels (`CUDA_HOME` = `site-packages/nvidia/cu13` of the runtime's venv):

- The JIT links with `-L$CUDA_HOME/lib64 -lcudart` (and `-lcublas`, `-lcublasLt`), but the
  wheel only ships `lib/libcudart.so.13` etc. (no `lib64`, no unversioned `.so`), so the
  link fails with `ld: cannot find -lcudart`. Create a directory of unversioned symlinks
  outside the venv (do not modify the venv) and put it on `LIBRARY_PATH`.
- When the pip `nvcc` is newer than the pip CUDA headers, FlashInfer's bundled CCCL refuses
  to compile ("CUDA compiler and CUDA toolkit headers are incompatible"); set
  `FLASHINFER_EXTRA_CUDAFLAGS=-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK` for that runtime.

[`apps/backend/deploy/systemd/paw-llm-main.service`](../apps/backend/deploy/systemd/paw-llm-main.service)
is an example unit with all of the above.

Model footprint (Decision 0039 1): give each `DeploymentSpec` a footprint (`gpu_bytes`,
the sum of its four parts) of the benchmark's measured peak, not
`gpu-memory-utilization` × the GPU: `max(GPU memory used during the run − used before
the load) + 2 GiB`. vLLM exceeded its `gpu-memory-utilization` budget by up to 8.4 GiB.

## Implementation (Issue #54, Decision 0079)

[Decision 0079](decisions/0079-deploy-update-rollback.md) proposes the mechanism; until it is
approved, no production update or rollback is run with it. Operator steps are in the
[backend README](../apps/backend/README.md#deploy--update--rollbackpaw-068).

- **Versioned release**: `paw-release build` makes one immutable directory per commit,
  `/opt/paw/releases/<YYYYMMDD-sha12>/` (`git archive`, its own venv and web build, and
  `release.json` with the schema head, the migration chain and a source digest). The units run
  `/opt/paw/current`, a symlink switched atomically. Releases are kept; one becomes
  **known-good** when it passes an update's health check.
- **Manual update**: `paw-release update <version>` on the server by the Owner/Admin
  (no HTTP API, no automatic update). `paw-release precheck <version>` shows the checks
  and whether a schema migration is needed, changing nothing.
- **Safe drain / checkpoint**: a maintenance row in the database stops the task queue from
  handing out work; running tasks are held like in Full GPU Mode (Decision 0055) and finish
  their running node (the checkpoint); the update waits up to `drain_timeout_seconds`
  (900 s) and otherwise aborts and resumes them. `--stop-now` (critical security update)
  goes on without waiting; the tasks are not cancelled and resume afterwards.
- **Pre-update health / recovery check**: database reachable and migrated, no maintenance on,
  `audit_events` partitions to the end of next month (the `audit-retention-check` of
  Decision 0031), the last Recovery backup completed within 90 minutes, the Recovery
  Repository configured, disk space, the schema compatibility, and configured commands
  (for example that `paw-audit-retention.timer` and `paw-recovery-backup.timer` are enabled).
  The Recovery projection is refreshed (`recovery-backup-run`) before the writers stop.
- **Backward-compatible migration**: a migration declares `paw_compatibility = "expand"` when
  the release before it runs on the new schema; a rollback over expand-only migrations keeps
  the database. Anything else (or no declaration) needs a restore point to roll back.
- **Restore point**: only for an update with a migration, after every writer stopped:
  `pg_dump` to a private directory outside the Recovery Repository, verified by restoring it
  into a scratch database; restored into a new database that then takes the workspace
  database's name (the replaced one is kept). A restore is refused when a user was deleted
  after the point was taken; `user-erasure-run` runs after a restore. Points are deleted
  after 7 days.
- **Known-good rollback**: a failed migration, start or health check rolls back automatically
  (restore point, previous release, health check, then resume). If that fails too, the system
  stays in maintenance and the configured notify commands run; `paw-release end-maintenance`
  resumes after a manual fix. `paw-release rollback` goes back to a known-good release by hand.

### Deploy checklist (audit retention timer, Decision 0031)

Deploying (and every update's pre-check) includes the scheduled jobs whose failure would stop
the workspace later:

1. Install `paw-audit-retention.service`, `paw-audit-retention.timer` and
   `paw-audit-retention-failure.service` and run `systemctl enable --now paw-audit-retention.timer`
   (Decision 0031). Without it no `audit_events` partition is created and every audited action
   fails once the existing ones run out.
2. Likewise enable `paw-recovery-backup.timer` (Decision 0054), `paw-memory-projection.timer`
   and `paw-user-erasure.timer`.
3. Before each update, `paw-release precheck` must pass: it runs `audit-retention-check`'s
   coverage check (partitions to the end of next month) and checks that the timers are enabled.

## Open implementation choice

The concrete packaging/runtime mechanism (Docker, systemd units, release directories, etc.)
is not required to be frozen in requirements, as long as the operational requirements above are met.
