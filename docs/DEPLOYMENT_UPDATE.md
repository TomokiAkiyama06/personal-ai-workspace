# Deployment / Update / Rollback

更新日: 2026-10-16
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
  `FLASHINFER_EXTRA_CUDAFLAGS=-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK` for that runtime
  (the case with vLLM 0.30.0's wheels on the first deploy, 2026-10-16).
- The JIT runs `ninja` (from the venv) and `nvcc` (`$CUDA_HOME/bin`). A unit does not have
  the venv on `PATH`, so the first sampling fails with `No such file or directory: 'ninja'`
  (first deploy, 2026-10-16); put both directories on the unit's `PATH`.

[`apps/backend/deploy/systemd/paw-llm-main.service`](../apps/backend/deploy/systemd/paw-llm-main.service)
is an example unit with all of the above.

Model footprint (Decision 0039 1): give each `DeploymentSpec` a footprint (`gpu_bytes`,
the sum of its four parts) of the benchmark's measured peak, not
`gpu-memory-utilization` × the GPU: `max(GPU memory used during the run − used before
the load) + 2 GiB`. vLLM exceeded its `gpu-memory-utilization` budget by up to 8.4 GiB.

## Tool sandbox wrapper and `PAW_SANDBOX_OOM_STATUS` (Decision 0092, 5)

The server's SSH wrapper (`apps/backend/deploy/ssh-git-wrapper/paw_git_wrapper.py`) is
installed per workspace account and moves on its own, apart from the application. Its
`paw-sandbox/v2 exec` adds `oom=<0|1|->` to the status line; the backend sends `v2`
only with `PAW_SANDBOX_OOM_STATUS=true`. Order:

1. Replace the wrapper of **every** account first (an old backend keeps sending `v1`,
   which the new wrapper answers as before).
2. Then set `PAW_SANDBOX_OOM_STATUS=true` and restart the backend.
3. To roll back, set it back to `false` (or roll the backend back) **before** an old
   wrapper returns: an old wrapper refuses `v2` (`bad_protocol`) and the sandbox tools
   fail as unavailable, visibly, instead of missing an OOM kill.

Details: `apps/backend/deploy/sandbox/README.md` ("OOM の検出").

## Runtime configuration (`PAW_RUNTIME_CONFIG_FILE`, Decision 0093)

[Decision 0093](decisions/0093-production-runtime-config.md) (Approved, issue #208).
Without `PAW_RUNTIME_CONFIG_FILE` the backend builds only authentication, projects /
repositories and System Health: the scheduler's routes answer `503`
(`compute_not_configured`), no orchestrator or worker runs, and git is
`SubprocessGitRunner` (another account's checkout is refused as `identity_mismatch`).
The start logs one warning, `runtime configuration not set: ...`. The file
(`/etc/paw/runtime.toml`) turns the parts on; it is read once at start (restart after a
change, `paw-release` restarts too).

Files in `apps/backend/deploy/runtime/`:
[`runtime.example.toml`](../apps/backend/deploy/runtime/runtime.example.toml) (this host's
example, Decision 0093 4), [`paw-unit-pids`](../apps/backend/deploy/runtime/paw-unit-pids)
(the `pids` command; a stopped unit has no cgroup, so `cat .../cgroup.procs` would fail and the
scheduler would never start the model),
[`50-paw-llm-main.rules`](../apps/backend/deploy/runtime/50-paw-llm-main.rules) (polkit).

**Whoever can write the file runs commands as `paw`**, so the backend refuses to start
(exit code 2, `Invalid runtime configuration: <section>.<key>: <reason>`, no value) unless
the file is owned by root and not writable by its group or others, every directory up to
`/` is root's and not group/other-writable, and every command's `argv[0]` — each
symbolic link on its way, each link's directory, and a script's `#!` interpreter — is
too (no `/usr/bin/env`, a Python script needs `-I`). A shell or a launcher as `argv[0]`
(`sh`, `bash`, `env`, `sudo`, `python*`, ...), a URL that is not
`http://127.0.0.1:<port>/...` / `http://[::1]:<port>/...`, an unknown key, a value a
Decision fixed (headroom, timeouts, the 40 GiB host memory minimum, parallelism, ...),
`[orchestrator]` without `[compute]` + `[local_model]` + `[sandbox]` (or without
`PAW_DATABASE_URL`), and `[sandbox] runner = "ssh"` without `[git] runner = "ssh"` are
refused too. No part starts without the rest.

Install (as root; the agent never does this):

```sh
install -d -o root -g root -m 0755 /usr/local/lib/paw /etc/paw
install -o root -g root -m 0755 apps/backend/deploy/runtime/paw-unit-pids /usr/local/lib/paw/paw-unit-pids
install -o root -g root -m 0644 apps/backend/deploy/runtime/50-paw-llm-main.rules /etc/polkit-1/rules.d/50-paw-llm-main.rules
install -o root -g paw -m 0640 apps/backend/deploy/runtime/runtime.example.toml /etc/paw/runtime.toml
# Edit /etc/paw/runtime.toml: keep only the sections of the step below.
# /etc/paw/backend.env: PAW_RUNTIME_CONFIG_FILE=/etc/paw/runtime.toml
# As paw, with backend.env's settings ([orchestrator] needs PAW_DATABASE_URL; no connection is made):
systemd-run --uid=paw --pipe --wait --quiet -p EnvironmentFile=/etc/paw/backend.env \
  /opt/paw/current/venv/bin/python -m paw_backend.cli runtime-config-check /etc/paw/runtime.toml
systemctl restart paw-backend.service
```

Enable it in three steps, restarting and checking after each:

1. `[ssh]` + `[git] runner = "ssh"` + `[compute]` (the scheduler starts
   `paw-llm-main.service`; do not enable that unit).
2. `[local_model]` (the Inferred Preference interpreter asks the main model).
3. `[sandbox]` + `[orchestrator]` (the local agent runs task nodes). **The worker takes
   queued tasks at once** (`PAW_ORCHESTRATOR_WORKERS`, default 1): check the queue first,
   or start with `PAW_ORCHESTRATOR_WORKERS=0` to check the wiring only.

Checklist (Decision 0093 5; the human, not an agent):

- [ ] `ls -l /etc/paw/runtime.toml` is `root paw -rw-r-----`; `/etc/paw` is
  `root root drwxr-xr-x`.
- [ ] The polkit rule lets `paw` start / stop `paw-llm-main.service` only, and
  `sudo -u paw /usr/bin/systemctl show -p Result -p ActiveState -p InvocationID -- paw-llm-main.service`
  reads (Decision 0092 1).
- [ ] `sudo -u paw /usr/local/lib/paw/paw-unit-pids paw-llm-main.service` prints every
  vLLM pid while the unit runs, nothing with exit code 0 while it is stopped, and exits
  non-zero for a unit name that does not exist.
- [ ] `namei -l /usr/bin/systemctl /usr/local/lib/paw/paw-unit-pids /usr/bin/nvidia-smi /usr/bin/python3`:
  every step root's, not writable by group / others.
- [ ] `tomoki`'s `authorized_keys` has the wrapper's `command=`
  (`--sandbox-image=...@sha256:...`, `--sandbox-shim-base=/run/paw-sandbox`,
  `--sandbox-keep-groups=yes`) and `from="127.0.0.1"`; `/etc/paw/ssh-keys/tomoki.key`
  (owned by `paw`, `0600`); `/etc/paw/ssh_known_hosts` (Decision 0029,
  `apps/backend/deploy/ssh-git-wrapper/README.md`, `apps/backend/deploy/sandbox/README.md`).
- [ ] Each account's home and checkout root (issue #285; details in
  `apps/backend/deploy/ssh-git-wrapper/README.md`, "Home と `workspaces` の権限"):
  - the home is searchable by `paw` and by no other local account: an ACL entry
    `setfacl -m u:paw:x <home>`, not the other-execute bit (`o+x` / `0751` / `0711` lets
    every local user traverse to checkouts that `git clone` creates `0755` / `0644`);
    the mode stays `0750` / `0700`, not writable by group / others. The backend itself
    `lstat`s the checkout root and every checkout (`namei -l /home/tomoki/workspaces` as
    `paw` reaches the end; `getfacl -p` shows `user:paw:--x` with an effective `--x`);
  - `<home>/workspaces` exists, is owned by the account (`0700`) and is searchable by
    `paw` through the same ACL entry (the wrapper's root, `--root=`, refuses every call
    with `root_unavailable` while it is missing; a later `chmod` can change the ACL mask,
    so check again after one);
  - a repository registered as `既存のディレクトリ` lies below `<home>/workspaces`
    (the wrapper runs git only there; see the `PAW_REPOSITORY_EXISTING_ROOTS` item
    below), and every directory on the way is searchable by `paw`.
- [ ] `GitHub から Clone`, `新規（ローカル）` and `新規（GitHub）` under `git: ssh` create
  the checkout directory through the wrapper's `paw-dir/v1` as the account (Decision
  0097, issue #285): install the wrapper that knows `paw-dir/v1` before (or with) the
  backend that sends it; an older wrapper refuses it with `bad_protocol` (126) and the
  registration fails as `git_failed` without creating anything. Until both are in, clone
  by hand as the account from a neutral working directory and register the path as
  `既存のディレクトリ`: `cd / && sudo -u <user> git clone <url> <home>/workspaces/<dir>/<repo>`
  (`<dir>` is any directory below `workspaces` that is not `.paw-worktrees`).
- [ ] `/etc/paw/backend.env` has `PAW_REPOSITORY_EXISTING_ROOTS={home}/workspaces`
  (`{home}/<PAW_REPOSITORY_WORKSPACE_SUBDIR>` if that is changed). With
  `[git] runner = "ssh"` the wrapper runs git only below `<home>/workspaces` (its
  `--root=`), so the backend's default `{home}` would accept an existing repository
  elsewhere in the home that then fails at its first git command. The default stays
  `{home}` for the `subprocess` runner (Decision 0097 6, issue #285).
- [ ] `/usr/local/lib/paw/paw-git-wrapper` knows `paw-sandbox/v2` (the sandbox README's
  checklist item 9); otherwise `PAW_SANDBOX_OOM_STATUS=false`.
- [ ] `/run/paw-sandbox` (`paw:paw-sandbox 0711`, `tmpfiles.d`) and the group
  `paw-sandbox` (`paw`, `tomoki`).
- [ ] `paw-llm-main.service`'s `--port 18080` matches `base_url` / `metrics_url`,
  `--served-model-name main` matches `served_model`, and the footprint matches its
  `--gpu-memory-utilization` (the parser does not read the unit).
- [ ] `runtime-config-check` passes and lists the expected parts.
- [ ] After each step: `GET /api/v1/system/health` shows `compute`,
  `GET /api/v1/admin/compute/full-gpu` is not `503`, the main model shows as `gpu`.
  After step 2 the preference confirmation answers with the model. After step 3 a small
  task (a read-only look at a repository) runs on a local node and
  `agent_dag_node_attempts` records `local_gpu` — run it on the GPU only with the
  human's go-ahead, as for the GPU smoke (Decision 0083 9). Create that task through the
  product's entry point (issue #274), never by calling `TaskService` from a script (that
  skips the `project.task.run` check and its audit):
  1. With the human's go-ahead, set `PAW_ORCHESTRATOR_WORKERS=1` (step 3 runs with `0`)
     and restart the backend, with nothing else in the queue
     (`SELECT count(*) FROM queue_entries WHERE status IN ('queued', 'claimed')` is 0).
  2. Signed in as a Manager / Contributor of the project, open `エージェント / タスク` ›
     `新しいタスク` (`/agents`): the project, a title, an instruction to read and report
     (for example "README と docs/ の構成を読み、5 行でまとめてください。"), check
     `読み取りだけ`, the repository under `読むリポジトリ`, the budget `標準`. The same is
     `POST /api/v1/projects/{project_id}/tasks` with `{"title", "instruction",
     "repositories": [{"repository_id", "role": "referenced"}], "budget_preset":
     "standard", "read_only": true}`. The backend guarantees the task writes nothing
     (issue #279, Decision 0094 1 A): its repository is only `referenced`, a plan with a
     `worker` node is refused (the next planner is asked) and no node gets a capability
     or a tool that writes.
  3. The task goes from `キュー待ち` to running; then
     `SELECT placement, placement_agent, placement_model FROM agent_dag_node_attempts a
     JOIN agent_dags d ON d.id = a.dag_id WHERE d.task_id = '<task id>'` shows `local_gpu`.
     When its DAG succeeds the task is `completed` (no evaluation, no pull request), and
     the summary of each node is kept in its `complete` event:
     `SELECT detail->'result' FROM task_events WHERE task_id = '<task id>' AND command =
     'complete'`. Nothing is left to cancel.

The CLI runtimes (`[agents.codex]` / `[agents.claude]`) cannot be enabled yet: the names
are reserved and refused until a `CloudPolicy` Decision and the CLI smoke (Decision
0093 3).

## Implementation (Issue #54, Decision 0079)

[Decision 0079](decisions/0079-deploy-update-rollback.md) (Approved 2026-10-08) decides the
mechanism. Operator steps are in the
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
  The Recovery projection is refreshed before the writers stop: the configured
  `recovery_backup` commands (the backup unit, which runs as the owner of the checkout and its
  deploy key), else `recovery-backup-run` as the `backend_prefix` user.
- **Backward-compatible migration**: a migration declares `paw_compatibility = "expand"` when
  the release before it runs on the new schema; a rollback over expand-only migrations keeps
  the database. Anything else (or no declaration) needs a restore point to roll back.
- **Restore point**: only for an update with a migration, after every writer stopped:
  `pg_dump` to a private directory outside the Recovery Repository, verified by restoring it
  into a scratch database; restored into a new database that then takes the workspace
  database's name (the replaced one is kept). A restore is refused when a user was deleted
  after the point was taken; `user-erasure-run` runs after a restore. Points are deleted
  after 7 days (after every update and rollback, and daily by `paw-release-prune.timer`).
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
2. Likewise enable `paw-memory-projection.timer` and `paw-user-erasure.timer`, and
   `paw-recovery-backup.timer` (Decision 0054) once the Recovery Repository (private repository,
   deploy key, checkout, `PAW_RECOVERY_REPOSITORY_DIR`) exists. Until then keep that timer out of
   the release configuration's `start` and `precheck` (`apps/backend/README.md`, "配置");
   `paw-release update` is refused until a recovery backup has succeeded.
3. Before each update, `paw-release precheck` must pass: it runs `audit-retention-check`'s
   coverage check (partitions to the end of next month) and checks that the timers are enabled.

## Open implementation choice

The concrete packaging/runtime mechanism (Docker, systemd units, release directories, etc.)
is not required to be frozen in requirements, as long as the operational requirements above are met.
