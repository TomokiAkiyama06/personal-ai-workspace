# Orchestrator の実 Runtime Adapter（Local の Main Model on vLLM・Codex CLI・Claude Code CLI）の設計（境界、Local の Tool loop、Reasoning の履歴の差し込み口、使用量の報告、OOM と Error の分類、Credential と Sandbox、承認、Timeout と取り消し、Test、PR の分け方）

- Status: Proposed
- Date: 2026-10-08
- Scope: Issue [#208](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/208)。この Decision は設計だけで、実装は承認の後に下の 11 の段階ごとの PR で行う。関係する既存の口: `apps/backend/paw_backend/orchestrator/runtime.py`（`AgentRuntime`・`NodeAssignment`・`NodeOutcome`・`NodeTools`・`NodeBudget`・`NodePlacement`）、`compute/runtimes.py`（`HybridRuntime`）、`compute/wiring.py`（`LocalRuntime`）、`orchestrator/composition.py`（`local_runtimes`・`agent_runtimes`）、`orchestrator/errors.py`（`RUNTIME_ERROR_CLASSES`・`AGENT_OUT_OF_MEMORY`）、`connections/`（`ConnectionAdapter`・`ConnectionService.execute`）、`tools/`（Tool Broker・`ToolRunner`・Registry）、`apps/backend/deploy/systemd/paw-llm-main.service`
- Supersedes: なし。[Decision 0021](0021-dag-orchestrator-policy.md)（Runtime の Protocol・Error Class・Node の Timeout）、[Decision 0016](0016-shared-connection-adapter-policy.md)（共有 Connection・Credential・Quota）、[Decision 0006](0006-tool-broker-policy.md)（Tool Broker）、[Decision 0047](0047-task-execution-composition-and-task-end-effects.md) の 5（本番の Runtime はまだない）、[Decision 0048](0048-node-placement-audit.md)（Placement の記録）、[Decision 0058](0058-compute-scheduler-application-wiring.md) の 7（`local_runtimes`）、[Decision 0071](0071-agent-oom-and-escalation-records.md)（`AgentOutOfMemory`）、[Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md)（Main は Qwen3.8-27B-FP8）は書き換えず、その中で Runtime を作る。Budget / 使用量の記録は Proposed の Decision 0077（PR #207）を前提にし、Reasoning の履歴の方針そのものは #200 の Decision（0076 を予約済み）に委ねる

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
  - `CodexCliRuntime` / `ClaudeCliRuntime`: CLI を 1 回の Node の試行として動かす。Ladder の上の段（`agent_runtimes` に Label で渡す）と、`HybridRuntime` の `cloud`（Local が混んでいるときの振り替え）の両方で使える。どちらで呼ばれても、Cloud に送る前に `placement.record(CLOUD, agent="codex" | "claude", model=...)` が済んでいること（Decision 0048）。`placement` のない呼び出し（Planner）では Cloud に送らず `COMPUTE_UNAVAILABLE` 相当で失敗にする（`HybridRuntime` と同じ fail-closed）。Ladder から直接呼ばれる場合は Runtime 自身が `record` する。
  - 共有部品: `ChatCompletionsClient`（vLLM への 1 回の呼び出し。httpx の非同期 Client。Tool なしの 1 回の呼び出しは #38 の Interpreter と Planner も使う）、`ErrorClassifier`（4）、`ReasoningHistoryPolicy`（3）、`NodeResult` の組み立て（10）。
- CLI の 2 つは **`ConnectionService` を必ず通す**（Admission・Quota・使用量の行・Task の Budget の `TOKENS`・`Secret` の取り扱いを 1 か所に保つ）。今の `ConnectionAdapter.run(secret, AdapterRequest)` は Prompt → 文の 1 回の呼び出しなので、Worktree と Sandbox を受け取る別の口 `AgentSessionAdapter.run_session(secret, AgentSessionRequest) -> AgentSessionResult`（最終の文・入出力の Token・終了の分類）と、`ConnectionService.execute_session`（`execute` と同じ順の確認・記録。用途の Category は Node の Role から `coding` / `review`）を足す。`AdapterRequest` の型は変えない。
- 代替: (a) CLI の Runtime が `ConnectionService` を通らず直接 CLI を起動する（Quota・使用量・Credential の規則が 2 か所に分かれるので推奨しない）。(b) Local も `ConnectionAdapter`（`ConnectionKind` に `local` を足す）として作る（共有 Connection の Admission・Quota・Reaper は Local に当てはまらない。Decision 0077 の 1 の代替 (a) と同じ理由で推奨しない）。

### 2. Local の Tool loop: Benchmark の Harness の「振る舞い」を移し、実行は Tool Broker を通す

- **推奨:** paw-bench の `coding_harness.py` のコードはそのまま使わず（Repository の外・同期の urllib と Thread・Docker を直接呼ぶ）、`LocalAgentRuntime` に asyncio で書き直す。ただし **Model から見える形は Benchmark と同じにする**（Decision 0074 の数値はその形で測ったため）。
  - 同じにするもの: System Prompt の構成（Worktree の場所の言い換えだけ変える）、Tool の名前と引数（`bash`・`str_replace`・`write_file`・`submit`）、`tool_choice: auto`、Tool の出力の切り詰め（12,000 文字、前後を残す）、Tool を呼ばない応答への催促（3 回で終わり）、Step の上限 60、1 回の応答の `max_tokens` 16,384、Prompt の上限 120,000 token、Sampling は Model の `generation_config` の既定。
  - 変えるもの: Tool の実行は Docker を直接呼ばず、すべて `NodeTools.call` で Tool Broker を通す。そのために Broker に Tool と Executor を足す（`repo.file.str_replace` / `repo.file.write`: `write`、`repo.shell.run`: `execute`。Decision 0006 の表で範囲内は `SCOPED_AUTO`。Path は Node の Scope（Worktree）の中だけ、`.git` は編集不可）。`submit` は Broker を通らない Adapter の内部の Tool。
  - 壁時計の上限は Benchmark の 45 分ではなく、Orchestrator の Node の Timeout（Decision 0021 の 3、30 分）を外側の上限とし、Adapter は残り時間で HTTP の Timeout を決める。
  - 同等性は Test で固定する: 記録した会話（Fake の Server の応答列）を与え、送る Request の Message・Tool の定義・Option が Benchmark の Harness と一致することを確かめる。
- Read-only の Role（Planner・Reviewer など、Worktree のない Node）は同じ loop で、書き込みの Tool を渡さない（`bash` も Read-only の Executor にする。7 の Sandbox の読み取り専用の Mount）。
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
  | Context の上限（Prompt の上限や vLLM の `maximum context length`）、Step の上限、Tool を呼ばない応答の繰り返し | `RuntimeError` | いいえ（同じ方法の再試行は無駄。Loop 検知と Escalation は Orchestrator が決める） |
  | 応答が JSON でない・Tool 呼び出しの形が壊れている（3 回続いたら） | `ValueError` | はい |
  | CLI の Provider の Rate limit・Credential の失効 | `ConnectionService` の `FailureCode`（`rate_limited` / `expired`）で記録し、Runtime は `ConnectionError` | Rate limit ははい、失効はいいえ |
  | `submit` の引数が `NodeResult` として不正 | `InvalidNodeResultError` | はい |
  | その他 | `AdapterError` | はい |

  - Python の `MemoryError` は `AgentOutOfMemory` と同じに数えられる（Decision 0071 の 1）。Adapter は自分の `MemoryError` を握りつぶさない。
  - `NodeStopped`（Budget の超過・Task の終了）は捕まえずにそのまま上げる（`runtime.py` の規則）。
- 代替: OOM を System Health の側だけで見て、Adapter は報告しない（どの Node の失敗か分からず、Decision 0071 の記録が付かない）。

### 5. Budget・Token・GPU 時間の報告（Decision 0077 を前提）

- **推奨:**
  - Local: vLLM の応答ごとに、`usage.prompt_tokens + usage.completion_tokens` を `NodeBudget.charge(TOKENS, n)` で **その場で** 報告する（Node の終わりにまとめない。途中で止まっても使った分が残り、Budget の超過でその場で止まる）。Prefix Cache の当たりは引かない（Server が処理した量として数える。Codex / Claude の「入力 + 出力」と同じ数え方）。`HybridRuntime` の包みがこれを数えて `local_usage` に入れる（Decision 0077 の 2）。`usage` のない応答は 0。
  - GPU 時間: Adapter は報告しない（`HybridRuntime` が Lease の時間を数える）。
  - CLI: Token は CLI の機械読みの出力（JSON の `usage`）から `AgentSessionResult` に入れ、`ConnectionService` が使用量の行と Task の Budget の `TOKENS` に計上する。**Runtime は `NodeBudget` に二重に報告しない。** CLI が数を出さないときは `None`（Decision 0016 の 5: 0 として数える）。
- 代替: Node の終わりに合計を 1 回だけ報告する（途中で止まった分が失われ、長い Node が Budget を超えて走る）。

### 6. Credential と Sandbox

- **推奨（Local の Shell・ファイルの Tool）:** `repo.shell.run` などの Executor は、Node ごとの Container（`--network none`、CPU / Memory / PID の上限、Worktree だけを Mount、Read-only の Role は読み取り専用で Mount）の中で動かす。Container は **Task の作成者の Linux の Account で** 起動する（Worktree はその Account の持ち物: Decision 0017 / 0029）。経路は Decision 0029 の SSH の Wrapper を広げ、許す操作を「決まった Image の Sandbox の起動・その中での実行・停止」に限る（Rootless の Podman を想定。実装の PR で確かめる）。Backend の User が Docker を直接呼ぶ形（Benchmark の形）は、Docker の Group が Host の root と同じ権限なので採らない。
- **推奨（CLI）:** CLI は同じ形の Container の中で動かす。違いは Network で、Provider の Endpoint だけに出られる（Egress の Proxy の Allowlist）。Subscription の認証情報は Secret Store にあり、呼び出しの間だけ Container の中の tmpfs（`CODEX_HOME` / `CLAUDE_CONFIG_DIR` に当たる場所、Mode 0700）に書き、終わったら消す。CLI が更新した Token（OAuth の Refresh）は、終わったときに Secret Store の Handle に書き戻す（`ConnectionService` の Credential の置き換えの内部の経路。Audit は `connection.replace` と同じ Event）。認証情報は Log・DB・Error・`NodeResult`・Tool の出力に出さず、CLI の出力は Decision 0016 の 1 の Redact を通してから使う。Git の Push の Credential は Container に渡さない（Push は Integration の後に Backend が行う: Decision 0052）。
- **推奨（CLI 自身の Tool）:** 最初の実装では、CLI の組み込みの Tool（Shell・編集）を **Container の中に限って** 使わせ、Broker の 1 回の呼び出し `agent.cli.run`（`execute` + `network` + `credential-use`、範囲内なら `SCOPED_AUTO`）で Run の開始を記録する。CLI の個々の Tool 呼び出しは Broker を通らないが、触れられるのは専用の Worktree（専用の Branch）と Provider の Endpoint だけで、Push・他の Repository・他の Host には届かない。後続で、CLI の組み込みの Tool を止めて PAW の Tool を MCP で渡し、1 つずつ Broker を通す形（Claude Code の Tool の制限と MCP の設定、Codex の MCP の設定。Flag は実装時に確認）を検討する。
- 代替: (a) Sandbox なしで Backend の Process から直接実行する（Path の外への書き込み・Network を止められないので推奨しない）。(b) 最初から MCP の形にする（CLI の組み込みの Tool を確実に止められるかが CLI ごと・版ごとに違い、確かめるまで品質が読めない）。(c) Benchmark と同じく Backend の User が Docker を呼ぶ（上の理由で推奨しない）。

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
  - 固定するもの: 2 の Request の同等性、3 の方針の適用、4 の対応表の各行、5 の報告（二重にならない）、6 の Credential が Log・Error・`repr`・結果に出ないこと（Secret の形の文字列は分割して書く）、7 の承認の扱い、8 の取り消しで Process と Container が残らないこと、`NodeStopped` がそのまま上がること。
  - **GPU を使う Smoke Test は、#180 の Run が終わった後に、Human の許可を得てから** 行う。内容: 本番の `paw-llm-main.service`（12 の Flag を足したもの）で paw-seed-v1 の数 Task を `LocalAgentRuntime` に解かせ、Benchmark の Harness と Tool 呼び出しの形・Step 数が大きく違わないことを見る。CLI の Smoke は、Subscription の利用規約（Decision 0016 の前提）と使用量への影響を Human が確認した後。
- 代替: 本物の vLLM を CI で動かす（GPU が要り、#180 と共有の Machine で負荷が大きい）。

### 10. `NodeResult` の作り方と Commit

- **推奨:** Model の `submit` の引数は `summary`（必須）と、任意の `discovered_facts`・`unresolved_questions`・`confidence` だけにする。`changed_files` と `commit` は Adapter が Worktree から作る: `submit` の後、Adapter が Broker の Git の Tool（`repo.git.commit`、決まった Commit Message と Author）で Commit し、変更の一覧と Commit の SHA を `NodeResult` に入れる。Model には Benchmark と同じく「Commit しない」と指示する（Model に Git の操作をさせない）。`test_result` は Model の申告を使わない（Evaluator の仕事。AGENTS.md の「Agent 自身の完了を成功判定に使わない」）。
- Planner は `submit` の引数に Plan の JSON を入れ、`NodeOutcome.succeeded(result, plan=...)` で返す。
- 代替: Model に Commit させる（Message・Author・`.git` の扱いが Model 任せになる）。

### 11. PR の分け方（段階）

- **推奨:** 次の順で、1 段階 1 PR（どれも「Refs #208」。最後の段階で Closes）。
  1. `agents/` の共有部品: `ChatCompletionsClient`・`ErrorClassifier`（4）・`ReasoningHistoryPolicy`（3、既定は残す）と、#38 の Model の Interpreter の本番の配線（Tool なしの 1 回の呼び出し）。GPU なしの Test のみ。
  2. Tool Broker のファイル・Shell・Git の Tool と Executor、Sandbox の Runner（6。SSH の Wrapper の拡張を含む）。
  3. `LocalAgentRuntime`（2・5・7・8・10）、`local_runtimes` への配線、`paw-llm-main.service` に `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足す（Deploy の変更は Human が反映する）。ここで Orchestrator の Worker（`Orchestrator.serve`）を起動するかどうかは、Decision 0047 の 5 のとおりこの段階の PR で提案する。
  4. `AgentSessionAdapter` と `ConnectionService.execute_session`、`CodexCliRuntime` / `ClaudeCliRuntime`（1・6）。
  5. Human の許可を得た GPU の Smoke と、その記録（9）。
- 代替: 1 つの大きな PR にする（Review と Revert が難しく、#180 との GPU の調整も分けられない）。

### 12. Model の Server の設定

- **推奨:** 本番の `paw-llm-main.service` の起動の引数に、Benchmark と同じ `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足す（11 の 3 の PR）。`--max-model-len` などの Context の大きさは #200 の Decision に委ね、ここでは変えない。Adapter は Server の `served-model-name`（`main`）を Model の名前として使い、Placement の `model` には Deployment の実際の Model の ID（`Qwen3.8-27B-FP8`）を記録する。
- 代替: Adapter の側で Model の生の出力から Tool 呼び出しと Reasoning を取り出す（Parser を自前で持つことになり、Benchmark と形が変わる）。

## 決めてほしいこと

1. **3 つの Runtime を新しい `paw_backend/agents/` に置き、Local は `local_runtimes` で `HybridRuntime` に包み、CLI の 2 つは Ladder の段と `HybridRuntime` の `cloud` の両方で使う。CLI は必ず `ConnectionService` を通し、そのために Worktree を受け取る `AgentSessionAdapter` と `execute_session` を足す（`AdapterRequest` は変えない）**（1）でよいか。推奨: はい。
2. **Local の Tool loop は Benchmark の Harness のコードではなく振る舞いを asyncio で移し（Prompt・4 つの Tool・切り詰め・催促・60 Step・16,384 / 120,000 token は同じ）、Tool の実行はすべて Tool Broker に足すファイル・Shell の Tool を通す。外側の時間の上限は Node の Timeout（30 分）**（2）でよいか。推奨: はい。
3. **Reasoning の履歴は `ReasoningHistoryPolicy` の差し込み口だけを作り、既定は Benchmark と同じ「残す」。方針の選択は #200 の Decision（0076）に委ねる。Reasoning は保存・Log しない**（3）でよいか。推奨: はい。
4. **失敗の分類は 4 の表のとおり（CUDA OOM・OOM Killer・Sandbox の cgroup の OOM は `AgentOutOfMemory`、Context・Step の上限は `RuntimeError` で再試行しない など）**（4）でよいか。推奨: はい。
5. **Local は応答ごとに `prompt_tokens + completion_tokens` をその場で `TOKENS` として報告し（Prefix Cache は引かない）、GPU 時間は `HybridRuntime` に任せる。CLI の Token は `ConnectionService` だけが計上し、Runtime は二重に報告しない**（5）でよいか。推奨: はい。
6. **Shell・ファイルの Tool と CLI は、Task の作成者の Linux の Account で起動する Node ごとの Container（Worktree だけを Mount。Local は Network なし、CLI は Provider の Endpoint だけ）で動かし、経路は Decision 0029 の SSH の Wrapper を広げる。CLI の認証情報は呼び出しの間だけ tmpfs に置き、Refresh された Token は Secret Store に書き戻す。CLI の組み込みの Tool は最初は Container の中に限って使わせ、Run の開始を Broker の `agent.cli.run` で記録する（MCP で 1 つずつ Broker を通す形は後続）**（6）でよいか。推奨: はい。
7. **承認が要る呼び出しは待たずに「実行していない」と Model に返し、残った承認は `unresolved_questions` に Tool 名と承認の ID だけで載せる。試行をまたいで承認を待って再開する仕組みは後続の Decision**（7）でよいか。推奨: はい。
8. **Adapter の上限（HTTP は残り時間と 15 分の小さい方、Tool は 300 秒、CLI は Node の Timeout）と、取り消しで SIGTERM → 5 秒で SIGKILL → Container の停止（`HybridRuntime` の 10 秒の待ちより短く）**（8）でよいか。推奨: はい（数値は暫定）。
9. **CI は Fake の Server・Fake の CLI・Fake の Sandbox だけで Test し、GPU の Smoke は #180 の Run の後に Human の許可を得てから、CLI の Smoke は利用規約と使用量への影響を Human が確認した後に行う**（9）でよいか。推奨: はい。
10. **Model の `submit` は `summary` などだけを返し、`changed_files` と `commit` は Adapter が Broker の Git の Tool で Commit して作る。`test_result` は Model の申告を使わない**（10）でよいか。推奨: はい。
11. **11 の 5 段階で PR を分ける（共有部品と #38 の Interpreter → Broker の Tool と Sandbox → Local の Runtime と Server の設定 → CLI の Runtime → GPU の Smoke）**（11）でよいか。推奨: はい。
12. **本番の `paw-llm-main.service` に `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` を足し、Context の大きさは #200 に委ねる**（12）でよいか。推奨: はい。

## リスク

- Benchmark と Model から見える形を同じにしても、Tool の実行の経路（Broker・Sandbox の違い、承認で実行されない呼び出し）が違うので、本番の解ける割合は Benchmark の数値と同じとは限らない。9 の Smoke で大きな差がないことを見る。
- CLI の組み込みの Tool を Container の中で使わせる間は、個々の Tool 呼び出しが Broker の Audit に残らない（Run の開始だけが残る）。Worktree の差分は Integration と Review で見る。
- CLI の認証の形（OAuth の File の場所・Refresh の仕方）と Option は CLI の版で変わりうる。実装の PR でその時点の公式の情報を確かめ、Fake の CLI の Test を合わせる。
- Rootless の Container を Task の作成者の Account で動かす準備（Image の配布・cgroup の委任）が Host ごとに要る。Deploy の手順に入れる。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
