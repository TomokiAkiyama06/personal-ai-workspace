# Orchestrator の実 Runtime Adapter（Local の Main Model on vLLM・Codex CLI・Claude Code CLI）の設計（境界、Local の Tool loop、Reasoning の履歴の差し込み口、使用量の報告、OOM と Error の分類、Credential と Sandbox、承認、Timeout と取り消し、Test、PR の分け方）

- Status: Proposed
- Date: 2026-10-08
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208)。この Decision は設計だけで、実装は承認の後に下の 11 の段階ごとの PR で行う。関係する既存の口: `apps/backend/paw_backend/orchestrator/runtime.py`（`AgentRuntime`・`NodeAssignment`・`NodeOutcome`・`NodeTools`・`NodeBudget`・`NodePlacement`）、`orchestrator/orchestrator.py`（`_retry_step`）、`compute/runtimes.py`（`HybridRuntime`）、`compute/wiring.py`（`LocalRuntime`）、`orchestrator/composition.py`（`local_runtimes`・`agent_runtimes`）、`orchestrator/errors.py`（`RUNTIME_ERROR_CLASSES`・`AGENT_OUT_OF_MEMORY`）、`orchestrator/workspaces.py`（`NodeWorktree`）、`integration/coordinator.py`（`GitWorktreeCoordinator`）、`connections/`（`ConnectionAdapter`・`ConnectionService.execute`）、`tools/`（Tool Broker・`ToolRunner`・Registry）、`apps/backend/deploy/systemd/paw-llm-main.service`
- Supersedes（一部）:
  - [Decision 0016](0016-shared-connection-adapter-policy.md)（Approved）の 5 のうち「`tokens` と `runtime_seconds` は呼び出しが終わってから分かるので、終わった呼び出しの分だけを数える」「返さない呼び出しは 0」を、CLI の Session（下の 1 の `execute_session`）に限って下の 5 で置き換える（Session の途中で Turn ごとに計上する。数が分からない Session は 0 ではなく見積もり）。1 回の Prompt の呼び出し（`execute`）の数え方は変えない。
  - Decision 0016 の 7（Credential の置き換えは `admin.config.manage`）に、Backend の内部だけが行う「同じ Credential の OAuth の Refresh の書き戻し」（`rotate_credential`。Actor なし、`check_health` と同じ扱い）を下の 6 で足す。Admin による置き換えの規則は変えない。
  - [Decision 0021](0021-dag-orchestrator-policy.md)（Approved）の 3 に、`NodeOutcome` の `escalate`（この段では再試行せず、上の段があれば Escalation する）を下の 4 で足す。`retryable=False` の意味は変えない。
  - 次は書き換えず、その中で Runtime を作る: Decision 0021 のその他（Runtime の Protocol・Error Class・Node の Timeout）、Decision 0016 のその他（共有 Connection・Credential・Quota）、[Decision 0006](0006-tool-broker-policy.md)（Tool Broker）、[Decision 0036](0036-parallel-worktree-integration.md) の 7（Worker が Commit し、Backend は自動で Commit しない）、[Decision 0037](0037-gpu-compute-scheduler.md) の 14（`CloudPolicy` がなければ Cloud に送らない）、[Decision 0046](0046-tool-call-lease-fencing.md) / [Decision 0057](0057-connection-and-node-charge-lease-fencing.md)（Fencing）、[Decision 0047](0047-task-execution-composition-and-task-end-effects.md) の 5（本番の Runtime はまだない）、[Decision 0048](0048-node-placement-audit.md)（Placement の記録）、[Decision 0058](0058-compute-scheduler-application-wiring.md) の 7（`local_runtimes`）、[Decision 0071](0071-agent-oom-and-escalation-records.md)（`AgentOutOfMemory`）、[Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md)（Main は Qwen3.8-27B-FP8）、[Decision 0077](0077-local-usage-gpu-time-and-escalation-records.md)（Approved。Local の使用量の記録）。Reasoning の履歴の方針そのものは #200 の Decision（0076 を予約済み）に委ねる

## 背景

Orchestrator（PAW-034）は Node の 1 回の試行を `AgentRuntime.run_node(NodeAssignment) -> NodeOutcome` で動かす。周りの口はそろっている。

- `HybridRuntime`（PAW-036）が Local の Lease を取り、Lease を持った時間を `GPU_SECONDS` として Task の Budget に計上し、Placement（Local の GPU / CPU・Cloud）を走らせる **前に** 記録する（Decision 0048）。Cloud の Runtime（`cloud`）と `CloudPolicy` を渡せば、Local が混んでいるときに Cloud へ振り替えられる。Local の実行は Decision 0077（Approved）で `local_usage` に記録される。
- Runtime は `NodeTools.call`（Tool Broker を通る Tool 呼び出し）と `NodeBudget.charge`（`TOKENS` / `GPU_SECONDS` の報告）しか持たない。Broker・DB 接続・Grant・`TaskContext`・Queue の Lease・他の Node の会話は渡されない。
- 書き込む Worker の Node には専用の Worktree（`NodeAssignment.worktrees`、PAW-035）があり、Runtime はそこに Commit する。
- Runtime が名乗れる失敗は閉じた一覧（`RUNTIME_ERROR_CLASSES`）だけで、OOM は `AgentOutOfMemory`（Decision 0071）。失敗の文は Hash だけが Loop 検知に使われ、保存されない。`retryable=False` の失敗は、Orchestrator（`_retry_step`）が Escalation を考える前に諦める。
- Codex / Claude は共有 Connection（PAW-030）の `ConnectionService.execute` を通る（Admission・Quota・使用量の行・Task の Budget への `TOKENS` の計上・`Secret` は 1 回の呼び出しの間だけ）。ただし `ConnectionAdapter.run` は「1 回の Prompt → 1 つの文」の形で、Worktree や Tool を持たず、Token は呼び出しが終わってから計上される。Adapter の実装は Repository に 1 つもない。
- Main の Coding Model は Qwen3.8-27B-FP8（Decision 0074）。その選定の数値は、Repository の外の paw-bench の Coding Harness（vLLM の OpenAI 互換の `/v1/chat/completions`、`bash` / `str_replace` / `write_file` / `submit` の 4 つの Tool、60 Step・45 分・Prompt 120k token・応答 16,384 token、Reasoning を履歴に残す、Docker の `--network none` の Sandbox に Repository の完全な Clone）で測った。

足りないもの:

