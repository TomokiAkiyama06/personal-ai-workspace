# Orchestrator の実 Runtime Adapter（Local の Main Model on vLLM・Codex CLI・Claude Code CLI）の設計（境界、Local の Tool loop、Reasoning の履歴の差し込み口、使用量の報告、OOM と Error の分類、Credential と Sandbox、承認、Timeout と取り消し、Test、PR の分け方）

- Status: Proposed
- Date: 2026-10-08
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208)。この Decision は設計だけで、実装は承認の後に下の 11 の段階ごとの PR で行う。関係する既存の口: `apps/backend/paw_backend/orchestrator/runtime.py`（`AgentRuntime`・`NodeAssignment`・`NodeOutcome`・`NodeTools`・`NodeBudget`・`NodePlacement`）、`compute/runtimes.py`（`HybridRuntime`）、`compute/wiring.py`（`LocalRuntime`）、`orchestrator/composition.py`（`local_runtimes`・`agent_runtimes`）、`orchestrator/errors.py`（`RUNTIME_ERROR_CLASSES`・`AGENT_OUT_OF_MEMORY`）、`connections/`（`ConnectionAdapter`・`ConnectionService.execute`）、`tools/`（Tool Broker・`ToolRunner`・Registry）、`apps/backend/deploy/systemd/paw-llm-main.service`
- Supersedes: なし。[Decision 0021](0021-dag-orchestrator-policy.md)（Runtime の Protocol・Error Class・Node の Timeout）、[Decision 0016](0016-shared-connection-adapter-policy.md)（共有 Connection・Credential・Quota）、[Decision 0006](0006-tool-broker-policy.md)（Tool Broker）、[Decision 0047](0047-task-execution-composition-and-task-end-effects.md) の 5（本番の Runtime はまだない）、[Decision 0036](0036-parallel-worktree-integration.md) の 7（Worker が Commit し、Backend は自動で Commit しない）、[Decision 0037](0037-gpu-compute-scheduler.md) の 14（`CloudPolicy` がなければ Cloud に送らない）、[Decision 0046](0046-tool-call-lease-fencing.md)（Tool 呼び出しの Fencing）、[Decision 0048](0048-node-placement-audit.md)（Placement の記録）、[Decision 0058](0058-compute-scheduler-application-wiring.md) の 7（`local_runtimes`）、[Decision 0071](0071-agent-oom-and-escalation-records.md)（`AgentOutOfMemory`）、[Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md)（Main は Qwen3.8-27B-FP8）は書き換えず、その中で Runtime を作る。Budget / 使用量の記録は Proposed の Decision 0077（PR #207）を前提にし、Reasoning の履歴の方針そのものは #200 の Decision（0076 を予約済み）に委ねる

## 背景

Orchestrator（PAW-034）は Node の 1 回の試行を `AgentRuntime.run_node(NodeAssignment) -> NodeOutcome` で動かす。周りの口はそろっている。

- `HybridRuntime`（PAW-036）が Local の Lease を取り、Lease を持った時間を `GPU_SECONDS` として Task の Budget に計上し、Placement（Local の GPU / CPU・Cloud）を走らせる **前に** 記録する（Decision 0048）。Cloud の Runtime（`cloud`）と `CloudPolicy` を渡せば、Local が混んでいるときに Cloud へ振り替えられる。
- Runtime は `NodeTools.call`（Tool Broker を通る Tool 呼び出し）と `NodeBudget.charge`（`TOKENS` / `GPU_SECONDS` の報告）しか持たない。Broker・DB 接続・Grant・他の Node の会話は渡されない。
- 書き込む Worker の Node には専用の Worktree（`NodeAssignment.worktrees`、PAW-035）があり、Runtime はそこに Commit する。
- Runtime が名乗れる失敗は閉じた一覧（`RUNTIME_ERROR_CLASSES`）だけで、OOM は `AgentOutOfMemory`（Decision 0071）。失敗の文は Hash だけが Loop 検知に使われ、保存されない。
- Codex / Claude は共有 Connection（PAW-030）の `ConnectionService.execute` を通る（Admission・Quota・使用量の行・Task の Budget への `TOKENS` の計上・`Secret` は 1 回の呼び出しの間だけ）。ただし `ConnectionAdapter.run` は「1 回の Prompt → 1 つの文」の形で、Worktree や Tool を持たない。Adapter の実装は Repository に 1 つもない。
- Main の Coding Model は Qwen3.8-27B-FP8（Decision 0074）。その選定の数値は、Repository の外の paw-bench の Coding Harness（vLLM の OpenAI 互換の `/v1/chat/completions`、`bash` / `str_replace` / `write_file` / `submit` の 4 つの Tool、60 Step・45 分・Prompt 120k token・応答 16,384 token、Reasoning を履歴に残す、Docker の `--network none` の Sandbox）で測った。

足りないもの:

