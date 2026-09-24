# Benchmark の準備領域

Benchmark Task の仕様、候補 Agent の比較、公開可能な評価用データを整備するためのディレクトリです。
[Task schema](schemas/task-v1.schema.json) とvalidatorはTaskの形式だけを検証し、Repository取得、
check実行、Hidden Test参照の解決、隔離実行は行いません。

実装順序は [Benchmark / Evaluator 設計](../docs/BENCHMARK_EVALUATOR.md) と
[Implementation Backlog](../docs/IMPLEMENTATION_BACKLOG.md) に従います。

- [PAW-010 — Benchmark Task Schema](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/7)：Task の機械可読な形式を定義する。PAW-001 に依存する。
- [PAW-011 — Evaluator Result Schema](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/8)：Evaluator の判定結果と計測値の形式を定義する。
- [PAW-012 — 隔離 Worktree Runner](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/9)：同一の開始状態から候補 Agent を実行する。
- [PAW-013 — Test / Hidden Acceptance Runner](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/10)：Visible TestとHidden Testを分離して実行する。
- [PAW-015 — Candidate Adapter Interface](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/12)：モデルや Runtime の違いを共通 Interface で扱う。
- [PAW-016 — Seed Benchmark Dataset](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/13)：仕様と検証可能な挙動に基づく初期評価セットを作る。

機械的な判定と結果の扱いについては [Evaluator](../evaluator/README.md) を参照してください。

実データを追加する際は [Security Policy](../SECURITY.md) に従います。
Private Dataset、実際の会話・Memory、Credential、Model Weights はこの Public Repository に保存しません。

## Task schema v1

Task JSONの必須fieldは次のとおりです。

| Field | 内容 | Candidateへの可視性 |
| --- | --- | --- |
| `schema_version` | Schema version。v1は`1.0` | 可視 |
| `task_id` | Taskを識別するID | 可視 |
| `kind` | `historical`、`spec`、`injected_bug`のいずれか | 可視 |
| `repository` | Credentialを含まないRepository locatorとstarting commit。known-good commitは任意 | 可視 |
| `issue_text` | Candidateへ与える課題 | 可視 |
| `visible_checks` | Candidateへ公開するcheckのID、種別、argv形式のcommand | 可視 |
| `hidden_checks` | Hidden checkのID、種別、opaqueな`reference_id`だけ | Evaluator metadata |

`visible_checks`と`hidden_checks`は空配列を許容します。Hidden Test本文、command、path、Credentialを
`hidden_checks`へ保存してはいけません。`reference_id`の保存先や解決方法、Candidateからの隔離方法は
PAW-013で定義します。Schema validationはlocatorやcommitの存在確認、credentialの検出を行いません。
Task authorは[Security Policy](../SECURITY.md)に従い、credentialをlocatorへ保存してはいけません。

## Isolated worktree runner

`benchmarks.worktree_runner.WorktreeRunner` is evaluator infrastructure for starting a
candidate process from a specified commit.  Each `create()` call resolves the commit,
creates a detached worktree below an evaluator-owned runs directory, and appends a
JSONL lifecycle log (every record carries `run_id`, `candidate_id` and `commit`) to a
separate logs directory.  `execute()` removes the worktree after a normal exit,
timeout, or cancellation; its log remains available for audit.  Cleanup also removes
the run directory and Git's `.git/worktrees/<id>` record when a candidate deleted,
renamed, locked, or damaged its checkout.  If the candidate renamed or moved the run
directory itself, cleanup follows it: a directory handle opened at creation is resolved
through `/proc/self/fd`, the directory's identity (`st_dev`, `st_ino`) is verified (the
`/proc` text is not trusted: a live directory named `x (deleted)` reads like a deleted
one), and it is removed wherever it now is (a symlink planted at the old path is unlinked, never
followed).  When the directory cannot be found (no `/proc`) or removed, the log records
`cleanup_incomplete` instead of `cleanup_finished` and `cleanup()`/`execute()` raise
`WorktreeRunnerError`, after removing what could be removed and pruning Git's record.

The runner does not execute visible or hidden checks and does not select a model.  It
does not persist command text, stdout, or stderr because those fields can contain
credentials.  It records only lifecycle events, exit status, duration, and byte counts:
the final event of an invocation (`completed`, `timed_out`, `cancelled` or
`launch_failed`) carries `exit_code`, `duration_ms`, `stdout_bytes` and `stderr_bytes`,
the same numbers `execute()` returns.  Output is read and discarded while the process
runs, so memory use does not depend on how much a candidate prints.  PAW-013 owns check execution and hidden-test isolation.