1. 本番の `AgentRuntime` が 1 つもない（Decision 0047 の 5）。Orchestrator は組み立てられず、Queue を Claim する Worker も起動しない。
2. Tool Broker の Registry は Working Set の Tool だけで、ファイル・Shell・Git の Tool の Executor がない（Decision 0047 の 5）。Local の Model が Worktree を編集する手段がない。
3. #183（PR #195）は OOM の報告の口と記録だけで、実際に CUDA OOM / OOM Killer を見分ける Adapter がない。
4. #200 が求める「古い Reasoning を履歴から外す」は Adapter 側の処理で、入れる場所がない。
5. #38（PR #205）の自由入力の構造化は Model の Interpreter の Port だけで、本番の配線がない。
6. 本番の `paw-llm-main.service` は `vllm serve /srv/models/main --served-model-name main` だけで、Benchmark の `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を付けていない（`--max-model-len`・`--gpu-memory-utilization` も付けていない）。このままでは Tool 呼び出しと Reasoning が Benchmark と同じ形で返らない。

**次のことは要件も既存の Decision も決めていない。** 3 つの Runtime の境界と置き場所、Local の Tool loop を Benchmark の Harness からどう作るか、Reasoning の履歴の方針をどこに差し込むか、Token の数え方と誰が Budget に報告するか、Error の分類と Escalation、CLI の Credential と Sandbox、承認が要る Tool 呼び出しの扱い、Timeout と取り消し、Test の方針、PR の分け方。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、推奨をここに置いて承認を求める。CLI の Option・認証の形などの最新の仕様は、AGENTS.md の 10 に従い、実装の PR でその時点の公式の情報を確かめてから使う（ここに書いた Flag 名は確認前の見込み）。

## 提案

### 1. Adapter の境界と置き場所

- **推奨:** 新しい Package `paw_backend/agents/` に、`AgentRuntime` を満たす 3 つの Runtime と、それらが共有する部品を置く。
  - `LocalAgentRuntime`: Local の Main Model（vLLM の OpenAI 互換 API）で Tool loop を回す。組み立ては今の `local_runtimes`（`LocalRuntime(runtime=..., deployment="main", local_model=...)`）に渡し、`HybridRuntime` が包む（Lease・GPU 時間・Placement・`local_usage` は `HybridRuntime` の仕事のまま。Adapter は GPU 時間を報告しない）。Planner の Local の呼び出しも同じく `HybridRuntime` を通る。
  - `CodexCliRuntime` / `ClaudeCliRuntime`: CLI を 1 回の Node の試行として動かす。Worker・Researcher・Reviewer の Ladder の上の段（`agent_runtimes` に Label で渡す）と、`HybridRuntime` の `cloud`（Local が混んでいるときの振り替え）で使える。
    - **Planner の Ladder には置かない。** Planner の呼び出しには `placement` がなく、外部送信を記録できないので、Cloud には送れない（`HybridRuntime` と同じ fail-closed）。組み立てが Planner の Ladder に CLI の Label があれば起動時に拒否する。
    - **`CloudPolicy` が注入されていない間は、CLI の段を Ladder に置けない。** 今の組み立ては `CloudPolicy` を注入しない（Decision 0037 の 14）ので、置いても毎回 Gate で拒まれ、Escalation が試行を使い切って `WAIT_FOR_USER` になるだけである。組み立てが起動時に拒否する。CLI の段が実際に動くのは、`CloudPolicy` の本番の実装が承認されて注入された後になる（その中身は後続の Decision）。
    - **外部へ送る順序（`CloudGate`）**: `CloudPolicy.allows`（Task の依存・権限・Quota による外部送信の可否）→ `ConnectionService` の Admission（Credential・Quota・Lease・下の 5 の Token の最低の枠）→ `placement.record(CLOUD, agent="codex" | "claude", model=...)`（Decision 0048。Audit を含む）→ Adapter の起動。どれかが通らなければ何も送らず、Placement も記録しない。`execute_session` は Admission の後・Adapter を呼ぶ前に、渡された Gate（`placement.record` を呼ぶ口）を呼ぶ。Gate が失敗したら、Admission で開いた使用量の行は送らずに `cancelled` で閉じる。この場合も、Decision 0016 の 5 のとおり `requests`（Admission を通った呼び出し、取り消しも数える）に 1 回が数えられる。Placement の記録が失敗するのは DB の失敗か試行が止まったときだけで、数え過ぎ（多めの側）なので、そのまま受け入れる。
    - Ladder から直接呼ばれたときも、CLI の Runtime 自身がこの Gate を通す（Placement と Audit だけでは外部送信の許可にならない）。今の `HybridRuntime` は Cloud の Runtime を呼ぶ前に自分で Placement を記録するので、CLI の Runtime を `cloud` に渡すときは記録をこの Gate に任せる（`HybridRuntime` は `CloudPolicy` を確かめて CLI の Runtime を呼ぶだけにする。11 の 4 の PR で `HybridRuntime` を変える）。
    - Gate で拒まれたときの `error_class` は、閉じた一覧にある `ConnectionError`（理由ごとの固定の文。下の 4）。`HybridRuntime` が今使う `ComputeUnavailable` は一覧にないので `AdapterError` として記録されている（既存の挙動。この Decision では変えない）。
  - 共有部品: `ChatCompletionsClient`（vLLM への 1 回の呼び出し。httpx の非同期 Client）、`ErrorClassifier`（4）、`ReasoningHistoryPolicy`（3）、`NodeResult` の組み立て（10）。`ChatCompletionsClient` は Compute の Lease の内側でだけ使う: Node と Planner は `HybridRuntime` の Lease の中、#38 の Interpreter（Task の外の 1 回の呼び出し）は `ScheduledMemoryWorker` と同じ形の包み（`ScheduledInterpreter`。`INTERACTIVE` の Lease を取り、取れなければ規則の Interpreter に戻る）で使う。Interpreter の呼び出しは Task に属さないので、Task の Budget と `local_usage`（Decision 0077 は Task の実行だけを記録する）には入らない。
  - **`submit` の経路**: Local では Adapter の内部の Tool。CLI では MCP の Bridge（6）が `submit` を PAW の Tool と並べて CLI に見せるが、`NodeTools.call` には渡さず、Bridge から CLI の Runtime へ直接渡す（Broker を通らない唯一の Tool。Worktree にも外部にも何もしない）。`AgentSessionResult` は最後の文の代わりに、受け取った `submit` の引数（無ければ「提出なし」）を持つ。検証と Commit（10）は Local と同じ処理。
    - Bridge は Tool 呼び出しを 1 つずつ順に実行する（CLI が並べて出した呼び出しも直列にする）。`submit` を受けた時点で以後の Tool 呼び出しをすべて拒否し（凍結）、すでに受け付けて実行中の呼び出しが終わるのを待ってから（8 の Tool の上限で打ち切り、打ち切ったら Container の中の Process を止めてから）検証する。合わなければ凍結を解いて差を `submit` の結果として返す。合えば Commit し、CLI を止め（8 の取り消しと同じ手順）、以後の `submit` も受けない（最初に受け付けた 1 回だけが結果）。Worktree を変えられるのは Bridge を通る呼び出しだけなので、凍結の間に Worktree が変わることはない。CLI が `submit` せずに終わったら `RuntimeError`（固定の文 `no_submission`）。
- CLI の 2 つは **`ConnectionService` を必ず通す**（Admission・Quota・使用量の行・Task の Budget の `TOKENS`・`Secret` の取り扱いを 1 か所に保つ）。今の `ConnectionAdapter.run(secret, AdapterRequest)` は Prompt → 文の 1 回の呼び出しなので、別の口 `AgentSessionAdapter.run_session(credential, AgentSessionRequest) -> AgentSessionResult`（`submit` の引数・入出力の Token の合計・終了の分類。`credential` は 6 の Proxy の口か `Secret`）と、`ConnectionService.execute_session` を足す。`AdapterRequest` の型は変えない。
  - `execute_session` は `execute` と同じ順で確かめて記録する。用途の Category は Node の Role から決める: Worker と Planner は `coding`、Reviewer は `review`、Researcher は `research`（Planner は上のとおり CLI に来ないが、対応は全 Role で決めておく）。
  - `AgentSessionRequest` には Turn ごとの使用量を Session の途中で渡す非同期の Callback（`on_usage(input_tokens, output_tokens)`）を持たせ、Adapter は CLI の機械読みの Stream で Turn が終わるたびにそれを呼ぶ（5 の途中の計上と停止はこの Callback の中で `ConnectionService` が行い、尽きたら Callback が停止を返して Adapter が CLI を止める）。`AdapterRegistry` は起動時に Callback を呼ぶ能力までは確かめられない（呼ぶかどうかは Adapter の実行時の振る舞い）。保証は、CLI の版ごとの Test（その版の実際の Stream の形を固定した Fake）と、実行時の検査（Turn の Event が来たのに `on_usage` が来ないまま次の Turn に進んだら、その Session を止めて失敗にする）による。
  - Runtime は `Principal`・`TaskContext`・Queue の Lease を持たない（`NodeAssignment` の外）ので、**Orchestrator が試行ごとに作る `NodeConnections`（`NodeTools` と同じ形の口）を `NodeAssignment.connections` として足し**、`execute_session(kind, request)` だけを見せる。中身は Orchestrator がその試行のために作った `TaskContext`（Run と、その時点の Queue の Lease）と作成者の `Principal` で、Runtime はそれを取り出せない。Lease を失った・Run が替わった試行の呼び出しは、Tool 呼び出しと同じく `NodeStopped` になる（Decision 0046 / 0057 の Fencing。`task_id` から Lease を作り直すことはしない）。
- 代替: (a) CLI の Runtime が `ConnectionService` を通らず直接 CLI を起動する（Quota・使用量・Credential の規則が 2 か所に分かれるので推奨しない）。(b) Local も `ConnectionAdapter`（`ConnectionKind` に `local` を足す）として作る（共有 Connection の Admission・Quota・Reaper は Local に当てはまらない。Decision 0077 の 1 の代替 (a) と同じ理由で推奨しない）。

### 2. Local の Tool loop: Benchmark の Harness の「振る舞い」を移し、実行は Tool Broker を通す

- **推奨:** paw-bench の `coding_harness.py` のコードはそのまま使わず（Repository の外・同期の urllib と Thread・Docker を直接呼ぶ）、`LocalAgentRuntime` に asyncio で書き直す。ただし **Model から見える形は Benchmark と同じにする**（Decision 0074 の数値はその形で測ったため）。
  - 同じにするもの: System Prompt の構成（Worktree の場所の言い換えだけ変える）、Tool の名前と引数（`bash`・`str_replace`・`write_file`・`submit`）、`tool_choice: auto`、Tool の出力の切り詰め（12,000 文字、前後を残す）、Tool を呼ばない応答への催促（3 回で終わり）、Step の上限 60、1 回の応答の `max_tokens` 16,384、Prompt の上限 120,000 token、Sampling は Model の `generation_config` の既定。
  - 変えるもの: Tool の実行は Docker を直接呼ばず、すべて `NodeTools.call` で Tool Broker を通す。そのために Broker に Tool と Executor を足す（`repo.file.str_replace` / `repo.file.write`: `write`、`repo.shell.run`: `execute`。Decision 0006 の表で範囲内は `SCOPED_AUTO`。Path は Node の Scope（Worktree）の中だけ、`.git` と `excluded_paths` は不可）。`submit` は Broker を通らない Adapter の内部の Tool。Model から見える形で Benchmark と違うのは、`submit` の引数に `changed_files`（10）を足すことだけ。
  - **Git**: Benchmark の Sandbox は完全な Clone だったので、`bash` の中で `git status` / `git diff` が使えた。本番の Worktree は Linked Worktree で、その Git Directory は Mount の外にあり、`.git` の File を書き換えれば別の Git Directory を指させることもできる。そこで Sandbox の中では本物の git を使わせず、Sandbox の `git` を小さな Shim にする: `status`・`diff`・`log`・`show` だけを Host 側の Broker の Read-only の Git の Tool（Coordinator の `_pin` と同じく `--git-dir` を固定し、`repositories/git.py` の硬い Runner で実行）へ渡し、他の副コマンド（`commit`・`checkout`・`reset` など）は拒否する。副コマンドだけでなく **引数も許可リストで決める**: 許すのは決まった Option（`--stat`・`--name-only`・`--name-status`・`--cached`・`-p`・`--oneline`・`-n <数>`・`--format` の決まった値 など）と、Revision（`HEAD`・`HEAD~<数>`・Node の起点と Branch の名前・40 桁の SHA）と、`--` の後の Path（文字どおりの相対 Path で、Worktree の中・`.git` と `excluded_paths` の外であることを Broker の Scope の判定で確かめる）だけ。`--no-index`・`-O`・`--output`・`--ext-diff`・`--textconv`・`-c`・`--git-dir`・`--work-tree`・`--exec-path` など、それ以外の Option はすべて拒否する。Host 側は `--no-ext-diff --no-textconv` と `core.pager=cat` を固定し、環境変数の `GIT_*` を消して、Worktree を作業 Directory にして実行する。Worktree の `.git` の File は読み取り専用で Mount し、Sandbox から書き換えられないようにする。Model から見える `bash` の中の `git status` / `git diff` は Benchmark と同じように動く。
  - 壁時計の上限は Benchmark の 45 分ではなく、Orchestrator の Node の Timeout（Decision 0021 の 3、30 分）を外側の上限とし、Adapter は残り時間で HTTP の Timeout を決める。
  - 同等性は Test で固定する: 記録した会話（Fake の Server の応答列）を与え、送る Request の Message・Tool の定義・Option が Benchmark の Harness と一致すること（`submit` の `changed_files` を除く）を確かめる。
- Read-only の Role（Planner・Researcher・Reviewer、Worktree のない Node）は同じ loop で、書き込みの Tool を渡さない（`bash` も Read-only の Executor にする。6 の Sandbox の読み取り専用の Mount）。
- 代替: (a) Harness のコードを Repository に取り込んで使う（Broker を通らず、同期の I/O で Event loop を塞ぐので推奨しない）。(b) Tool の形を Backend 向けに作り直す（Broker の名前を Model に見せる等。測っていない形になり、Decision 0074 の比較が当てはまらなくなる）。(c) Git Directory を読み取り専用で Sandbox に Mount する（`.git` の書き換えは防げるが、Common Directory の全体が見え、Index の更新の失敗の扱いが git の版に依存するので、Shim を推奨する）。

### 3. Reasoning の履歴の方針の差し込み口（#200）

- **推奨:** `LocalAgentRuntime` は、毎回の Request の前に会話の履歴を `ReasoningHistoryPolicy.prepare(messages) -> messages` に通す。方針は差し込み口だけを作り、**既定は Benchmark と同じ「Reasoning を残す」（`KeepAllReasoning`）**。#200 の Decision（0076）が承認されたら、その方針（例: 直近 N Step より古い `reasoning_content` を外す `DropOlderReasoning(n)`）を設定で選べるようにする。
  - Reasoning は Model に返すためだけに Memory の中に持ち、DB・Log・Audit・`NodeResult` に書かない（失敗の文と同じ扱い）。Log には文字数だけを出してよい。
  - Prompt の上限（120,000 token）に近づいたときの扱いも同じ口で行える（方針が要約や切り詰めを返してよい）が、既定は Benchmark と同じく「上限を超えたら終わる」。
- 代替: 方針を Orchestrator 側で持つ（Orchestrator は会話を持たないので推奨しない）。

### 4. OOM と Error の分類、Escalation（#183、Decision 0071・0021 の 3）

- **推奨:** Runtime は失敗を次の閉じた対応で `NodeOutcome.failed` にする（文は Loop 検知の Hash にだけ使われ、保存されない。理由ごとに固定の文を付ける）。対応表は `ErrorClassifier` に 1 か所で持ち、Test で 1 行ずつ固定する。`error_class` はすべて `RUNTIME_ERROR_CLASSES` にある名前で、`retryable=False` はどの行にも使わない（使うと Escalation もされない）。

  | 起きたこと | `error_class` | `escalate` |
  | --- | --- | --- |
  | vLLM が CUDA の OOM を返した（応答の Error の型・本文に `CUDA out of memory` / `OutOfMemoryError`）、Model の Server の Unit が OOM Killer で終わった（`systemctl show` の `Result=oom-kill`。Scheduler / System Health が持つ状態から読む）、CLI や Tool の Process が Container の cgroup の `memory.events` の `oom_kill` の増加とともに終わった | `AgentOutOfMemory` | いいえ |
  | Model の Server に繋がらない・503（起動中・Unload 中）、1 の `CloudGate` で拒まれた | `ConnectionError` | いいえ |
  | HTTP / CLI の Timeout（Node の Timeout より先に Adapter の上限に達した） | `TimeoutError` | いいえ |
  | Context の上限（Prompt の上限や vLLM の `maximum context length`）、Step の上限、Tool を呼ばない応答の繰り返し、CLI の `no_submission` | `RuntimeError` | **はい** |
  | 応答が JSON でない・Tool 呼び出しの形が壊れている（3 回続いたら） | `ValueError` | いいえ |
  | CLI の Provider の Rate limit | `ConnectionService` の `FailureCode`（`rate_limited`）で記録し、Runtime は `ConnectionError` | いいえ |
  | CLI の Credential の失効 | `FailureCode` の `expired` で記録し、Runtime は `ConnectionError` | **はい** |
  | Sandbox を起動できない（6 の Mask を置けないなど） | `PermissionError` | いいえ |
  | `submit` の引数が `NodeResult` として不正（10 の 3 回を超えた） | `InvalidNodeResultError` | いいえ |
  | その他 | `AdapterError` | いいえ |

  - **`escalate`（Decision 0021 の 3 への追加）**: `NodeOutcome.failed(error_class, message, escalate=True)` は「この段で同じ方法を繰り返しても無駄だが、上の段なら解けるかもしれない」を表す。`_retry_step` は、`escalate=True` の失敗で上の段があり、`MAX_APPROACH` と Budget が許すなら、Loop 検知を待たずに `ESCALATE_AGENT` にする。上の段がなければ今までどおり（Loop 検知と段の試行の上限）。理由: Loop 検知（Decision 0007）は Task ごとの直近 10 件の Window で同じ Signature（Class・Node の key・文）が 3 回のときだけ働くので、並列の Node の失敗や、違う理由の失敗が交互に起きると Escalation されず、段の試行の上限（6 回）で Node が諦めてしまう。Context の上限まで走る Node では、1 回の無駄が 30 分になりうる。
  - Worktree を用意できない（`WorktreeUnavailable`）のは Runtime ではなく Worktree の準備（Coordinator）の失敗で、Orchestrator が自分の名前で記録する（Runtime は名乗らない）。
  - Python の `MemoryError` は `AgentOutOfMemory` と同じに数えられる（Decision 0071 の 1）。Adapter は自分の `MemoryError` を握りつぶさない。
  - `NodeStopped`（Budget の超過・Task の終了・Lease の喪失）は捕まえずにそのまま上げる（`runtime.py` の規則）。
- 代替: (a) `escalate` を足さず、固定の文で Loop 検知に任せる（Protocol は変わらないが、上の理由で Escalation されないことがある）。(b) Context の上限などを `retryable=False` にする（今の `_retry_step` では Escalation もされず、Local の段で Node が終わる）。(c) OOM を System Health の側だけで見て、Adapter は報告しない（どの Node の失敗か分からず、Decision 0071 の記録が付かない）。

### 5. Budget・Token・GPU 時間の報告（Decision 0077 の上で）

- **推奨（Local）:** vLLM の応答ごとに、`usage.prompt_tokens + usage.completion_tokens` を `NodeBudget.charge(TOKENS, n)` で **その場で** 報告する（Node の終わりにまとめない。途中で止まっても使った分が残り、Budget の超過でその場で止まる）。Prefix Cache の当たりは引かない（Server が処理した量として数える。Codex / Claude の「入力 + 出力」と同じ数え方）。`HybridRuntime` の包みがこれを数えて `local_usage` に入れる（Decision 0077 の 2）。`usage` のない応答は 0 にしない: 対応する vLLM の版は非 Stream の応答で `usage` を返すことを Test で確かめたうえで、それでも欠けた応答には控えめな見積もり（送った Message の大きさから `BYTES_PER_TOKEN_ESTIMATE` で見積もった入力 + その Request の `max_tokens`）を報告する。
- **推奨（GPU 時間）:** Adapter は報告しない（`HybridRuntime` が Lease の時間を数える）。
- **推奨（CLI）:** Token は `ConnectionService` だけが計上する。**Runtime は `NodeBudget` に報告しない**（二重にしない）。
  - (a) 開始の前に、Task の `TOKENS` の残りが Session の最低の枠（設定 `cli_session_min_tokens`。暫定 50,000）以上あることを確かめ、足りなければ始めない（Admission の一部）。
  - (b) Session の途中は、1 の `on_usage` で Turn ごとに増えた分を受け、その分を使用量の行と Task の Budget に **その場で** 計上する。Budget が尽きたらその場で CLI を止める（8 の取り消しと同じ手順。Node には `NodeStopped`）。
  - **保存の形**: 今の `connection_usage` は `in_flight` の間 Token の列を NULL に保つ CHECK（`no_tokens_in_flight`）があり、Token は精算の時にだけ書かれる。そこで 11 の 4 の PR で Migration を足し、Session の行（`execute_session` の行。新しい列で区別する）に限って `in_flight` の間も Token の列を持てるようにし、Turn ごとの計上は「行の Token の列への加算（`COALESCE(…, 0) + 増分`）と Task の Budget への計上を 1 つの Transaction で」行う（Decision 0057 の Lease の確認も同じ Transaction）。Process が落ちたときは、加算済みの分が行と Budget に残り、Reaper（`ABANDONED_CALL_AGE_SECONDS`）が行を閉じるときもそれを保つ。1 回の Prompt の呼び出し（`execute`）の行の CHECK は変えない。
  - (c) Session の終わりの精算（`_settle` に当たる処理）は、`AgentSessionResult` の合計から **すでに途中で計上した分を引いた残りだけ** を計上する（合計が途中の計上より小さければ何も足さず、引き戻しもしない）。使用量の行の合計は「途中の計上 + 残り」で、二重には数えない。
  - (d) Turn ごとの Token を機械読みの出力で報告しない CLI・版は **有効にしない**。有効にした版でも、Session の終わりに合計が分からない（異常終了など）ときは、0 とせず、控えめな上限の見積もり（設定 `cli_unknown_usage_tokens`。暫定 200,000 token）から **途中で計上した分を引いた残り**（0 未満なら 0）を計上する。
  - (e) User ごとの共有 Connection の `tokens` の Quota は、今までどおり Admission（Session の開始）の時にだけ判定する（Decision 0016 の 3: 始まった呼び出しは止めず、実行中の Task も止めない）。Session の途中で上限を超えることはありうる（0016 の 5 で認めた超過と同じ。Task の Budget が抑える）。
- 代替: (a) Node の終わりに合計を 1 回だけ報告する（途中で止まった分が失われ、長い Node が Budget を超えて走る）。(b) CLI の Session の開始で `max` の Token を予約し終わりに精算する（Decision 0016 の 5 が後で足せるとした形。予約の残りの後始末が要り、30 分の Session では予約が大きすぎる）。

### 6. Credential と Sandbox

- **推奨（Tool の Container: Local の Shell・ファイルの Tool と、CLI が Bridge 経由で呼ぶ Tool）:** Executor は Node ごとの Container（`--network none`、CPU / Memory / PID の上限、認証情報なし）の中で動かす。Container は **Task の作成者の Linux の Account で** 起動する（Worktree はその Account の持ち物: Decision 0017 / 0029）。経路は Decision 0029 の SSH の Wrapper を広げ、許す操作を「決まった Image の Sandbox の起動・その中での実行・停止」に限る（Rootless の Podman を想定。実装の PR で確かめる）。
  - Mount: 書き込む Worker の Node では自分の Worktree（読み書き）。Worktree のない Node（Planner・Researcher・Reviewer・読むだけの Worker。Decision 0036 のとおり既存の Checkout を読む）では、Executor が知っている Node の Scope（`TaskScope`）の Repository の Root（読み取り専用）。
  - **どちらの場合も、Mount した Root の中にある `TaskScope.excluded_paths` と `NodeWorktree.protected` を Mask する**（空の読み取り専用の tmpfs / File を上に重ねる。Worktree の `.git` の File は 2 のとおり読み取り専用）。ファイルの Tool も Broker の Scope の判定でそれらを拒否する。Mask を置けない（Path の形が Mask に向かないなど）ときは Sandbox を起動せず、Node は `PermissionError`（固定の文 `sandbox_unavailable`）で失敗する。CLI の経路でも Tool はこの Container で動くので、Mask された File は Provider に送られない。
  - Backend の User が Docker を直接呼ぶ形（Benchmark の形）は、Docker の Group が Host の root と同じ権限なので採らない。
- **推奨（CLI 自身の Tool）:** CLI の組み込みの Tool（Shell・編集・読み取り）は **最初の実装から止め**、CLI が使える Tool は PAW の Tool と `submit` だけにする。PAW の Tool は MCP の Server（Backend 側の Bridge）として CLI に渡し、1 回の呼び出しごとに `NodeTools.call` で Tool Broker を通す（Grant・ACL・承認・Budget・Queue の Lease の Fencing: Decision 0006 / 0046 / 0057 をそのまま適用する）。組み込みの Tool を確実に止められること（Claude Code の Tool の制限と MCP の設定、Codex の MCP の設定と組み込みの Shell の無効化。Flag は実装時に公式の情報で確かめる）を CLI の版ごとに Test で確かめ、**止められない CLI・版は有効にしない**（fail-closed）。Run 全体を 1 つの Broker の呼び出しとして許す形は、Decision 0006 の 1 回ごとの判定を飛ばすので採らない。
- **推奨（MCP の Bridge の呼び出し元の確認）:** Bridge は Session ごとに作り、1 つの Session（Task・Node の試行・その試行の `NodeTools`）にだけ結び付ける。受け口は Session ごとの Unix Socket（Session ごとの Directory に置き、その CLI の Container にだけ Mount する。TCP では受けない）で、さらに Session ごとの推測できない Token（256 bit。MCP の設定で CLI に渡し、Log に出さない）を呼び出しごとに確かめる。Socket・Token が違う呼び出しは拒否する。Session が終わったら Socket を消し Token を無効にする。他の Container・Process から別の Task の Tool を動かすことはできない。
- **推奨（CLI の Process の Container と認証情報）:** CLI の Process は Tool の Container とは **別の** Container で動かす: Worktree を Mount せず、Network は Provider の Endpoint（または下の Proxy）と Bridge の Socket だけ（Egress の Allowlist）。Worktree が要らないので、**この Container は Task の作成者ではなく、専用の System Account（例 `paw-agent-cli`。Login できず、どの User の Group にも入らない）で起動する**。Task の作成者を含む Host の一般の User は、別の Account の Rootless の Container の中（`podman exec`、`/proc`）に入れない（Host の root は入れる。root は Admin の側として受け入れる）。
  - **認証情報は、できる限り Credential を付ける Proxy で扱う（推奨）。** Backend が専用の Account で動かす Proxy が、Secret Store の Credential で Provider への Request に認証を付け、CLI の Container には Credential を置かない。OAuth の Refresh は Proxy が Connection ごとに 1 つだけ（Single-flight の Lock）行い、新しい Credential を Secret Store に書き戻す。CLI がこの形で動くか（Endpoint の差し替え・Subscription の認証の形）は CLI ごとに実装の PR で確かめる。
  - Proxy で動かない CLI は、Credential の File を Session の間だけ CLI の Container の tmpfs（`CODEX_HOME` / `CLAUDE_CONFIG_DIR` に当たる場所、Mode 0700、所有者は上の専用の Account）に置く。この場合は **Connection ごとに同時に 1 つの Session だけ** を動かす（Refresh Token が使い捨てで回転する Provider では、並列の Session が互いの Token を失効させ、後から書いた方が失効した Token を保存しうるため）。Session が終わったとき（成功・失敗・取り消しのどれでも。取り消しの時も精算と同じく `asyncio.shield` の中で）、File の Credential が渡したものから変わっていれば、Connection ごとの Lock の中で、保存されている版が渡した版のままのときだけ（Compare-and-swap）書き戻す。Process ごと落ちて書き戻せなかったときは、次の Health Check が `expired` を見つけたら Admin が繋ぎ直す（下の「リスク」。これが Proxy を推奨する理由）。
  - Refresh の書き戻しは、Backend の内部だけが呼べる `ConnectionService.rotate_credential`（Actor なし。`check_health` と同じく Backend 内部。同じ Connection の同じ Credential の新しい版だけを受け、別の Credential への置き換えはできない。Audit は System の `connection.rotate`）で行う。Admin の `replace_credential`（`admin.config.manage`）は変えない（Decision 0016 の 7 への追加）。
  - 認証情報は Log・DB・Error・`NodeResult`・Tool の出力に出さず、CLI の出力は Decision 0016 の 1 の Redact を通してから使う。Git の Push の Credential はどの Container にも渡さない（Push は Integration の後に Backend が行う: Decision 0052）。
- 代替: (a) Sandbox なしで Backend の Process から直接実行する（Path の外への書き込み・Network を止められないので推奨しない）。(b) CLI の組み込みの Tool を Container の中で使わせ、Run の開始だけを Broker に記録する（個々の操作が Broker の承認・ACL・Budget・Fencing を通らず、Decision 0006 / 0046 に反する。同じ Container の Shell から認証情報の File が読めるので、Mode 0700 では守れない。推奨しない）。(c) CLI の Container を Task の作成者の Account で動かす（その User が自分の Container に入って共有の Subscription の Credential を読めるので推奨しない）。(d) Benchmark と同じく Backend の User が Docker を呼ぶ（Docker の Group は Host の root と同じ権限なので推奨しない）。

### 7. 承認が要る Tool 呼び出し

- **推奨:** Broker が `NEEDS_APPROVAL` を返したとき、Runtime は **待たない**（Local の Lease を承認待ちで持ち続けない）。Model には「承認が要るので実行していない」という固定の文を Tool の結果として返し、Model は他の作業を続けられる。Node の終わりに未承認の呼び出しが残っていれば、`NodeResult.unresolved_questions` に固定形式（Tool 名と承認の ID だけ。引数は入れない）で載せる。
  - 同じ試行の中で同じ呼び出しが再び出て、そのときには承認済みなら、Runtime は覚えている `approval_id` を付けて `NodeTools.call` する（承認は呼び出しの Hash に結び付いている: Decision 0006）。
  - 試行をまたいで承認を待って再開する仕組み（Task を `waiting`（承認）にし、承認で Node を続ける）は、この Decision では作らず、後続の Decision で決める。
- `DENY` は Model にそのまま「拒否された」と返し、同じ呼び出しの繰り返しは Loop 検知と Budget に任せる。
- 代替: 承認を Poll して待つ（Local の GPU を空いたまま押さえ、ほかの Node が Lease を取れない）。

### 8. Timeout と取り消し

- **推奨:**
  - 外側の上限は Orchestrator の Node の Timeout（Decision 0021 の 3）と Task の Budget。Adapter はそれより短い自分の上限を持つ:
    - Local の 1 回の HTTP の呼び出しは「残り時間」と 15 分の小さい方。
    - Tool の 1 回の実行は 300 秒（Benchmark と同じ）。`ToolRunner` の既定（`DEFAULT_EXECUTION_TIMEOUT` = 600 秒）より短いので、2 で足す Tool の `ToolRunner` には `execution_timeout=300` を渡す。
    - CLI の 1 回の Session は「Node の Timeout − 60 秒」（終わりの精算・`submit` の検証と Commit・Container の停止の余裕。60 秒は暫定）。今の `OrchestratorConfig.node_timeout_seconds` は正の値なら何でも受けるので、CLI の段を持つ組み立ては `node_timeout_seconds` が 300 秒以上であることを起動時に確かめ、足りなければ拒否する（0 以下の期限を Adapter に渡さない）。
  - 取り消し（`asyncio.CancelledError`）を受けたら: Local は HTTP の Request を閉じる（vLLM は Client の切断で生成を止める）、Container の中の Command と CLI の Process は Process Group に SIGTERM、5 秒で SIGKILL、Container を止める。`HybridRuntime` の待ち（`CANCEL_GRACE_SECONDS` = 10 秒）より短く終える。取り消しは握りつぶさず上げる。
  - 取り消しの後も CLI が使った Token は分かった範囲で `ConnectionService` に記録する（`cancelled` の行。Decision 0016 の 9。5 の (c)(d) と同じく途中の計上を引いた残り）。
- 代替: Adapter が上限を持たず Orchestrator の Timeout だけに任せる（1 回の HTTP が Node の残りを全部使い、Step の途中で切られて Token の記録が残らない）。

### 9. Test の方針

- **推奨:**
  - CI では GPU・Network・本物の CLI を使わない。Local は httpx の `MockTransport` の Fake の OpenAI 互換 Server（記録した応答列、Tool 呼び出し・Reasoning・`usage`・Error の本文を返す）で、CLI は Fake の CLI（その版の実際の Stream の形を固定した JSON を出す Script。終了 Code・Signal・遅延・Token の Refresh・組み込みの Tool の使用の試みを真似る）で Test する。Sandbox は Fake の Runner で、実際の Container を使う Test は手動の Smoke に回す。
  - 固定するもの: 2 の Request の同等性と Git の Shim、3 の方針の適用、4 の対応表の各行と `escalate` の Escalation、5 の報告（Local の見積もり、CLI の途中の計上と終わりの残りだけの精算で二重にならないこと、見積もりから途中の分を引くこと）、6 の Credential が Log・Error・`repr`・結果に出ないこと（Secret の形の文字列は分割して書く）、組み込みの Tool が止まっていること（Fake の CLI が組み込みの Tool を使おうとしたら失敗にする）、Mask された Path が読めないこと、Bridge が別の Socket・Token の呼び出しを拒むこと、Refresh の書き戻しの Compare-and-swap と取り消しの時の書き戻し、7 の承認の扱い、8 の取り消しで Process と Container が残らないこと、`NodeStopped` がそのまま上がること、1 の Gate の順序（Admission の前に Placement を記録しない）と組み立ての拒否（Planner の Ladder の CLI、`CloudPolicy` なしの CLI の段）。
  - **GPU を使う Smoke Test は、Human の許可を得てから** 行う。内容: 本番の `paw-llm-main.service`（12 の Flag を足したもの）で paw-seed-v1 の数 Task を `LocalAgentRuntime` に解かせ、Benchmark の Harness と Tool 呼び出しの形・Step 数が大きく違わないことを見る。CLI の Smoke は、Subscription の利用規約（下の「Human に確かめてほしいこと」）と使用量への影響を Human が確認した後。
- 代替: 本物の vLLM を CI で動かす（GPU が要り、共有の Machine で負荷が大きい）。

### 10. `NodeResult` の作り方と Commit

- **推奨:** Commit するのは Worker の Node（Runtime）で、Backend の自動 Commit ではない（Decision 0036 の 7 を変えない）。Model の `submit` の引数は `summary`（必須）・`changed_files`（必須。変えた File の一覧）と、任意の `discovered_facts`・`unresolved_questions`・`confidence`。
  - Adapter は `submit` を受けたら、Node の Branch の **起点の Commit からの差分**（起点から `HEAD` までの Commit 済みの変更と、未 Commit の変更・未追跡の File の和。Host 側の Broker の Read-only の Git の Tool）と `changed_files` を突き合わせる。`git status` だけで比べないのは、前の試行が Commit した変更を再試行で見失わないため。
  - 一致しない（申告にない変更・未追跡の File がある、申告した File に変更がない、Scope の外・`.git`・`excluded_paths` を指す）ときは `submit` を受け付けず、その差を Model に返して直させる（3 回まで。それでも合わなければ `InvalidNodeResultError` で失敗）。
  - 一致したら、申告した Path のうち **未 Commit のものだけ** を指定して Broker の Git の Tool（`repo.git.commit`、決まった Commit Message と Author、Trailer `PAW-Node: <Task の ID>/<Node の key>`）で Commit する（未 Commit のものがなければ Commit しない）。`NodeResult.changed_files` は起点からの差分の全体、`NodeResult.commit` はその後の Branch の `HEAD` の SHA（起点と同じなら空）。何度やり直しても同じ結果を Branch から作り直せる。Commit の後に未 Commit の変更が残ることはない（残れば Decision 0036 の 7 のとおり統合の前に `dirty` で止まり、Backend は自動で Commit も破棄もしない）。
  - Model には Benchmark と同じく「Commit しない」と指示する（Model に Git の書き込みの操作をさせない。2 の Shim も拒否する）。`test_result` は Model の申告を使わない（Evaluator の仕事。AGENTS.md の「Agent 自身の完了を成功判定に使わない」）。
- **起点の Commit**: Worktree の準備（`GitWorktreeCoordinator.prepare_node`）が Branch を作り、上流の Worker の Branch の最初の Merge が終わった後の Commit。準備はその直後に、同じ Repository に起点を指す Ref（`refs/paw/base/<Branch の名前>`。Branch と同じ `paw/` の名前空間）を作り、一度作ったら動かさない。再試行・Process の再起動で同じ Branch を再び渡すときは、今の `HEAD` ではなくこの Ref を読んで `NodeWorktree.base_commit` に入れる（今の `NodeWorktree` は `repo_id`・`path`・`branch`・`protected` だけ）。Decision 0036 のとおり状態の正本は git で、Table は足さない。
  - Branch を作って Merge した後、Ref を書く前に Process が落ちた場合: 次の準備は Ref のない既存の Branch を見つけたら、`HEAD` から First-parent で辿り、この Node の Trailer（`PAW-Node: <Task の ID>/<Node の key>`）を持つ Commit を飛ばした最初の Commit を起点として Ref を作る（Runtime の Commit にだけこの Trailer が付き、上流の Merge と上流の Worker の Commit には付かないので、決まった 1 つになる）。それでも決められない（Trailer のない Commit が Runtime 以外から足された）ときは推測せず、Worktree の準備の失敗（`WorktreeUnavailable`。Orchestrator の名前）にする。
- **複数の Repository の Worktree を持つ Node**（`NodeAssignment.worktrees` が 2 つ以上）: Worktree は 1 つなら Benchmark と同じく `/workspace` に、2 つ以上なら `/workspace/<repo_id>/`（`NodeWorktree.repo_id` の UUID）に置く。Repository の名前は Project の中でしか一意でなく、Task は別の Project の同じ名前の Repository を持ちうるので、名前は使わない（Model には System Prompt で Directory と Repository の対応を示す。表示名は Backend が Scope から渡す）。`changed_files` は `<repo_id>/` で始まる Path で区別する。Adapter は Repository ごとに突き合わせて、Repository ごとに 1 つの Commit を作る。`NodeResult.commit`（1 つの値、64 文字）は Repository が 1 つのときだけその SHA を入れ、2 つ以上のときは空にして、Repository ごとの Commit を `artifacts` に固定形式（`commit:<repo_id>:<sha>`）で 1 行ずつ入れる。統合は Node の Branch を読む（Decision 0036）ので、`commit` の値には依存しない。どれかの Repository で Commit に失敗したら Node を失敗にする。Commit 済みの他の Repository はそのままで、再試行では「起点からの差分」で突き合わせるので、先に Commit した Repository の変更も `changed_files` と `artifacts` に戻る（Commit を Repository をまたいで不可分にはしない）。
- **Role ごとの `submit` の形**: 上の検証と Commit は Worker だけ。Researcher・Reviewer（Worktree なし）の `submit` は `summary`（必須）と任意の `discovered_facts`・`unresolved_questions`・`confidence` で、`changed_files` は持たない（Commit もしない）。Planner（Local だけ。1）には `submit` の代わりに `submit_plan`（`summary` と `plan`（Plan の JSON。Decision 0021 の 1 の形））を見せ、Adapter は `NodeResult(summary=...)` と `plan` を組み立てて `NodeOutcome.succeeded(result, plan=plan)` で返す（Plan の検査は今までどおり Orchestrator が行う）。
- 代替: (a) Model に Commit させる（Message・Author・`.git` の扱いが Model 任せになる）。(b) `submit` の後に Worktree の変更をすべて Commit する（申告にない生成物・一時 File を含みうる。Decision 0036 の 7 が退けた形なので推奨しない）。(c) 起点を Table に保存する（git の外に第 2 の正本ができるので推奨しない）。

### 11. PR の分け方（段階）

- **推奨:** 次の順で、1 段階 1 PR（どれも「Refs #208」。最後の段階で Closes）。
  1. `agents/` の共有部品: `ChatCompletionsClient`・`ErrorClassifier`（4）・`ReasoningHistoryPolicy`（3、既定は残す）、`NodeOutcome.escalate` と `_retry_step`（4）、#38 の Model の Interpreter の本番の配線（`ScheduledInterpreter`、Tool なしの 1 回の呼び出し）。GPU なしの Test のみ。
  2. Tool Broker のファイル・Shell・Git の Tool と Executor（Git の Shim を含む）、Sandbox の Runner と Mask（6。SSH の Wrapper の拡張を含む）。
  3. `LocalAgentRuntime`（2・5・7・8・10）、`NodeWorktree.base_commit` と `refs/paw/base/*`（Worktree の準備）、`local_runtimes` への配線、`paw-llm-main.service` の Flag（12。Deploy の変更は Human が反映する）。ここで Orchestrator の Worker（`Orchestrator.serve`）を起動するかどうかは、Decision 0047 の 5 のとおりこの段階の PR で提案する。
  4. `CloudGate`、`NodeAssignment.connections`、`AgentSessionAdapter` と `ConnectionService.execute_session`（途中の計上・残りだけの精算）・`rotate_credential`、Credential の Proxy、PAW の Tool の MCP の Bridge、`CodexCliRuntime` / `ClaudeCliRuntime`、`HybridRuntime` の Cloud の経路の変更（1・5・6）。CLI の段は `CloudPolicy` の Decision の承認までは組み立てで有効にならない。
  5. Human の許可を得た GPU の Smoke と、その記録（9）。
- 代替: 1 つの大きな PR にする（Review と Revert が難しく、GPU の調整も分けられない）。

### 12. Model の Server の設定

- **推奨:** 本番の `paw-llm-main.service` の起動の引数に、Benchmark と同じ `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足す（11 の 3 の PR）。`--max-model-len`・`--gpu-memory-utilization` など Context と Memory の大きさは、#200 の Decision（共存時の値は Decision 0073 / 0075）に委ね、ここでは変えない。今の Unit はどちらも付けていない（vLLM の既定で動く）ことを Human に知らせる。Adapter は Server の `served-model-name`（`main`）を Model の名前として使い、Placement の `model` には Deployment の実際の Model の ID（`Qwen3.8-27B-FP8`）を記録する。
- 代替: Adapter の側で Model の生の出力から Tool 呼び出しと Reasoning を取り出す（Parser を自前で持つことになり、Benchmark と形が変わる）。

## 決めてほしいこと

1. **3 つの Runtime を新しい `paw_backend/agents/` に置く。Local は `local_runtimes` で `HybridRuntime` に包む（Planner も同じ）。CLI の 2 つは Worker・Researcher・Reviewer の Ladder の段と `HybridRuntime` の `cloud` で使い、Planner の Ladder には置かず、`CloudPolicy` が注入されるまでは段に置けない（組み立てが拒否）。外部へ送る順は `CloudPolicy` → Admission → Placement の記録 → 起動（`CloudGate`）。CLI は `ConnectionService` の新しい `execute_session`（`AgentSessionAdapter`、Turn ごとの `on_usage`）を通し、Runtime には Orchestrator が試行ごとに作る `NodeAssignment.connections` だけを渡す。用途は Worker・Planner が `coding`、Reviewer が `review`、Researcher が `research`。#38 の Interpreter は `ScheduledInterpreter` で Lease を取って呼ぶ**（1）でよいか。推奨: はい。
2. **Local の Tool loop は Benchmark の Harness のコードではなく振る舞いを asyncio で移し（Prompt・4 つの Tool・切り詰め・催促・60 Step・16,384 / 120,000 token は同じ。`submit` に `changed_files` を足すことだけが違う）、Tool の実行はすべて Tool Broker に足すファイル・Shell の Tool を通す。Sandbox の `git` は読み取りの副コマンドを、許可リストの Option・Revision・Scope の中の Path だけで Host 側の固定した Git の Tool へ渡す Shim にする（`--no-index` などは拒否）。外側の時間の上限は Node の Timeout（30 分）**（2）でよいか。推奨: はい。
3. **Reasoning の履歴は `ReasoningHistoryPolicy` の差し込み口だけを作り、既定は Benchmark と同じ「残す」。方針の選択は #200 の Decision（0076）に委ねる。Reasoning は保存・Log しない**（3）でよいか。推奨: はい。
4. **失敗の分類は 4 の表のとおり（どれも `RUNTIME_ERROR_CLASSES` の名前で、`retryable=False` は使わない）。Context・Step の上限・提出なし・Credential の失効には、Decision 0021 の 3 に足す `NodeOutcome.escalate=True` を付け、上の段があれば Loop 検知を待たずに Escalation する**（4）でよいか。推奨: はい。
5. **Local は応答ごとに `prompt_tokens + completion_tokens` をその場で報告し（Prefix Cache は引かない。`usage` が欠けたら見積もり）、GPU 時間は `HybridRuntime` に任せる。CLI は `ConnectionService` だけが計上し、開始前に Token の残りが最低の枠（暫定 50,000）以上かを確かめ、Turn ごとにその場で計上して尽きたら止め、終わりの精算は途中の分を引いた残りだけ（途中の分は Migration で Session の行に加算して残す）、合計が分からなければ見積もり（暫定 200,000）から途中の分を引いた残り。Turn ごとの Token を報告しない CLI・版は有効にしない。User の Connection の Token の Quota は今までどおり開始の時だけ判定する。Decision 0016 の 5 をこの範囲で Supersedes**（5）でよいか。推奨: はい。
6. **Tool は Task の作成者の Account の Node ごとの Container（書き込む Node は自分の Worktree、Worktree のない Node は Scope の Repository の Root を読み取り専用、どちらも `excluded_paths` と `protected` を Mask、Network なし、認証情報なし）で動かし、経路は Decision 0029 の SSH の Wrapper を広げる。CLI は組み込みの Tool を最初から止め、PAW の Tool を Session ごとの Unix Socket と Token に結び付けた MCP の Bridge で 1 回ずつ Tool Broker に通す（止められない CLI・版は有効にしない）。CLI の Process は Worktree のない別の Container を専用の System Account（`paw-agent-cli`）で動かす。認証情報は Credential を付ける Proxy を推奨し（Refresh は Proxy が Connection ごとに 1 つ）、Proxy で動かない CLI は Connection ごとに同時に 1 Session・tmpfs・取り消しの時も Compare-and-swap で書き戻す。書き戻しは Backend 内部だけの `rotate_credential`（Decision 0016 の 7 への追加）**（6）でよいか。推奨: はい。
7. **承認が要る呼び出しは待たずに「実行していない」と Model に返し、残った承認は `unresolved_questions` に Tool 名と承認の ID だけで載せる。試行をまたいで承認を待って再開する仕組みは後続の Decision**（7）でよいか。推奨: はい。
8. **Adapter の上限（HTTP は残り時間と 15 分の小さい方、Tool は 300 秒で `ToolRunner` に渡す、CLI は Node の Timeout − 60 秒で、CLI の段を持つ組み立ては Node の Timeout 300 秒以上が必要）と、取り消しで SIGTERM → 5 秒で SIGKILL → Container の停止（`HybridRuntime` の 10 秒の待ちより短く）**（8）でよいか。推奨: はい（数値は暫定）。
9. **CI は Fake の Server・Fake の CLI・Fake の Sandbox だけで Test し、GPU の Smoke は Human の許可を得てから、CLI の Smoke は利用規約と使用量への影響を Human が確認した後に行う**（9）でよいか。推奨: はい。
10. **Commit は Worker の Node（Runtime）が行い、Backend の自動 Commit にはしない（Decision 0036 の 7 のまま）。Worker の Model は `submit` で `changed_files` を申告し（Researcher・Reviewer は `changed_files` なし、Planner は `submit_plan` で Plan を返す）、Adapter は Branch の起点（`refs/paw/base/<Branch>`。上流の Merge の後に作り、落ちたときは Node の Trailer から作り直す）からの差分と突き合わせ、一致したときだけ申告した Path のうち未 Commit のものを Commit する（合わなければ 3 回まで直させる）。複数の Repository は `/workspace/<repo_id>/` に置き、Repository ごとの Commit を `artifacts` に入れる。`test_result` は Model の申告を使わない**（10）でよいか。推奨: はい。
11. **11 の 5 段階で PR を分ける（共有部品・`escalate`・#38 の Interpreter → Broker の Tool と Sandbox → Local の Runtime と Server の設定 → CLI の Runtime と Proxy と Bridge → GPU の Smoke）**（11）でよいか。推奨: はい。
12. **本番の `paw-llm-main.service` に `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足し、`--max-model-len`・`--gpu-memory-utilization` は #200 の Decision に委ねる**（12）でよいか。推奨: はい。

## Human に確かめてほしいこと（Decision の外の前提）

- **Provider の利用規約**: 共有の Subscription の CLI を、Headless で、組み込みの Tool を止め MCP の Tool だけで、Backend が複数の User のために動かすことが、各 Provider の規約に合うか（Decision 0016 の前提の確認は Prompt → 文の呼び出しについてのもの）。CLI の Smoke の前に確かめる。
- **Decision 0016 の前提**: Secret Store（製品と保存方式）と Network Policy（Egress の Allowlist・Proxy）は、まだ Repository にない。CLI の Runtime（11 の 4）はこれらが決まってから。
- **本番の `paw-llm-main.service`**: `--max-model-len` と `--gpu-memory-utilization` を付けていない（vLLM の既定で動く）。共存時の値（Decision 0073 / 0075）と #200 の Decision に合わせて Deploy で反映が要る。

## リスク

- Benchmark と Model から見える形を同じにしても、Tool の実行の経路（Broker・Sandbox・Git の Shim、承認で実行されない呼び出し）が違うので、本番の解ける割合は Benchmark の数値と同じとは限らない。9 の Smoke で大きな差がないことを見る。
- CLI の組み込みの Tool を止めて MCP の Tool だけで動かすと、CLI 本来の Tool を使う場合より解ける割合が下がるかもしれない。CLI の Smoke で確かめ、下がる場合も Broker を通らない形には戻さず、Tool の形を見直す。
- CLI の認証の形（OAuth の File の場所・Refresh の仕方・Proxy で動くか）と Option は CLI の版で変わりうる。実装の PR でその時点の公式の情報を確かめ、Fake の CLI の Test を合わせる。
- Proxy を使えない CLI で Process ごと落ちると、Refresh した Credential を書き戻せず、Connection が `expired` になりうる（Admin が繋ぎ直す）。
- Rootless の Container を Task の作成者の Account と専用の Account で動かす準備（Image の配布・cgroup の委任・Socket の受け渡し）が Host ごとに要る。Deploy の手順に入れる。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
