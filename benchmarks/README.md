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
creates a detached worktree below an evaluator-owned runs directory, and retains a
JSONL lifecycle log outside the worktree.  `execute()` removes that worktree after a
normal exit, timeout, or cancellation; its log remains available for audit.

The runner does not execute visible or hidden checks and does not select a model.  It
does not persist command text, stdout, or stderr because those fields can contain
credentials.  It records only lifecycle events, exit status, duration, and byte counts.
PAW-013 owns check execution and hidden-test isolation.

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
  is not cut short.  Whatever remains when it ends gets `SIGKILL`, and pipe reading
  stops after `drain_seconds` (1 s).  Leftover group members are also killed when a
  check exits normally.  A process that daemonizes (double fork plus `setsid`) cannot
  be found and can outlive the check; only a container or cgroup contains that.
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
  `error` with no exit code, never as `passed`.  Its pid may also be reused as an
  unrelated process group id, so the group is signalled only while a process recorded
  earlier as its member (same pid and start time) is still in it; otherwise nothing is
  sent.  Members are recorded every 0.2 s while the leader runs.
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