`starting_commit` must be a plain revision (letters, digits and `._/@~^{}-`, not
starting with `-`); it is passed to `git rev-parse --verify --end-of-options`, which
needs Git 2.30 or newer.  `candidate_id` is limited to letters, digits, `.`, `_`, `-`.
`execute()` needs a non-empty `argv[0]`; later arguments may be any string (including
`""`).

`runs_directory` and `logs_directory` must resolve, after symlinks, outside the source
repository (its worktree root, the main worktree of a linked worktree, and `.git`);
otherwise the constructor raises `ValueError`, so retained logs never dirty the checkout
being benchmarked.

### Candidate process containment

- The candidate gets an explicit environment: only `PATH`, `LANG`, `LANGUAGE`,
  `LC_ALL`, `LC_CTYPE` and `TZ` are inherited, plus a per-run `HOME`.  Credential
  variables and every `GIT_*` selector are dropped.
- The runner's own Git commands (`worktree add`, `rev-parse`, ...) get an allowlist too:
  the six variables above plus the operator's `HOME`, so Git configuration such as
  `safe.directory` still applies, and no credentials.  They also run with
  `core.hooksPath=/dev/null`.  The candidate shares the repository's Git directory, so
  without this a candidate could leave a `post-checkout` hook that the next
  `git worktree add` starts with the evaluator's credentials.
- The candidate runs in its own session.  On timeout or cancellation the runner sends
  `SIGTERM` to the process group and to the processes found below the candidate in
  `/proc`.  The grace period (`term_grace_seconds`, 2 s) applies to all of them, not
  only the leader: the runner waits until the leader has exited *and* no group member
  or known descendant is still running, so a descendant finishing its `SIGTERM` handler
  is not cut short.  Output keeps being drained during the grace period, so a handler
  that writes more than a pipe holds is not blocked (and killed) on a full pipe.  Whatever is left when the period ends gets `SIGKILL`, and reading
  the pipes stops after `drain_seconds` (1 s).  Leftover members of the candidate's
  process group are also killed when it exits normally.
- Descendants are tracked by identity, not by pid alone (pid plus the start time in
  `/proc/<pid>/stat`).  Before every `SIGTERM`/`SIGKILL` the identity is re-checked, and
  a pid now held by a different process is dropped and neither signalled nor waited
  for.  Where `pidfd_open` exists (Linux 5.3+, Python 3.9+) the pidfd is opened first
  and the identity checked afterwards, so the signal cannot reach a newcomer; without
  it a window of microseconds remains between the check and `kill`.  The leader is left
  unreaped until the last process-group signal, so the group id cannot be reused before
  it.