1. 本番の `AgentRuntime` が 1 つもない（Decision 0047 の 5）。Orchestrator は組み立てられず、Queue を Claim する Worker も起動しない。
2. Tool Broker の Registry は Working Set の Tool だけで、ファイル・Shell・Git の Tool の Executor がない（Decision 0047 の 5）。Local の Model が Worktree を編集する手段がない。
3. #183（PR #195）は OOM の報告の口と記録だけで、実際に CUDA OOM / OOM Killer を見分ける Adapter がない。
4. #200 が求める「古い Reasoning を履歴から外す」は Adapter 側の処理で、入れる場所がない。
5. #38（PR #205）の自由入力の構造化は Model の Interpreter の Port だけで、本番の配線がない。
6. 本番の `paw-llm-main.service` は `vllm serve /srv/models/main --served-model-name main` だけで、Benchmark の `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を付けていない。このままでは Tool 呼び出しと Reasoning が Benchmark と同じ形で返らない。

**次のことは要件も既存の Decision も決めていない。** 3 つの Runtime の境界と置き場所、Local の Tool loop を Benchmark の Harness からどう作るか、Reasoning の履歴の方針をどこに差し込むか、Token の数え方と誰が Budget に報告するか、Error の分類、CLI の Credential と Sandbox、承認が要る Tool 呼び出しの扱い、Timeout と取り消し、Test の方針、PR の分け方。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、推奨をここに置いて承認を求める。CLI の Option などの最新の仕様は、AGENTS.md の 10 に従い、実装の PR でその時点の公式の情報を確かめてから使う（ここに書いた Flag 名は確認前の見込み）。

## 提案

### 1. Adapter の境界と置き場所

- **推奨:** 新しい Package `paw_backend/agents/` に、`AgentRuntime` を満たす 3 つの Runtime と、それらが共有する部品を置く。
  - `LocalAgentRuntime`: Local の Main Model（vLLM の OpenAI 互換 API）で Tool loop を回す。組み立ては今の `local_runtimes`（`LocalRuntime(runtime=..., deployment="main", local_model=...)`）に渡し、`HybridRuntime` が包む（Lease・GPU 時間・Placement は `HybridRuntime` の仕事のまま。Adapter は GPU 時間を報告しない）。
  - `CodexCliRuntime` / `ClaudeCliRuntime`: CLI を 1 回の Node の試行として動かす。Ladder の上の段（`agent_runtimes` に Label で渡す）と、`HybridRuntime` の `cloud`（Local が混んでいるときの振り替え）の両方で使える。**どちらの経路でも、外部へ送る前に同じ `CloudPolicy`（Task の依存・権限・Quota による外部送信の可否。Decision 0037 の 14）を確かめる。** そのため判定を共通の `CloudGate`（`CloudPolicy.allows` → `placement.record(CLOUD, agent="codex" | "claude", model=...)` の順。どちらかが通らなければ何も送らない）にまとめ、`HybridRuntime` と CLI の Runtime の両方がこれを使う。Ladder から直接呼ばれたときも CLI の Runtime 自身がこの Gate を通す（Placement と Audit だけでは外部送信の許可にならない）。`CloudPolicy` が注入されていない（今の組み立てがそう: Decision 0037 の 14）・`placement` のない呼び出し（Planner）では、Cloud に送らず `COMPUTE_UNAVAILABLE` で失敗にする（fail-closed）。つまり CLI の段が実際に動くのは、`CloudPolicy` の本番の実装が承認されて注入された後になる（その中身は後続の Decision）。
  - 共有部品: `ChatCompletionsClient`（vLLM への 1 回の呼び出し。httpx の非同期 Client。Tool なしの 1 回の呼び出しは #38 の Interpreter と Planner も使う）、`ErrorClassifier`（4）、`ReasoningHistoryPolicy`（3）、`NodeResult` の組み立て（10）。
  - **`submit` の経路**: Local では Adapter の内部の Tool。CLI では MCP の Bridge が `submit` を PAW の Tool と並べて CLI に見せるが、`NodeTools.call` には渡さず、Bridge から CLI の Runtime へ直接渡す（Broker を通らない唯一の Tool。Worktree にも外部にも何もしない）。`AgentSessionResult` は最後の文の代わりに、受け取った `submit` の引数（無ければ「提出なし」）を持つ。検証と Commit（10）は Local と同じ処理で、合わなければ Bridge が `submit` の結果としてその差を CLI に返す。Bridge は Tool 呼び出しを 1 つずつ順に実行する（CLI が並べて出した呼び出しも直列にする）。`submit` を受けた時点で以後の Tool 呼び出しをすべて拒否し（凍結）、**すでに受け付けて実行中の呼び出しが終わるのを待ってから**（8 の Tool の上限 300 秒で打ち切り、打ち切ったら Container の中の Process を止めてから）検証する。合わなければ凍結を解いて差を返す。合えば Commit し、CLI を止め（8 の取り消しと同じ手順）、以後の `submit` も受けない（最初に受け付けた 1 回だけが結果）。Worktree を変えられるのは Bridge を通る呼び出しだけなので、凍結の間に Worktree が変わることはない。CLI が `submit` せずに終わったら `RuntimeError`（固定の文 `no_submission`）。
- CLI の 2 つは **`ConnectionService` を必ず通す**（Admission・Quota・使用量の行・Task の Budget の `TOKENS`・`Secret` の取り扱いを 1 か所に保つ）。今の `ConnectionAdapter.run(secret, AdapterRequest)` は Prompt → 文の 1 回の呼び出しなので、Worktree と Sandbox を受け取る別の口 `AgentSessionAdapter.run_session(secret, AgentSessionRequest) -> AgentSessionResult`（`submit` の引数・入出力の Token・終了の分類）と、`ConnectionService.execute_session`（`execute` と同じ順の確認・記録。用途の Category は Node の Role から決める: Worker と Planner は `coding`、Reviewer は `review`、Researcher は `research`）。Runtime は `Principal`・`TaskContext`・Queue の Lease を持たない（`NodeAssignment` の外）ので、**Orchestrator が試行ごとに作る `NodeConnections`（`NodeTools` と同じ形の口）を `NodeAssignment.connections` として足し**、`execute_session(kind, request)` だけを見せる。中身は Orchestrator がその試行のために作った `TaskContext`（Run と、その時点の Queue の Lease）と作成者の `Principal` で、Runtime はそれを取り出せない。Lease を失った・Run が替わった試行の呼び出しは、Tool 呼び出しと同じく `NodeStopped` になる（Decision 0046 / 0057 の Fencing。`task_id` から Lease を作り直すことはしない）を足す。`AdapterRequest` の型は変えない。
- 代替: (a) CLI の Runtime が `ConnectionService` を通らず直接 CLI を起動する（Quota・使用量・Credential の規則が 2 か所に分かれるので推奨しない）。(b) Local も `ConnectionAdapter`（`ConnectionKind` に `local` を足す）として作る（共有 Connection の Admission・Quota・Reaper は Local に当てはまらない。Decision 0077 の 1 の代替 (a) と同じ理由で推奨しない）。

### 2. Local の Tool loop: Benchmark の Harness の「振る舞い」を移し、実行は Tool Broker を通す

- **推奨:** paw-bench の `coding_harness.py` のコードはそのまま使わず（Repository の外・同期の urllib と Thread・Docker を直接呼ぶ）、`LocalAgentRuntime` に asyncio で書き直す。ただし **Model から見える形は Benchmark と同じにする**（Decision 0074 の数値はその形で測ったため）。
  - 同じにするもの: System Prompt の構成（Worktree の場所の言い換えだけ変える）、Tool の名前と引数（`bash`・`str_replace`・`write_file`・`submit`）、`tool_choice: auto`、Tool の出力の切り詰め（12,000 文字、前後を残す）、Tool を呼ばない応答への催促（3 回で終わり）、Step の上限 60、1 回の応答の `max_tokens` 16,384、Prompt の上限 120,000 token、Sampling は Model の `generation_config` の既定。
  - 変えるもの: Tool の実行は Docker を直接呼ばず、すべて `NodeTools.call` で Tool Broker を通す。そのために Broker に Tool と Executor を足す（`repo.file.str_replace` / `repo.file.write`: `write`、`repo.shell.run`: `execute`。Decision 0006 の表で範囲内は `SCOPED_AUTO`。Path は Node の Scope（Worktree）の中だけ、`.git` は編集不可）。`submit` は Broker を通らない Adapter の内部の Tool。Model から見える形で Benchmark と違うのは、`submit` の引数に `changed_files`（10）を足すことだけ。
  - 壁時計の上限は Benchmark の 45 分ではなく、Orchestrator の Node の Timeout（Decision 0021 の 3、30 分）を外側の上限とし、Adapter は残り時間で HTTP の Timeout を決める。
  - 同等性は Test で固定する: 記録した会話（Fake の Server の応答列）を与え、送る Request の Message・Tool の定義・Option が Benchmark の Harness と一致することを確かめる。
- Read-only の Role（Planner・Reviewer など、Worktree のない Node）は同じ loop で、書き込みの Tool を渡さない（`bash` も Read-only の Executor にする。6 の Sandbox の読み取り専用の Mount）。
- 代替: (a) Harness のコードを Repository に取り込んで使う（Broker を通らず、同期の I/O で Event loop を塞ぐので推奨しない）。(b) Tool の形を Backend 向けに作り直す（Broker の名前を Model に見せる等。測っていない形になり、Decision 0074 の比較が当てはまらなくなる）。

### 3. Reasoning の履歴の方針の差し込み口（#200）

- **推奨:** `LocalAgentRuntime` は、毎回の Request の前に会話の履歴を `ReasoningHistoryPolicy.prepare(messages) -> messages` に通す。方針は差し込み口だけを作り、**既定は Benchmark と同じ「Reasoning を残す」（`KeepAllReasoning`）**。#200 の Decision（0076）が承認されたら、その方針（例: 直近 N Step より古い `reasoning_content` を外す `DropOlderReasoning(n)`）を設定で選べるようにする。
  - Reasoning は Model に返すためだけに Memory の中に持ち、DB・Log・Audit・`NodeResult` に書かない（失敗の文と同じ扱い）。Log には文字数だけを出してよい。
  - Prompt の上限（120,000 token）に近づいたときの扱いも同じ口で行える（方針が要約や切り詰めを返してよい）が、既定は Benchmark と同じく「上限を超えたら終わる」。
- 代替: 方針を Orchestrator 側で持つ（Orchestrator は会話を持たないので推奨しない）。

### 4. OOM と Error の分類（#183、Decision 0071）

- **推奨:** Runtime は失敗を次の閉じた対応で `NodeOutcome.failed` にする（文は Loop 検知の Hash にだけ使われ、保存されない）。対応表は `ErrorClassifier` に 1 か所で持ち、Test で 1 行ずつ固定する。

  | 起きたこと | `error_class` | `retryable` |
  | --- | --- | --- |
  | vLLM が CUDA の OOM を返した（応答の Error の型・本文に `CUDA out of memory` / `OutOfMemoryError`）、Model の Server の Unit が OOM Killer で終わった（`systemctl show` の `Result=oom-kill`。Scheduler / System Health が持つ状態から読む）、CLI の Process が Sandbox の cgroup の `memory.events` の `oom_kill` の増加とともに終わった | `AgentOutOfMemory` | はい |
  | Model の Server に繋がらない・503（起動中・Unload 中） | `ConnectionError` | はい |
  | HTTP / CLI の Timeout（Node の Timeout より先に Adapter の上限に達した） | `TimeoutError` | はい |
  | Context の上限（Prompt の上限や vLLM の `maximum context length`）、Step の上限、Tool を呼ばない応答の繰り返し | `RuntimeError` | はい（下の注。理由ごとに固定の文を付ける） |
  | 応答が JSON でない・Tool 呼び出しの形が壊れている（3 回続いたら） | `ValueError` | はい |
  | CLI の Provider の Rate limit・Credential の失効 | `ConnectionService` の `FailureCode`（`rate_limited` / `expired`）で記録し、Runtime は `ConnectionError` | はい（下の注） |
  | `submit` の引数が `NodeResult` として不正 | `InvalidNodeResultError` | はい |
  | その他 | `AdapterError` | はい |

  - Python の `MemoryError` は `AgentOutOfMemory` と同じに数えられる（Decision 0071 の 1）。Adapter は自分の `MemoryError` を握りつぶさない。
  - **`retryable=False` は使わない（どの行も `True`）。** 今の Orchestrator（`_retry_step`）は `retryable=False` の失敗を Escalation の判定より前に `GIVE_UP` にするので、Local の段の Context の上限で Node が終わり、上の段（Codex / Claude）へ上がれなくなる。代わりに、Context の上限・Step の上限・Tool を呼ばない応答・Credential の失効には理由ごとの **固定の文**（例 `context_exhausted`）を付けて返す。同じ段で同じ理由が続くと、失敗の文の Hash が同じなので Loop 検知（Decision 0007: 直近 10 件で同じ Signature が 3 回）がまず代替（`TRY_ALTERNATIVE`）を、その後 `ESCALATE_AGENT` を選び、次の段へ上がる。**欠点**: 上がるまでに同じ段で最大 6 回ほど試すので、長い Node（Context の上限まで走る）では時間と Token が無駄になる。これを避けるには「この段では再試行しないが Escalation はする」を `NodeOutcome` に表す欄を足し、Orchestrator の `_retry_step` がそれを Escalation の判定に回す必要がある。Runtime の Protocol と Retry の規則（Decision 0021 の 3）の変更なので、この Decision では代替として示し、Local の Runtime の PR（11 の 3）の前に、実際の無駄の大きさを見て別の Decision で決める（推奨は、まず固定の文と Loop 検知で始めること）。
  - `NodeStopped`（Budget の超過・Task の終了）は捕まえずにそのまま上げる（`runtime.py` の規則）。
- 代替: OOM を System Health の側だけで見て、Adapter は報告しない（どの Node の失敗か分からず、Decision 0071 の記録が付かない）。

### 5. Budget・Token・GPU 時間の報告（Decision 0077 を前提）

- **推奨:**
  - Local: vLLM の応答ごとに、`usage.prompt_tokens + usage.completion_tokens` を `NodeBudget.charge(TOKENS, n)` で **その場で** 報告する（Node の終わりにまとめない。途中で止まっても使った分が残り、Budget の超過でその場で止まる）。Prefix Cache の当たりは引かない（Server が処理した量として数える。Codex / Claude の「入力 + 出力」と同じ数え方）。`HybridRuntime` の包みがこれを数えて `local_usage` に入れる（Decision 0077 の 2）。`usage` のない応答は 0 にしない: 対応する vLLM の版は非 Stream の応答で `usage` を返すことを Test で確かめたうえで、それでも欠けた応答には控えめな見積もり（送った Message の大きさからの見積もり `BYTES_PER_TOKEN_ESTIMATE` による入力 + その Request の `max_tokens`）を報告する。
  - GPU 時間: Adapter は報告しない（`HybridRuntime` が Lease の時間を数える）。
  - CLI: Token は CLI の機械読みの出力（JSON の `usage`）から `AgentSessionResult` に入れ、`ConnectionService` が使用量の行と Task の Budget の `TOKENS` に計上する。**Runtime は `NodeBudget` に二重に報告しない。** **Session の途中でも Task の Budget で止められるようにする。** 今の `ConnectionService` は呼び出しの開始で Budget が残っているかを見て、終わったときに合計を計上するだけなので、30 分・何十 Step の CLI の Run を 1 回の呼び出しとして扱うと、残りが 1 token でも始まり、終わるまで止められない。そこで (a) 開始の前に、Task の `TOKENS` の残りが Session の最低の枠（設定 `cli_session_min_tokens`。暫定 50,000）以上あることを確かめ、足りなければ始めない。(b) CLI の機械読みの Stream（Turn ごとの `usage`）から増えた分を Turn ごとに `ConnectionService` が使用量の行と Task の Budget に計上し（`execute_session` の中の途中の計上。Decision 0016 の 5 が「後で足せる」とした形）、Budget が尽きたらその場で CLI を止める（8 の取り消しと同じ手順。Node には `NodeStopped`）。Turn ごとの Token を機械読みの出力で報告しない CLI・版は **有効にしない**（Fake の CLI ではなく、実装の PR でその版の出力を確かめ Test に固定する）。有効にした版でも 1 回の Run の出力に Token の数がない（異常終了など）ときは、0 とせず、控えめな上限の見積もり（設定 `cli_unknown_usage_tokens`。暫定 200,000 token）を `AgentSessionResult` に入れて計上する（Decision 0016 の 5 の「返さない呼び出しは 0」は 1 回の Prompt の呼び出しの規則で、何十 Step も走る CLI の Run には当てはめない）。
- 代替: Node の終わりに合計を 1 回だけ報告する（途中で止まった分が失われ、長い Node が Budget を超えて走る）。

### 6. Credential と Sandbox

- **推奨（Local の Shell・ファイルの Tool）:** `repo.shell.run` などの Executor は、Node ごとの Container（`--network none`、CPU / Memory / PID の上限）の中で動かす。Mount するのは、書き込む Worker の Node では自分の Worktree だけ（読み書き）。Worktree のない Node（Planner・Researcher・Reviewer・読むだけの Worker。Decision 0036 のとおり既存の Checkout を読む）では、Executor が知っている Node の Scope（`TaskScope`）の Repository の Root を **読み取り専用** で Mount する（`excluded_paths` は Mount しない）。Container は **Task の作成者の Linux の Account で** 起動する（Worktree はその Account の持ち物: Decision 0017 / 0029）。経路は Decision 0029 の SSH の Wrapper を広げ、許す操作を「決まった Image の Sandbox の起動・その中での実行・停止」に限る（Rootless の Podman を想定。実装の PR で確かめる）。Backend の User が Docker を直接呼ぶ形（Benchmark の形）は、Docker の Group が Host の root と同じ権限なので採らない。
- **推奨（CLI）:** CLI の組み込みの Tool（Shell・編集・読み取り）は **最初の実装から止め**、CLI が使える Tool は PAW の Tool だけにする。PAW の Tool は MCP の Server（Backend 側の Bridge）として CLI に渡し、1 回の呼び出しごとに `NodeTools.call` で Tool Broker を通す（Grant・ACL・承認・Budget・Queue の Lease の Fencing: Decision 0006 / 0046 / 0057 をそのまま適用する）。Tool の実行は Local と同じ Sandbox の Container（Worktree だけを Mount、Network なし、認証情報なし）で行う。CLI の Process 自身は **別の** Container で動かす: Worktree を Mount せず、Network は Provider の Endpoint と MCP の Bridge だけ（Egress の Allowlist）。こうすると Model が選んだ操作はすべて Credential の見えない Tool の Container で動き、CLI の認証情報は Model が触れる Filesystem の外にある。組み込みの Tool を確実に止められること（Claude Code の Tool の制限と MCP の設定、Codex の MCP の設定と組み込みの Shell の無効化。Flag は実装時に公式の情報で確かめる）を CLI の版ごとに Test で確かめ、**止められない CLI・版は有効にしない**（fail-closed）。Subscription の認証情報は Secret Store にあり、呼び出しの間だけ CLI の Container の tmpfs（`CODEX_HOME` / `CLAUDE_CONFIG_DIR` に当たる場所、Mode 0700）に書き、終わったら消す。CLI が更新した Token（OAuth の Refresh）は、終わったときに Secret Store の Handle に書き戻す（`ConnectionService` の Credential の置き換えの内部の経路。Audit は `connection.replace` と同じ Event）。認証情報は Log・DB・Error・`NodeResult`・Tool の出力に出さず、CLI の出力は Decision 0016 の 1 の Redact を通してから使う。Git の Push の Credential はどの Container にも渡さない（Push は Integration の後に Backend が行う: Decision 0052）。CLI が Provider への通信を外部の Proxy で認証できる（認証情報を Proxy が付ける）なら、tmpfs に置く代わりにその形を採る（CLI の Container にも認証情報を置かずに済む）。実装の PR で CLI ごとに確かめる。
- Run の開始は Broker の 1 回の呼び出しとしても記録しない（Run 全体を 1 つの Tool 呼び出しとして許す形は、Decision 0006 の 1 回ごとの判定を飛ばすので採らない）。外部送信の記録は 1 の `CloudGate` の Placement と Audit（Decision 0048）が担う。
- 代替: (a) Sandbox なしで Backend の Process から直接実行する（Path の外への書き込み・Network を止められないので推奨しない）。(b) CLI の組み込みの Tool を Container の中で使わせ、Run の開始だけを Broker に記録する（個々の操作が Broker の承認・ACL・Budget・Fencing を通らず、Decision 0006 / 0046 に反する。同じ Container の Shell から認証情報の File が読めるので、Mode 0700 では守れない。推奨しない）。(c) Benchmark と同じく Backend の User が Docker を呼ぶ（Docker の Group は Host の root と同じ権限なので推奨しない）。

### 7. 承認が要る Tool 呼び出し

- **推奨:** Broker が `NEEDS_APPROVAL` を返したとき、Runtime は **待たない**（Local の Lease を承認待ちで持ち続けない）。Model には「承認が要るので実行していない」という固定の文を Tool の結果として返し、Model は他の作業を続けられる。Node の終わりに未承認の呼び出しが残っていれば、`NodeResult.unresolved_questions` に固定形式（Tool 名と承認の ID だけ。引数は入れない）で載せる。
  - 同じ試行の中で同じ呼び出しが再び出て、そのときには承認済みなら、Runtime は覚えている `approval_id` を付けて `NodeTools.call` する（承認は呼び出しの Hash に結び付いている: Decision 0006）。
  - 試行をまたいで承認を待って再開する仕組み（Task を `waiting`（承認）にし、承認で Node を続ける）は、この Decision では作らず、後続の Decision で決める。
- `DENY` は Model にそのまま「拒否された」と返し、同じ呼び出しの繰り返しは Loop 検知に任せる。
- 代替: 承認を Poll して待つ（Local の GPU を空いたまま押さえ、ほかの Node が Lease を取れない）。

### 8. Timeout と取り消し

- **推奨:**
  - 外側の上限は Orchestrator の Node の Timeout（Decision 0021 の 3）と Task の Budget。Adapter はそれより短い自分の上限を持つ: Local の 1 回の HTTP の呼び出しは「残り時間」と 15 分の小さい方、Tool の 1 回の実行は 300 秒（Benchmark と同じ。Broker の `ToolRunner` の Timeout）、CLI の 1 回の Run は Node の Timeout と同じ。
  - 取り消し（`asyncio.CancelledError`）を受けたら: Local は HTTP の Request を閉じる（vLLM は Client の切断で生成を止める）、Container の中の Command と CLI の Process は Process Group に SIGTERM、5 秒で SIGKILL、Container を止める。`HybridRuntime` の待ち（`CANCEL_GRACE_SECONDS` = 10 秒）より短く終える。取り消しは握りつぶさず上げる。
  - 取り消しの後も CLI が使った Token は分かった範囲で `ConnectionService` に記録する（`cancelled` の行。Decision 0016 の 9）。
- 代替: Adapter が上限を持たず Orchestrator の Timeout だけに任せる（1 回の HTTP が Node の残りを全部使い、Step の途中で切られて Token の記録が残らない）。

### 9. Test の方針

- **推奨:**
  - CI では GPU・Network・本物の CLI を使わない。Local は httpx の `MockTransport` の Fake の OpenAI 互換 Server（記録した応答列、Tool 呼び出し・Reasoning・`usage`・Error の本文を返す）で、CLI は Fake の CLI（決まった JSON を出す Script。終了 Code・Signal・遅延・Token の Refresh を真似る）で Test する。Sandbox は Fake の Runner で、実際の Container を使う Test は手動の Smoke に回す。
  - 固定するもの: 2 の Request の同等性、3 の方針の適用、4 の対応表の各行、5 の報告（二重にならない）、6 の Credential が Log・Error・`repr`・結果に出ないこと（Secret の形の文字列は分割して書く）と CLI の組み込みの Tool が止まっていること（Fake の CLI が組み込みの Tool を使おうとしたら失敗にする）、7 の承認の扱い、8 の取り消しで Process と Container が残らないこと、`NodeStopped` がそのまま上がること。
  - **GPU を使う Smoke Test は、#180 の Run が終わった後に、Human の許可を得てから** 行う。内容: 本番の `paw-llm-main.service`（12 の Flag を足したもの）で paw-seed-v1 の数 Task を `LocalAgentRuntime` に解かせ、Benchmark の Harness と Tool 呼び出しの形・Step 数が大きく違わないことを見る。CLI の Smoke は、Subscription の利用規約（Decision 0016 の前提）と使用量への影響を Human が確認した後。
- 代替: 本物の vLLM を CI で動かす（GPU が要り、#180 と共有の Machine で負荷が大きい）。

### 10. `NodeResult` の作り方と Commit

- **推奨:** Commit するのは Worker の Node（Runtime）で、Backend の自動 Commit ではない（Decision 0036 の 7 を変えない）。Model の `submit` の引数は `summary`（必須）・`changed_files`（必須。変えた File の一覧）と、任意の `discovered_facts`・`unresolved_questions`・`confidence`。Adapter は `submit` を受けたら、Node の Branch の **起点の Commit からの差分**（起点から `HEAD` までの Commit 済みの変更と、未 Commit の変更・未追跡の File の和。Broker の Read-only の Git の Tool）と `changed_files` を突き合わせる。起点は Worktree の準備で Branch を初めて作った時の Commit（上流の Worker の Branch を Merge した後）。Worktree の準備（`GitWorktreeCoordinator.prepare_node`）は、Branch を作り **上流の Worker の Branch の最初の Merge が終わった後に**、同じ Repository に起点を指す Ref（`refs/paw/base/<Branch の名前>`。Branch と同じ `paw/` の名前空間）を作る（今の Coordinator は Branch を作ってから上流を Merge するので、作る時に Ref を置くと起点が上流の変更より前になり、上流の File まで自分の変更として申告させることになる）。Ref は一度作ったら動かさず、再試行・Process の再起動で同じ Branch を再び渡すときは、今の `HEAD` ではなくこの Ref を読んで `NodeWorktree.base_commit` に入れる（今の `NodeWorktree` は `repo_id`・`path`・`branch`・`protected` だけ）。Decision 0036 のとおり状態の正本は git で、Table は足さない。Ref が無い（この変更より前に作られた Branch）ときは、その Node の試行を `WorktreeUnavailable` にして推測しない。`git status` だけで比べないのは、前の試行が Commit した変更を再試行で見失わないため。一致しない（申告にない変更・未追跡の File がある、申告した File に変更がない、Scope の外や `.git` を指す）ときは `submit` を受け付けず、その差を Model に返して直させる（3 回まで。それでも合わなければ `InvalidNodeResultError` で失敗）。一致したら、申告した Path のうち **未 Commit のものだけ** を指定して Broker の Git の Tool（`repo.git.commit`、決まった Commit Message と Author）で Commit する（未 Commit のものがなければ Commit しない）。`NodeResult.changed_files` は起点からの差分の全体、`NodeResult.commit` はその後の Branch の `HEAD` の SHA（起点と同じなら空）にする。こうすると前の試行の Commit を含めて、何度やり直しても同じ結果を Branch から作り直せる。Commit の後に未 Commit の変更が残ることはない（残れば Decision 0036 の 7 のとおり統合の前に `dirty` で止まり、Backend は自動で Commit も破棄もしない）。Model には Benchmark と同じく「Commit しない」と指示する（Model に Git の操作をさせない）。`test_result` は Model の申告を使わない（Evaluator の仕事。AGENTS.md の「Agent 自身の完了を成功判定に使わない」）。
- **複数の Repository の Worktree を持つ Node**（`NodeAssignment.worktrees` が 2 つ以上）: Worktree は 1 つなら Benchmark と同じく `/workspace` に、2 つ以上なら `/workspace/<repo_id>/`（`NodeWorktree.repo_id` の UUID）に置く。Repository の名前は Project の中でしか一意でなく、Task は別の Project の同じ名前の Repository を持ちうるので、名前は使わない（Model には System Prompt で Directory と Repository の対応を示す。表示名は Backend が Scope から渡す）。`changed_files` の Path は `/workspace` からの相対なので、2 つ以上のときは `<repo_id>/` が先頭に付き、同じ相対 Path も区別できる。Adapter は Repository ごとに突き合わせて、Repository ごとに 1 つの Commit を作る。`NodeResult.commit`（1 つの値、64 文字）は Repository が 1 つのときだけその SHA を入れ、2 つ以上のときは空にして、Repository ごとの Commit を `artifacts` に固定形式（`commit:<repo_id>:<sha>`）で 1 行ずつ入れる。統合は Node の Branch を読む（Decision 0036）ので、`commit` の値には依存しない。どれかの Repository で Commit に失敗したら Node を失敗にする。Commit 済みの他の Repository はそのままで、再試行では上の「起点からの差分」で突き合わせるので、先に Commit した Repository の変更も `changed_files` と `artifacts` に戻る（Commit を Repository をまたいで不可分にはしない）。
- Planner は `submit` の引数に Plan の JSON を入れ、`NodeOutcome.succeeded(result, plan=...)` で返す。
- 代替: (a) Model に Commit させる（Message・Author・`.git` の扱いが Model 任せになる）。(b) `submit` の後に Worktree の変更をすべて Commit する（申告にない生成物・一時 File を含みうる。Decision 0036 の 7 が退けた形なので推奨しない）。

### 11. PR の分け方（段階）

- **推奨:** 次の順で、1 段階 1 PR（どれも「Refs #208」。最後の段階で Closes）。
  1. `agents/` の共有部品: `ChatCompletionsClient`・`ErrorClassifier`（4）・`ReasoningHistoryPolicy`（3、既定は残す）と、#38 の Model の Interpreter の本番の配線（Tool なしの 1 回の呼び出し）。GPU なしの Test のみ。
  2. Tool Broker のファイル・Shell・Git の Tool と Executor、Sandbox の Runner（6。SSH の Wrapper の拡張を含む）。
  3. `LocalAgentRuntime`（2・5・7・8・10）、`NodeWorktree.base_commit`（Worktree の準備で記録）、`local_runtimes` への配線、`paw-llm-main.service` に `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足す（Deploy の変更は Human が反映する）。ここで Orchestrator の Worker（`Orchestrator.serve`）を起動するかどうかは、Decision 0047 の 5 のとおりこの段階の PR で提案する。
  4. `CloudGate`、`AgentSessionAdapter` と `ConnectionService.execute_session`、PAW の Tool の MCP の Bridge、`CodexCliRuntime` / `ClaudeCliRuntime`（1・6）。
  5. Human の許可を得た GPU の Smoke と、その記録（9）。
