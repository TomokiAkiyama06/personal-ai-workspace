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
renamed, locked, or damaged its checkout.

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
- The candidate runs in its own session.  On timeout or cancellation the runner sends
  `SIGTERM` to the process group and to the processes found below the candidate in
  `/proc`.  The grace period (`term_grace_seconds`, 2 s) applies to all of them, not
  only the leader: the runner waits until the leader has exited *and* no group member
  or known descendant is still running, so a descendant finishing its `SIGTERM` handler
  is not cut short.  Whatever is left when the period ends gets `SIGKILL`, and reading
  the pipes stops after `drain_seconds` (1 s).  Leftover members of the candidate's
  process group are also killed when it exits normally.
- Not covered: a process that daemonizes (double fork plus `setsid`) is neither in the
  group nor below the candidate in `/proc`, so it can outlive the run.  Only a
  container or cgroup can contain that.

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

What remains: a candidate that guesses or lists `logs_directory` can still open the
log for writing as the same user, and changes made after the final append are not
detected.  Production must run candidates under a separate OS identity or inside a
container/mount namespace that cannot see the evaluator's runs, logs, and repository
storage.  The candidate can likewise modify the main repository's `.git`, which its
worktree shares.  See `docs/decisions/0001-hidden-check-boundary.md` (added by PAW-013)
for the same boundary applied to hidden checks.

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