- If something else reaps the leader (the evaluator ignores `SIGCHLD`, or another
  reaper collects it), its exit status is lost: `execute()` returns `exit_code=None`
  (and logs `null`) instead of the `0` that `Popen` would report.  The runner reaps the
  leader itself, so it notices a reaper that got there first at any point, and it
  re-checks at every group signal that the leader is still its own unreaped child (pid
  and start time).  Once it is not, its pid may be reused as an unrelated process group
  id, so the group is signalled only while a process recorded earlier as its member
  (same pid and start time) is still in it; otherwise nothing is sent.  Members are
  recorded every 0.2 s while the leader runs and once more at the moment it is seen to
  have vanished, so a child forked just before the exit is still known.  (Git children
  inherit an ignored `SIGCHLD` and fail, so an evaluator that ignores it cannot use the
  runner's Git operations at all.)
- **Documented residuals** of signalling a same-user candidate from the same UID, none of
  which can be closed in-process (see `docs/decisions/0001-hidden-check-boundary.md`,
  added by PAW-013):
  - A process that daemonizes (double fork plus `setsid`) is neither in the group nor
    below the candidate in `/proc`, so it can outlive the run.
  - A check-then-signal gap of microseconds remains, because a process group cannot be
    signalled through a pidfd: after the last identity check a reaper could still free
    the group id before `killpg` runs.  The last look at the group assumes the id was
    not reused within one polling interval (50 ms) of the leader vanishing.
  - A member forked into the group only after the leader was reaped, by members that
    have all exited before the signal, is not recorded and survives.
  - The shared Git directory is writable by the candidate.  Hooks are disabled and the
    Git commands carry no credentials, but a candidate can still write other
    configuration there (a `filter.*` or `core.fsmonitor` setting, an alias) that makes a
    later Git command run a program as the evaluator's user and with its `HOME`.
    Production must run candidates under another OS user or in a container, with the
    repository mounted read-only.
  - Containing descendants reliably needs a PID namespace or a cgroup (`cgroup.kill`);
    production must run candidates in one.

### Same-user candidates and the evaluator process

A candidate runs as the evaluator's OS user.  On Linux any such process can read
`/proc/<evaluator pid>/environ`, the environment the evaluator was *started* with (later
`os.environ` changes do not appear there), so credentials the evaluator inherited are
exposed even though the candidate's own environment is an allowlist.  `/proc/<pid>/mem`
was already refused in the tested configuration by `ptrace_scope=1`.

Mitigation: the constructor calls `prctl(PR_SET_DUMPABLE, 0)` on the evaluator (Linux,
best effort, `harden_process=False` opts out; `runner.process_hardened` reports the
result).  Observed on Linux 7.0 as an unprivileged user: the candidate's reads of the
evaluator's `environ` and `mem` fail with `EACCES`, the candidate itself is dumpable
again after `exec`, and the evaluator stays non-dumpable.  Side effects: no core dumps,
no debugger attach (`gdb`, `py-spy`), and the evaluator cannot read its own
`/proc/self/environ` (`/proc/self/fd`, `status` and `cmdline` stay readable).  The flag
is process-wide, so a host application that needs core dumps or a debugger should opt
out and provide isolation another way.

This does **not** close the exposure:

- **Ancestors.**  The shell or service manager that started the evaluator still has the
  same environment, and its `/proc/<pid>/environ` stays readable to a same-user
  candidate (it finds the parent pid in the evaluator's world-readable
  `/proc/<pid>/stat`).  Start the evaluator from a clean environment
  (`env -i PATH=... python ...`, or a service unit that passes no secrets).
- **Root candidates** and any process with `CAP_SYS_PTRACE` or `CAP_DAC_READ_SEARCH`
  ignore the flag.
- **Other same-user channels**: the real home directory (`~/.ssh`, `~/.aws`, ...) by
  absolute path, the user's keyrings, `ssh-agent`/D-Bus/X11 sockets, the evaluator's
  command line (world-readable `/proc/<pid>/cmdline`, so never pass secrets as
  arguments), files the evaluator wrote, and every other process of the same user.
- Where the flag is unavailable (non-Linux, or a refused `prctl`) nothing changes.

Closing these needs a separate OS identity, or a PID namespace / container in which the
evaluator's processes and files do not exist.  Unprivileged user namespaces are not
always permitted (they are refused on the machine used to test this), so that must be
provided by the deployment, not by this module.  See
`docs/decisions/0001-hidden-check-boundary.md` (added by PAW-013) for the same boundary
applied to hidden checks.

### Lifecycle log limits

A candidate runs under the same OS user as the evaluator, so **in-process code cannot
make the lifecycle log tamper-proof**.  What the runner does:

- The log lives in `logs_directory` (default `<runs_directory>-logs`), not inside the
  run directory, so it is not reachable as `../execution.jsonl` from the checkout and
  is outside the checkout's parent chain.  Directories are created `0700` and log
  files `0600` at creation time; an existing state directory that is not owned by the
  evaluator or is group/other-writable is refused.
- The log is opened with `O_NOFOLLOW` (and `O_EXCL` on creation) and never `chmod`ed
  by path, so a planted symlink is neither followed nor used to change another file's
  mode.
- Before every append the runner checks that the file is the same regular,
  single-link, evaluator-owned, owner-only file of the expected size.  Replacement,
  truncation, extra appended lines, and extra hard links raise `WorktreeRunnerError`
  (checkout removal still completes).
- A write or a `close(2)` that fails (a full disk, an I/O error; `close` can report a
  delayed write error) is reported as `WorktreeRunnerError` too, without the operating system's message, so `cleanup()` still removes the
  checkout, its Git metadata and the in-memory ownership before it raises.  The failed
  append leaves the file at an unexpected size, so later appends are refused by the
  integrity check instead of writing after a torn record.
  If only the record of the outcome (`completed` / `timed_out` / `cancelled`) cannot be
  written, cleanup still runs and `execute()` then raises that `WorktreeRunnerError`
  instead of returning a result the durable log does not contain.
- If the output drain cannot be set up after the launch (descriptor exhaustion), or the
  supervisor cannot be built, the child is killed and reaped before the worktree is
  removed, instead of running on without a timeout.
- A removal that fails for a filesystem reason (a Git metadata entry replaced by a
  plain file is simply removed; a permission error is not) is reported as
  `cleanup_incomplete` and `WorktreeRunnerError`, never as a raw `OSError`.
  An entry that cannot be looked at (a candidate removed the search permission of
  `.git/worktrees`) is not treated as gone: only `ENOENT` / `ENOTDIR` mean absent, any
  other error makes the cleanup incomplete.

What remains: a candidate that guesses or lists `logs_directory` can still open the
log for writing as the same user, and changes made after the final append are not
detected.  Production must run candidates under a separate OS identity or inside a
container/mount namespace that cannot see the evaluator's runs, logs, and repository
storage.  The candidate can likewise modify the main repository's `.git`, which its
worktree shares.  See `docs/decisions/0001-hidden-check-boundary.md` (added by PAW-013)
for the same boundary applied to hidden checks.

## Test / hidden acceptance runner

`benchmarks.test_runner.TestRunner` executes visible checks supplied by the task and
hidden checks resolved from an evaluator-owned `HiddenCheckRegistry`.  The manifest
continues to contain only the opaque `reference_id`; the registry and its command
content are never placed in the candidate worktree or durable check log.  A check
record includes its status, timeout, exit code, duration, and, per stream, the byte
count, the SHA-256 of the retained bytes, and a truncation flag.  Raw stdout/stderr
stay in memory for the trusted evaluator caller, so credentials emitted by a command
do not become durable logs.

- Output is read while the check runs.  Only the first 64 KiB per stream is kept;
  the rest is counted and discarded, so evaluator memory does not depend on how much
  a check prints.  `truncated` is set when a stream exceeded 64 KiB.
- A check gets an explicit environment: only `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`,
  `LC_CTYPE` and `TZ` are inherited, plus a private temporary `HOME` removed after the
  check.  Credential variables and every `GIT_*` selector are dropped.
- On timeout the check's process group and the processes found below it in `/proc`
  get `SIGTERM`.  The grace period (`term_grace_seconds`, 2 s) applies to all of them,
  not only the leader: the runner waits until the leader has exited and no group member
  or known descendant is still running, so a descendant finishing its `SIGTERM` handler
  is not cut short.  Output keeps being drained during the grace period, so a handler
  that writes more than a pipe holds is not blocked (and killed) on a full pipe.
  Whatever remains when it ends gets `SIGKILL`, and pipe reading stops after
  `drain_seconds` (1 s).  Leftover group members are also killed when a
  check exits normally.  A process that daemonizes (double fork plus `setsid`) cannot
  be found and can outlive the check; only a container or cgroup contains that.
  If the output capture cannot be set up after the launch (descriptor exhaustion), the
  child is killed and reaped and the check is reported as an `error`, not left running.
  The child's pid and start time are recorded right after the launch, and nothing is
  signalled unless it is still the runner's own unreaped child (`waitid` with
  `WNOWAIT`) with that start time: a child that something else already reaped may have
  had its pid, which is also its process group id, reused, so then nothing is sent and
  nothing is waited for.  Once the leader object exists its guarded signalling
  (recorded members, identity re-check at every signal) is used instead.