- 代替: 1 つの大きな PR にする（Review と Revert が難しく、#180 との GPU の調整も分けられない）。

### 12. Model の Server の設定

- **推奨:** 本番の `paw-llm-main.service` の起動の引数に、Benchmark と同じ `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足す（11 の 3 の PR）。`--max-model-len` などの Context の大きさは #200 の Decision に委ね、ここでは変えない。Adapter は Server の `served-model-name`（`main`）を Model の名前として使い、Placement の `model` には Deployment の実際の Model の ID（`Qwen3.8-27B-FP8`）を記録する。
- 代替: Adapter の側で Model の生の出力から Tool 呼び出しと Reasoning を取り出す（Parser を自前で持つことになり、Benchmark と形が変わる）。

## 決めてほしいこと

1. **3 つの Runtime を新しい `paw_backend/agents/` に置き、Local は `local_runtimes` で `HybridRuntime` に包み、CLI の 2 つは Ladder の段と `HybridRuntime` の `cloud` の両方で使う。どちらの経路でも共通の `CloudGate`（`CloudPolicy` → Placement の記録）を通し、`CloudPolicy` がなければ Cloud に送らない。CLI は必ず `ConnectionService` を通し、そのために Worktree を受け取る `AgentSessionAdapter` と `execute_session` を足す（`AdapterRequest` は変えない）。Runtime には Orchestrator が試行ごとに作る `NodeAssignment.connections`（Fencing 済みの `TaskContext` を中に持つ口）だけを渡す。用途は Worker・Planner が `coding`、Reviewer が `review`、Researcher が `research`**（1）でよいか。推奨: はい。
2. **Local の Tool loop は Benchmark の Harness のコードではなく振る舞いを asyncio で移し（Prompt・4 つの Tool・切り詰め・催促・60 Step・16,384 / 120,000 token は同じ）、Tool の実行はすべて Tool Broker に足すファイル・Shell の Tool を通す。外側の時間の上限は Node の Timeout（30 分）**（2）でよいか。推奨: はい。
3. **Reasoning の履歴は `ReasoningHistoryPolicy` の差し込み口だけを作り、既定は Benchmark と同じ「残す」。方針の選択は #200 の Decision（0076）に委ねる。Reasoning は保存・Log しない**（3）でよいか。推奨: はい。
4. **失敗の分類は 4 の表のとおり（CUDA OOM・OOM Killer・Sandbox の cgroup の OOM は `AgentOutOfMemory`、Context・Step の上限は `RuntimeError`）。`retryable=False` は使わず、理由ごとの固定の文で Loop 検知に上の段への Escalation を選ばせる**（4）でよいか。推奨: はい。
5. **Local は応答ごとに `prompt_tokens + completion_tokens` をその場で `TOKENS` として報告し（Prefix Cache は引かない。`usage` が欠けた応答は 0 ではなく見積もり）、GPU 時間は `HybridRuntime` に任せる。CLI の Token は `ConnectionService` だけが計上し、Runtime は二重に報告しない。CLI は開始の前に Task の Token の残りが最低の枠（暫定 50,000）以上あることを確かめ、Turn ごとの `usage` をその場で計上して Budget が尽きたら止める。Turn ごとの Token を報告しない CLI・版は有効にせず、1 回の Run で数が欠けたら 0 ではなく控えめな見積もり（暫定 200,000）を計上する**（5）でよいか。推奨: はい。
6. **Shell・ファイルの Tool は、Task の作成者の Linux の Account で起動する Node ごとの Container（書き込む Node は自分の Worktree だけ、Worktree のない Node は Scope の Repository の Root を読み取り専用で Mount、Network なし、認証情報なし）で動かし、経路は Decision 0029 の SSH の Wrapper を広げる。CLI は組み込みの Tool を最初から止め、PAW の Tool だけを MCP の Bridge で渡して 1 回ずつ Tool Broker を通す（止められない CLI・版は有効にしない）。CLI の Process は Worktree のない別の Container（Provider と Bridge だけに出られる）で動かし、認証情報は呼び出しの間だけその tmpfs に置く（Proxy で付けられるならその形）。Refresh された Token は Secret Store に書き戻す**（6）でよいか。推奨: はい。
7. **承認が要る呼び出しは待たずに「実行していない」と Model に返し、残った承認は `unresolved_questions` に Tool 名と承認の ID だけで載せる。試行をまたいで承認を待って再開する仕組みは後続の Decision**（7）でよいか。推奨: はい。
8. **Adapter の上限（HTTP は残り時間と 15 分の小さい方、Tool は 300 秒、CLI は Node の Timeout）と、取り消しで SIGTERM → 5 秒で SIGKILL → Container の停止（`HybridRuntime` の 10 秒の待ちより短く）**（8）でよいか。推奨: はい（数値は暫定）。
9. **CI は Fake の Server・Fake の CLI・Fake の Sandbox だけで Test し、GPU の Smoke は #180 の Run の後に Human の許可を得てから、CLI の Smoke は利用規約と使用量への影響を Human が確認した後に行う**（9）でよいか。推奨: はい。
10. **Commit は Worker の Node（Runtime）が行い、Backend の自動 Commit にはしない（Decision 0036 の 7 のまま）。Model は `submit`（CLI では Bridge が受けて Broker に渡さない MCP の Tool。受け付けたら以後の呼び出しを拒否して CLI を止める）で `changed_files` を申告し、Adapter は Branch の起点の Commit（Worktree の準備が `refs/paw/base/<Branch>` に残し、`NodeWorktree.base_commit` で渡す）からの差分と突き合わせ、一致したときだけ申告した Path のうち未 Commit のものを Broker の Git の Tool で Commit する（合わなければ Model に直させ、3 回で失敗）。複数の Repository の Node は `/workspace/<repo_id>/` に置き、その Path で申告し、Repository ごとに Commit して SHA を `artifacts` に入れる。`test_result` は Model の申告を使わない**（10）でよいか。推奨: はい。
11. **11 の 5 段階で PR を分ける（共有部品と #38 の Interpreter → Broker の Tool と Sandbox → Local の Runtime と Server の設定 → CLI の Runtime → GPU の Smoke）**（11）でよいか。推奨: はい。
12. **本番の `paw-llm-main.service` に `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足し、Context の大きさは #200 に委ねる**（12）でよいか。推奨: はい。

## リスク

- Benchmark と Model から見える形を同じにしても、Tool の実行の経路（Broker・Sandbox の違い、承認で実行されない呼び出し）が違うので、本番の解ける割合は Benchmark の数値と同じとは限らない。9 の Smoke で大きな差がないことを見る。
- CLI の組み込みの Tool を止めて MCP の Tool だけで動かすと、CLI 本来の Tool を使う場合より解ける割合が下がるかもしれない。CLI の Smoke で確かめ、下がる場合も Broker を通らない形には戻さず、Tool の形を見直す。
- CLI の認証の形（OAuth の File の場所・Refresh の仕方）と Option は CLI の版で変わりうる。実装の PR でその時点の公式の情報を確かめ、Fake の CLI の Test を合わせる。
- Rootless の Container を Task の作成者の Account で動かす準備（Image の配布・cgroup の委任）が Host ごとに要る。Deploy の手順に入れる。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