- Descendants are tracked by identity, not by pid alone (pid plus the start time in
  `/proc/<pid>/stat`).  Before every `SIGTERM`/`SIGKILL` the identity is re-checked, and
  a pid now held by a different process is dropped and neither signalled nor waited
  for.  Where `pidfd_open` exists (Linux 5.3+, Python 3.9+) the pidfd is opened first
  and the identity checked afterwards, so the signal cannot reach a newcomer; without
  it a window of microseconds remains between the check and `kill`.  The check's
  leader is left unreaped until the last process-group signal, so the group id cannot
  be reused before it.
- If something else reaps the check's leader (the evaluator ignores `SIGCHLD`, or
  another reaper collects it), its exit status is lost, so the check is reported as
  `error` with no exit code, never as `passed`.  The runner reaps the leader itself, so
  it notices a reaper that got there first at any point, and it re-checks at every group
  signal that the leader is still its own unreaped child (pid and start time).  Once it
  is not, its pid may be reused as an unrelated process group id, so the group is
  signalled only while a process recorded earlier as its member (same pid and start
  time) is still in it; otherwise nothing is sent.  Members are recorded every 0.2 s
  while the leader runs, once more when it is first seen as a zombie (which still
  reserves the group id), and once more at the moment it is seen to have vanished, so a
  child forked just before the exit is still known even if another reaper collects the
  leader before the final group kill.
- **Documented residuals** (not closable in-process; see Decision 0001): a process that
  daemonizes (double fork plus `setsid`) can outlive the check; a check-then-signal gap
  of microseconds remains because a process group cannot be signalled through a pidfd,
  and the last look at the group assumes its id was not reused within one polling
  interval (50 ms) of the leader vanishing; a member forked into the group only after
  the leader was reaped, by members that have all exited before the signal, is missed.
  Containing descendants reliably needs a PID namespace or a cgroup (`cgroup.kill`).
- A check command needs a non-empty `argv[0]`; later arguments may be any string,
  including `""` (for example `("python3", "-c", "")`), as the task schema allows.
- The check log directory is created `0700` and the log `0600` at creation, opened
  with `O_NOFOLLOW`, and refused (`TestRunnerError`) if it is a symlink, has extra
  hard links, is not owned by the evaluator, or sits in a group/other-writable
  directory.  A wider mode on an existing log is tightened on the open descriptor
  before anything is written.
- An unknown hidden reference raises a `KeyError` that carries neither the reference
  nor a chained exception (`__cause__` and `__context__` are `None`).

The runner is a data/process boundary, not a hostile-code sandbox: checks run under
the evaluator's own OS user, so code under test can still open evaluator files it can
locate.  Production must run candidate code and private evaluator storage under
separately enforced OS or container permissions.  The proposed boundary is recorded in
[Decision 0001](../docs/decisions/0001-hidden-check-boundary.md).

## Candidate adapter interface

[`candidate_adapter.py`](candidate_adapter.py) defines the provider-neutral boundary used by
future Local, Codex, and Claude candidate implementations. A request fixes the system/task
prompt, JSON Schema tool declarations, and token limits. The Backend supplies a one-attempt
timeout and cancellation token, and owns retry decisions through `RetryPolicy`; adapters must
never retry internally.

The interface contains only public candidate identity fields. Credentials, provider clients,
raw provider errors, runtime execution, and worktree/test-runner behavior are intentionally
outside this module. Concrete adapters must obtain credentials through a private Backend
dependency and convert errors to `CandidateErrorCode` values; raw provider errors
must remain private. This benchmark-only `CandidateAdapter` contract is separate
from the production [Agent Adapter](../docs/ARCHITECTURE.md#7-agent-adapter).

`ToolDefinition.input_schema` is a validated, deeply read-only snapshot, so one request can be
reused across candidates without any of them changing another's tools. It is deliberately not
JSON-serializable itself: adapters build provider payloads from
`ToolDefinition.input_schema_as_dict()` (a fresh plain `dict`/`list` copy on every call) or
`ToolDefinition.to_dict()` (`name`, `description`, `input_schema`), both accepted by
`json.dumps`. Mutating those copies never affects the snapshot. `copy.deepcopy` and `pickle`
of a tool or request work and re-validate the copy; `dataclasses.asdict` is not supported for
tools and raises `TypeError`, so use `to_dict()` instead.

## Validator

standalone CLIはprojectの安定したvirtual environmentへCI依存を導入して実行します。
pre-commitのhook環境は別環境であり、standalone CLIからは利用しません。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r .github/requirements-ci.txt
.venv/bin/python -m benchmarks.validate_task benchmarks/tests/fixtures/task-schema/valid/spec.json
```

複数fileを一度に指定できます。すべてvalidなら終了code 0、入力が不正なら1、bundled schemaを
利用できない場合は2を返します。エラーはJSON pathと理由を表示し、拒否した入力値は表示しません。

## Evaluator Result schema v1

Result JSONは、Evaluator version、Task ID、Candidateのmodel/runtime/quantization、`FAIL_TO_PASS`と
`PASS_TO_PASS`、各Evaluator checkの結果、計測値を保存します。checkの種別は`build`、`syntax`、`unit`、
`integration`、`lint`、`type`、`regression`、`acceptance`、`security`、`forbidden_changes`です。statusは
`passed`、`failed`、`not_run`、`error`で表します。`check_results[].id`は対応するTaskの
`visible_checks`または`hidden_checks`の`id`を使い、Result内で重複してはいけません。

`metrics`では、取得できた場合に次を保存できます。durationの単位はミリ秒、VRAMはbytesです。

| Field | 内容 |
| --- | --- |
| `wall_clock_ms` | 実行時間 |
| `token_count` | token数 |
| `agent_steps` | Agent step数 |
| `diff_size` | 変更行数 |
| `tool_failures` | Tool失敗回数 |
| `peak_vram_bytes` | Peak VRAM |
| `human_correction_ms` | 人間による修正時間 |

Runtimeで取得できないmetricは、`metrics`から省略できます。これは計測不能と0を区別するためです。
Result schema validatorもTask schema validatorと同じ終了code・値を出力しないエラー方針を使います。
量子化をしないCandidateは`quantization`へ`none`を、Runtime側で詳細を公開しないCandidateは
`provider-managed`を記録します。

```bash
.venv/bin/python -m benchmarks.validate_result benchmarks/tests/fixtures/result-schema/valid/complete.json
```
