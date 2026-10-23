# DAG Orchestrator を、Claude が計画して Node ごとに Agent を割り当てる形にする（本番の `CloudPolicy`（すべての Project を許す）、Claude Code の CLI の Planner と その記録、Plan の `agent` と Ladder、Backend が保証する独立した Review、Prompt Injection と権限、PR の分け方）

- Status: Approved
- Approval: 2026-10-23、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨通りで」と回答して承認（approved as recommended, all 6 points: 1 は A（Backend が Claude と Codex の 2 つの独立した Reviewer の Node を並列に足し、どちらも必須）、2 は A（Researcher の候補は `local`・`claude`、既定 `local`、Local から Claude へ Escalation）、3 は A（Claude の Planner が使えなければ 2 回目を Local の Planner が行い、Log に残す）、4 は A（Worker の Ladder は `["local", "codex"]`。Codex が実装した Node があれば Codex の Review は独立でなく、Task は `failed` で Human が判断）、5 は A（すべての Project への Cloud の送信は、配備の有効な User がちょうど 1 人の間だけ）、6 は A（Reviewer に、上流の Worker の Branch を合わせた読み取り専用の Worktree を用意し、`refs/paw/review-base/<Branch>` から `HEAD` までの差分を読ませる）。Codex review（5ef5116）の P1・P2（Planner の呼び出しの識別に `retry_count`、Review の差分の起点、Fence された呼び出しの行）は承認の前に本文へ反映した。末尾の「承認時の決定」）
- Date: 2026-10-23（承認 2026-10-23）
- Scope: Issue [#297](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/297)。この Decision は設計だけで、コードは承認の後の下の「PR の分け方」の PR で書く。関係する既存の口: `apps/backend/paw_backend/orchestrator/orchestrator.py`（`_plan`・`_spec_of`・`_placement`・`_node_connections`・`_settle_failure`）、`orchestrator/plan.py`（`NODE_FIELDS`・`Plan.from_mapping`）、`orchestrator/config.py`（`OrchestratorConfig.ladders`）、`orchestrator/placement.py`（`NodePlacementHandle`・`content_digest`）、`orchestrator/domain.py`（`NodeRole`・`ROLE_CEILING`・`ExecutionPlacement`）、`orchestrator/audit.py`（`CloudSend`）、`agents/cloud.py`（`CloudGate`）、`agents/claude_cli.py`（`ClaudeCliRuntime`。今は Planner を `CLOUD_POLICY_DENIED` で断る）、`agents/codex_cli.py`、`agents/toolkit.py`（`submit_plan`）、`agents/local.py`（`PLANNER_PROMPT`）、`agents/wiring.py`、`compute/runtimes.py`（`HybridRuntime`・`CloudPolicy`）、`runtime_config.py`（`[agents.*]` を `cli runtimes are not available yet` で拒否）、`apps/backend/deploy/runtime/runtime.example.toml`
- Supersedes（一部）:
  - [Decision 0021](0021-dag-orchestrator-policy.md)（Approved）の 1 の Plan の形に、Node ごとの任意の Field `agent` を足す（下の 4）。0021 の 1 の「Agent の起動・Permission の付与・Worktree の割当・並列数は Backend が決める」は変えない（Planner は **Backend が決めた候補の中から提案するだけ** で、起動するかどうかは Backend が決める）。
  - 0021 の 3 の「Node は 1 段目で始まり」を「Node は割り当てられた Agent の段で始まる（割り当てがなければ 1 段目）」に、「Review の独立性は Reviewer の Ladder の並べ方で表す（Orchestrator は強制しない）」を「Backend が強制する」（下の 5）に置き換える。0021 の 3 のその他（Loop 検知・試行 6 回・Back-off・Timeout・Planner の 2 回の試行と 2 回目は Planner の Ladder の次の段）と、0083 の 4 の `escalate` は変えない（Human の回答 1: 実行中の Retry / Escalation は今の規則のまま。動的な再計画はしない）。
  - [Decision 0083](0083-agent-runtime-adapters.md)（Approved）の 1 の「CLI は Planner の Ladder には置かない」「組み立てが Planner の Ladder に CLI の Label があれば起動時に拒否する」と、10 の「Planner（Local だけ。1）」を、**Claude の CLI に限って** 置き換える（下の 3。Codex は Planner の Ladder に置けないまま）。0083 の 1 の `CloudGate` の順序（`CloudPolicy` → Admission → Placement の記録 → 起動）は変えず、Planner の呼び出しにも同じ順序を課す。
  - [Decision 0048](0048-node-placement-audit.md)（Approved）の 5「Planner の呼び出しは Cloud へ回さない」「Planner を Cloud で走らせるには Planner の呼び出しの記録が要る。今回は含めない」を、下の 3 の Planner の呼び出しの記録（新しい Table と同じ形の Audit）で置き換える。`HybridRuntime` が `placement` のない Assignment を Cloud へ回さない規則は変えない。
  - [Decision 0093](0093-production-runtime-config.md)（Approved）の 1 の表の「`[agents.codex]` / `[agents.claude]`（予約）。今は書けば拒否」と 3 の「CLI を有効にするときは、`CloudPolicy` の Decision と CLI の Smoke の後に、この File の Schema に Section を足す新しい Decision を作る」を、下の 7 の Section（`[cloud]`・`[agents.claude]`・`[agents.codex]`・`[orchestrator.assignment]`）で満たす。**CLI の段が実際に動くのは、0093 の 3 のとおり CLI の Smoke の後**（下の「PR の分け方」の 6）。`[orchestrator.ladders]` の Label が有効な Runtime の Label であること、File の保護と Fail closed（0093 の 2）は変えない。
  - [Decision 0037](0037-gpu-compute-scheduler.md)（Approved）の 14 は書き換えない。その条件（Placement が Audit に残る）は 0048 で満たされており（0048 の 6）、この Decision は 0048 の 6 が残した「`CloudPolicy` の本番の実装」を決める。0037 の 8 の `HybridRuntime` の Cloud への振り替え（Local が混んでいたら Cloud へ）は **使わない** 推奨にする（下の 4 の (5)。設定で `cloud` を渡さないだけで、0037 は変えない）。
  - 次は書き換えず、その中で作る: [Decision 0016](0016-shared-connection-adapter-policy.md)（共有 Connection・Quota・Admin の無効化）、[Decision 0087](0087-cli-secret-store-and-egress.md) / [Decision 0089](0089-codex-cli-runtime-route.md)（CLI の Credential・Egress・Codex の経路）、[Decision 0074](0074-seed-v2-and-qwen-27b-comparison.md)（Main は Qwen3.8-27B-FP8）、[Decision 0094](0094-task-create-entry-open-points.md)（読み取りだけの Task は Worker の Node を持たない）、[Decision 0098](0098-read-only-task-result-api-and-screen.md) の 8（Credential の形の `key` / `title` の拒否）。[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 背景

Human は 2026-10-23 に、DAG Orchestrator を「**Claude が Task を計画し、Node ごとにどの Agent が実行するかを割り当てる**」形に作り直したいと求め、次を決めた（Issue #297）。

1. Claude は **計画と Node ごとの割り当て** を行う。DAG は最初に 1 回だけ作り、Node ごとに実行する Agent を名指しする。実行中の Retry / Escalation は Backend の今の規則（Decision 0021 の 3）のままで、動的な再計画はしない。
2. 割り当ての型: **計画と割り当て = Claude、実装（Worker）= Local（vLLM の Qwen3.8-27B、Decision 0074）、Review = Claude と Codex**。Researcher は決めていない（推奨を出して確かめる）。
3. Cloud への送信の範囲: **すべての Project を許す**（1 人で使う配備）。外部への送信は今までどおりすべて記録する（Placement・Audit）。除外 Path・保護された File・Secret は送らない。

今の承認済みの設計は、これと次の点で合わない。

- **Plan に Agent の Field がない。** Decision 0021 の 1 の Plan の Node は `key`・`role`・`title`・`goal`・`depends_on`・`required`・`input`・`capabilities`・`repositories` だけで、未知の Field は拒否する（`plan.py` の `NODE_FIELDS`。Docstring は「Planner は Agent を選ばない」）。Node の Agent は Role ごとの Ladder（`OrchestratorConfig.ladders`）の段で決まり、Node は 1 段目で始まる（`_spec_of` の `ladder[attempt.agent_index]`）。
- **Planner は Local だけ。** Decision 0083 の 1 は「Planner の呼び出しには `placement` がなく、外部送信を記録できないので Cloud に送れない」とし、`ClaudeCliRuntime._run` は Planner の Role を `CLOUD_POLICY_DENIED` で断る。`_placement` と `_node_connections` は Planner の呼び出し（`spec.node is None`）に `None` を返す。Decision 0048 の 5 も同じ。
- **本番の `CloudPolicy` がない。** `CloudGate` は `CloudPolicy` がなければすべての送信を断る（Decision 0037 の 14、0083 の 1）。`runtime_config.py` は `[agents.*]` を `cli runtimes are not available yet` で拒否する（Decision 0093 の 3）。本番の `runtime.toml`（0093 の 4 の例と同じ）の Ladder は 4 つの Role とも `["local"]`。
- **Review の独立性は Orchestrator が強制しない**（0021 の 3、`OrchestratorConfig` の Docstring）。
- **Reviewer の Node は Worker の変更を読めない。** これは今回見つけた既存の隙間である。Worker の Node だけが専用の Worktree を持ち（`_prepare_worktrees` は `NodeRole.WORKER` だけ）、Reviewer（Worktree のない Node）は User の Checkout を読み取り専用で読む（Decision 0083 の 6）。Git の Shim は Worktree のない Node に `HEAD` だけを許し `show` を許さない（0083 の 2）。Reviewer が受け取るのは上流の `NodeResult`（`summary`・`changed_files`・`commit` の SHA など）だけで、**差分そのものは見えない**。Claude と Codex に Review させても、今のままでは Worker の要約を読むだけになる。
- CLI の Runtime（`ClaudeCliRuntime` #235、`CodexCliRuntime` #242）は Merge 済みだが「組み立てでは無効」で、CLI の Smoke と Provider の規約の確認（0083 の「Human に確かめてほしいこと」）はまだである。

要件との関係: REQUIREMENTS.md の「Logical roles」は「Codex / Claude 等も Adapter 経由で任意 Role に割当可能」「Planner は並列化・分解を提案できるが、実際の Agent 起動、Permission 付与、worktree 割当、並列数決定は Backend Orchestrator が行う」と定める。AGENTS.md の基本原則は Local-first だが「品質を犠牲にしてまで Local に固執しない」。「Review independence」は「Merge Ready 判定に利用する Reviewer は可能な限り実装 Agent と分離する」。Human の 3 つの回答は、これらの範囲で Planner の **提案** の幅を広げるもので、Backend の決定権は残す。[AGENTS.md](../../AGENTS.md) の 14 に従い、推奨をここに置いて承認を求める。

## 決定（承認された全体）

下の各節の「推奨」は、2026-10-23 に Human が「推奨通りで」と回答して、そのまま承認された方針である（「判断点」の 1〜6 はすべて A）。各節の「代替」は採らなかった案として残す。

### 1. 全体の流れ

```text
Task
→ Planner（Claude Code の CLI。1 回目）── 失敗・使えない ──→ Planner（Local。2 回目）
   └ Plan: Node ごとに role と agent（Backend が決めた候補の中から）
→ Backend が Plan を検査（agent の候補・Role の上限・Working Set・上限の数）
→ Backend が独立した Review の Node を足す（下の 5）
→ DAG の実行: Worker = Local（Qwen3.8-27B）、Researcher = 割り当て、Reviewer = Claude と Codex
   └ 失敗は 0021 の 3 / 0083 の 4 の今の規則で Retry・Escalation（再計画はしない）
→ Integration → Evaluator → Merge Ready → Human
```

- Planner は「どの Agent に」を **提案** するだけで、候補・起動・権限・Worktree・並列数は今までどおり Backend が決める（下の 4・6）。
- 外への送信はすべて `CloudGate`（`CloudPolicy` → Admission → Placement の記録と Audit → 起動）を通り、Planner の呼び出しもそこに入れる（下の 2・3）。

### 2. 本番の `CloudPolicy`: すべての Project を許し、送るたびに記録する（(a)）

- **推奨:** `paw_backend/agents/cloud_policy.py` に `ConfiguredCloudPolicy`（`CloudPolicy.allows(assignment)` を満たす）を置き、`runtime.toml` の `[cloud]`（下の 7）から組み立てる。`allows` は次をすべて満たすときだけ `True`（どれかが分からなければ `False`。今の `CloudGate` と同じく `True` そのものだけを許可とみなす）。
  1. `[cloud].enabled = true`（全体の Switch。既定は `false`。File は root の持ち物で Admin が変える: Decision 0093 の 2）。
  2. Assignment の Agent の Label が CLI の Runtime（`claude` / `codex`）で、その Agent がその Role の Ladder にある（下の 4 の表の「段」。Planner が始めさせられる候補（`allowed`）より広く、Escalation の行き先を含む。Worker が Cloud で走るのは「判断点」4 の A の Escalation のときだけ）。
  3. **配備の有効な User が 1 人だけ**（下の「判断点」5 の A。Human の回答 3 の前提「1 人で使う配備」を Policy で確かめる）。2 人目が有効になったら、Admin が決め直すまで `False`。**この条件は `allows` の時点だけでなく、送る前の記録の Transaction（下の「記録」）の中で確かめ直す**: Admission と Placement の間に 2 人目が有効になっても送らないため。記録の Transaction は配備で 1 つの Advisory Lock（`pg_advisory_xact_lock_shared`）を共有で取ってから有効な User を数え直し、1 人でなければ何も書かず `NodeStopped`（Gate は送らない）。User を作る・有効にする Transaction は同じ Lock を排他で取る。これで「有効な User の数え直し」と「User の有効化」は直列になり、記録が Commit された送信は、その時点で 1 人だった配備の送信になる。記録の後にすでに始まった Session は、Kill Switch（下の「止め方」）と同じく終わるまで走る。
  4. Task が Project に属し、その Project が削除待ちでない（今の `stop_project_tasks` の対象は送らない）。**Project ごとの許可の一覧は持たない**（Human の回答 3: すべての Project）。
- **送るもの**: Orchestrator が Node に渡す内容（`title`・`goal`・`input`・直接の依存の `NodeResult`。Planner には Task の `title` と `input`）、CLI が PAW の Tool（MCP の Bridge → Tool Broker）で読んだ Repository の内容（Node の Scope の中だけ）と Tool の出力（Broker の Redact の後）、Role の上限にある `project.memory.use` / `shared_memory.read` で読んだ Project の Memory と共有 Memory。
- **送らないもの**（今の仕組みのまま。この Decision で緩めない）: `TaskScope.excluded_paths` と `NodeWorktree.protected`（Sandbox で Mask し、Broker も拒否: 0083 の 6）、`.git` と Object Store（Mask と Git の Shim: 0083 の 2・6）、Credential（Connection の Handle は Task の `credential_handles` に入れない: 0016 の 1。CLI の Container に Worktree を Mount しない。Tool の出力と CLI の出力は Redact: 0083 の 6）、User の Private Memory（`memory.use` はどの Role にも与えない: 0021 の 2）、他の Task・他の User の Workspace。
- **記録**（上の 3 の確かめ直しを含む）: Node の試行は今の 0048 の記録（`agent_dag_node_attempts` の Placement の 7 列と `audit_events` の `orchestrator.cloud_send`、内容の指紋と大きさ）。Planner の呼び出しは下の 3 の新しい記録。どちらも **送る前に同じ Transaction で記録し、記録できなければ送らない**（0048 の 4）。Codex / Claude の 1 回ごとの使用量は今の `connection_usage`（0016 の 5・6、0083 の 5）。
- **止め方（Kill Switch）**: (1) すぐ止める: Admin が Connection を無効にする（0016 の 8 の `enabled`。進行中の Admission の終わりを待ってから返るので、返った後に新しい Session は始まらない。走っている Session は終わるまで走る）。新しい API は作らない。(2) 全体を止める: `[cloud].enabled = false` にして Backend を再起動（0093: File は起動の時に 1 回読む）。どちらでも Gate で断られた Node は 0083 の 4 の `escalate` で次の段へ移るか、段がなければ今の規則で止まる。
- **Quota との関係**: `CloudPolicy` は「この内容を外へ出してよいか」だけを決め、量は決めない。量は今までどおり Admission（0016 の 2・3: User ごと・Connection の種類ごとの Quota。判定は Task がその Connection を初めて使う時だけ）と Task の Budget（`tokens`、0083 の 5 の Session の最低の枠 50,000）が決める。Planner の呼び出しは Task の最初の Claude の呼び出しなので、**Claude の Quota の判定は計画の時に行われ**、同じ Task の後の Claude の Reviewer は Quota を超えていても通る（0016 の 3 のまま）。
- 代替: (a) Project ごとに許可の Flag を持つ（Human の回答 3 と違う。1 人の配備では設定の手間だけが増える）。(b) Policy を持たず `CloudGate` の検査を外す（0037 の 14・0083 の 1 の Fail closed を崩すので採らない）。

### 3. Planner を Claude Code の CLI で動かし、呼び出しを記録する（(b)）

- **推奨（Ladder）:** Planner の Ladder を `["claude", "local"]` にする。0021 の 3 のとおり Planner の試行は 2 回まで（`max_plan_attempts`）で、2 回目は Ladder の次の段なので、**Claude が使えない・Quota を超えた・Gate で断られた・受け入れられない Plan を返した・Timeout のときは、2 回目を Local の Planner が行う**（下の「判断点」3 の A）。Local の Planner の Plan も同じ検査を受け、`agent` を書かなければ Role の既定（下の 4）が入る。Task の Log に固定の 1 行（`plan: planner=local (fallback)`）を残し、Task の詳細で分かるようにする。組み立ては Planner の Ladder の CLI の Label を `claude` だけ許し（`codex` は拒否）、`claude` を置くなら `[cloud].enabled` と `[agents.claude]` を必須にする（0083 の 1 の「`CloudPolicy` なしの CLI の段は拒否」を Planner にも当てる）。
- **推奨（Runtime）:** `ClaudeCliRuntime` が Planner の Role を受ける。Local の Planner と同じく、CLI に見せる Tool は読み取りだけの PAW の Tool（MCP の Bridge 経由で Tool Broker を通る。0083 の 6）と `submit_plan`（0083 の 10。Bridge から Runtime へ直接渡し、Broker を通らない）。CLI の組み込みの Tool は止めたまま（0087 の 4 の `--tools ""` など）。Sandbox は Worktree のない Node と同じ読み取り専用の Mount（`.git`・除外 Path・保護された File を Mask）。Planner の Prompt（Local と Claude で同じ文）に、Role ごとの `agent` の候補と既定（下の 4 の表）、「Review の Node は Backend が足す」（下の 5）、0098 の 8 の `key` / `title` の規則を書く。
- **推奨（記録）:** Planner の呼び出しには DAG も試行の行もない（DAG を作るのは受け入れの後）ので、新しい Table `agent_plan_calls`（Migration。番号は実装の PR で Issue の番号から）を足す: `task_id`・Task の Run（`task_attempt` と `retry_count`。Retry は `attempt` を変えず `retry_count` だけを増やすので、両方で Run を区別する: `TaskRun`）・**Run の中の通し番号 `seq`**（下の (i)）・その Claim の中の呼び出しの番号 `claim_call`（1〜`max_plan_attempts`。Ladder の段を選ぶ今の数え方）・呼び出した Worker の `owner`・`placement`（`local_gpu` / `local_cpu` / `cloud`）・`placement_agent`・`placement_model`・`placed_at`（DB の時計）・Cloud のときだけ `content_fingerprint`（`sha256:` + 64 桁）・`content_bytes`・`placement_audit_id`。CHECK と「1 回だけ」の Trigger は 0048 の 1 の 7 列と同じ形。`UNIQUE (task_id, task_attempt, retry_count, seq)`。
  - (i) **`seq` は DB が Run ごとに続けて振る**: `_start_planner_call` の Transaction の中で、Queue の Entry を Lock して Lease を確かめた（`TaskQueue.heartbeat_in`。`DagStore.acquire_in` と同じ）うえで、その Run の行の最大の `seq` + 1 を使う。Retry の Run（`retry_count` だけが違う）も、**同じ Run の中で Lease を引き継いだ別の Worker**（今の `_plan` は Claim ごとに呼び出しの番号を 1 から数える: `orchestrator.py` の `_plan`）も、前の行とぶつからない。Ladder の段と `max_plan_attempts` は今までどおり Claim ごとの `claim_call` で決める（0021 の 3 の数え方を変えない。Q3 の A の「2 回目は Local」もそのまま）。引き継いだ Worker は、前の Worker の Placement が NULL の行を「始めたが送らなかった呼び出し」としてそのまま残し、書き換えない。DELETE を誰にも与えない（0021 の 8 の Table と同じ）。
  - 行は今の `_start_planner_call` の Transaction（Task が `running` でこの Run であることを確かめて `steps` / `retries` を計上する）で作り、Runtime が呼ぶ `PlanCallPlacementHandle.record(...)` が Placement の列を埋める。**Fencing は Task の Run と Queue の Lease で、DB の Transaction の中で確かめる**（DAG がないので `epoch` はない）。`RunGuard` は Task と Run が生きているかしか見ず、`AttemptFence` は Process の中だけのものなので、それだけでは足りない（`execute_session` は Admission の後に Gate を呼ぶので、Lease を失った古い Worker が記録して送りうる）。そこで `PlanCallPlacementHandle.record` は 1 つの Transaction で、(1) Queue の Entry を Lock して、この Worker がまだ Lease を持つこと（`TaskQueue.heartbeat_in`）、(2) 行の `owner` がこの Worker で、その Run の最大の `seq` がこの行であること（後から引き継いだ Worker の呼び出しがあれば古い）、(3) Task がこの Run で `running` であること、を確かめてから Placement の列と Audit の行を書く。どれかが合わなければ何も書かず `NodeStopped` で、`CloudGate` は送らない（Admission で開いた使用量の行は 0083 の 1 のとおり `cancelled` で閉じる）。`RunGuard` と `AttemptFence` はその前の早い停止にだけ使う。行は Budget の開始の記録と同じ Transaction で作るので消さない。**Placement の列を埋める前に Run が替わった・Lease を失った呼び出しは、Placement の列がすべて NULL の行として残り、それが「始めたが送らなかった呼び出し」の記録である**（`record` は何も書かず `NodeStopped`。CHECK は 0048 の 1 と同じく 4 列が揃って NULL を許す）。
  - Cloud のときは同じ Transaction で `audit_events` に 1 行（0048 の 3 と同じ `action` `orchestrator.cloud_send`、`reason` は `cloud_planning`、`resource_kind` `task`、`agent_id` は `agent_id_of(task, run, "@planner", call)`、`details` は NULL）。内容の指紋は **Orchestrator が** Task の `title` と `input` から計算する（Runtime は偽れない: 0048 の 3）。CLI が Tool で読んだ Repository の内容は、Node と同じく Tool Broker の Audit に残る。
  - Local の Planner の呼び出しも同じ行に記録する（0048 の 4 の「Local の試行も記録する」と同じ理由）。
  - Planner の呼び出しの `NodeConnections` は、今の `_node_connections` が `None` を返す代わりに、上の Fencing の Gateway を渡す（`execute_session` だけを見せる。0083 の 1）。
- **推奨（Budget と使用量）:** 0083 の 5 の CLI の規則をそのまま使う: Token は `ConnectionService.execute_session` だけが計上し（用途の Category は 0083 の 1 のとおり Planner は `coding`）、開始の前に Task の `tokens` の残りが最低の枠（50,000）以上かを確かめ、Turn ごとにその場で計上し、尽きたら止める。`steps` / `retries` は今の Planner の計上（0021 の 7）のまま。**Task の Budget に `tokens` がない Task は Claude の Planner を始められない**（`TaskBudgetError` → `TOKEN_FLOOR`）ので、2 回目の Local の Planner になる。
- **推奨（Timeout）:** Planner の呼び出しの上限は今の Node の Timeout（30 分、0021 の 3）で、CLI の Session はそれから 60 秒を引いた値（0083 の 8）。新しい値は作らない。Planner の実行中に Task の状態と Budget を読む規則（0021 の 7）も今のまま。
- 代替: (a) Claude が使えなければ Local に戻さず、Task を待たせる（`waiting`）か失敗にする（下の「判断点」3 の B / C）。(b) Planner の記録を `audit_events` の行だけにする（Placement と内容の指紋の置き場がなく、0048 と形が分かれる）。(c) Planner の呼び出しを DAG の行の前に「仮の DAG」として作る（DAG の一意の制約と Fencing の意味が変わるので採らない）。

### 4. Plan の `agent` と、Ladder・Escalation との関係（(c)）

- **推奨（形）:** Plan の Node に任意の Field `agent`（Label の文字列。`[a-z][a-z0-9._-]{0,63}`、Ladder の Label と同じ形）を足す。

  ```json
  {"key": "impl", "role": "worker", "title": "...", "goal": "...",
   "depends_on": ["research"], "agent": "local"}
  ```

- **推奨（候補の表。`[orchestrator.assignment]` の値で、Admin の File が決める。Planner は変えられない）:**

  | Role | 候補（`allowed`） | 既定（`agent` を書かないとき） | 段（Ladder。Escalation の行き先） |
  | --- | --- | --- | --- |
  | `worker` | `local` だけ | `local` | `["local", "codex"]`（「判断点」4 の A） |
  | `researcher` | `local`・`claude` | `local` | `["local", "claude"]`（「判断点」2 の A） |
  | `reviewer` | `claude`・`codex` | （Backend が足す Node は固定。下の 5） | `["claude", "codex"]`（ただし下の 5 の規則で他の Review と同じ Agent へは移らない） |
  | `planner`（Plan の中の読み取りだけの分析の Node） | `local` だけ | `local` | `["local"]` |

- **推奨（検査）:** `Plan` を作る時（`InvalidPlanError` の閉じた理由に `agent_not_allowed` を足す）に、`agent` が `allowed[role]` にない（未知の Label、Worker に `claude` など）Plan は **全体を拒否する**（黙って既定に直さない。0021 の 1 の「未知の Field は拒否」、0021 の 2 の「黙って削らない」と同じ考え方）。拒否は Planner の失敗の 1 回として数え、2 回目の Planner（Local）へ移る。Plan の大きさ（128 KiB）には `agent` も数える。受け入れた Plan は、`agent` を書かなかった Node にも既定を入れて **割り当てを明示して保存する**（何が割り当てられたかが記録から分かるように）。
- **推奨（Ladder との関係）:** 割り当ては **始まりの段** を決める。Node の実際の段は「その Role の Ladder の、割り当てた Agent の位置から上」で、Escalation は今の規則（0021 の 3 の Loop 検知、0083 の 4 の `escalate=True`）でその上の段へ移る。
  1. 割り当ては Ladder の中になければならない（組み立てで `allowed[role]` ⊆ `ladders[role]` を確かめ、外れていれば起動しない）。
  2. Worker は候補が `local` だけなので、**Planner は Worker を Cloud で始めさせられない**。Worker が Cloud（`codex`）で走るのは、Local の段で今の Escalation の規則が働いたときだけ（「判断点」4）。
  3. Reviewer は下の 5 の規則で、他の必須の Review と同じ Agent の段へは移らない（その段を飛ばし、上がなければ Escalation しない）。
  4. 段の数え方（`agent_index`）は「割り当ての位置からの相対」ではなく、今までどおり **Role の Ladder の絶対の位置** にする（Migration 0034 の CHECK `agent_index BETWEEN 0 AND 3` を変えない）。DAG を作る時に、Node の最初の試行の `agent_index` を割り当ての位置にする。
  5. `HybridRuntime` の Cloud への振り替え（0037 の 8。GPU が混んでいたら Cloud へ）は **使わない**（`cloud` を渡さない）。Planner の割り当てと Ladder の外で Worker が Cloud に出る経路になるため。GPU が混んでいる間、Local の段の Node は今までどおり待つ（`ComputeUnavailable` で再試行）。
- **推奨（保存と後方互換）:** Migration で `agent_dag_nodes` に NULL 可の列 `assigned_agent`（CHECK は Label の形）を足す。**既存の行は NULL** で、NULL は「割り当てなし = Ladder の 1 段目から」（今の動き）。Retry で開き直す古い DAG（0021 の 6）も同じく今の動きのまま。新しい DAG の Node は必ず非 NULL。設定の変更で `assigned_agent` が Ladder から消えたときは、その Node を固定の Class（`AssignmentUnavailable`。Orchestrator が自分の名前で記録する。Runtime の閉じた一覧には足さない）で失敗にし、別の Agent で黙って走らせない。
- 代替: (a) 割り当てを「その Node の唯一の Agent」にし、Escalation を止める（Human の回答 1 の「Retry / Escalation は今の規則のまま」と合わない）。(b) Planner に Ladder そのもの（段の並び）を書かせる（Planner が Escalation の行き先、つまり外へ送る先を決められるので採らない）。(c) 割り当ての候補を Plan の Prompt にだけ書いて Backend は検査しない（Prompt Injection で Worker を Cloud に出せるので採らない）。

### 5. 独立した Review は Backend が保証する（(d)）

- **推奨（Review の Node を Backend が足す）:** 書き込む Task（`read_only` でない Task）で、Plan に Worker の Node が 1 つ以上あれば、Backend は受け入れた Plan に **2 つの Reviewer の Node を足す**: `paw-review-claude`（`agent` `claude`）と `paw-review-codex`（`agent` `codex`）。どちらも `required: true`、すべての Worker の Node に依存し、互いには依存しない（並列。「判断点」1 の A）。`title` と `goal` は Backend の固定の文（Task の `title` を引用し、Worker の Node の key を並べる）。`paw-` で始まる `key` は Planner には使わせない（`InvalidPlanError` の `reserved_key`）。Node の数の上限（32）は足した後の数で確かめ、超えるなら Plan を拒否する（Planner に 30 以下を求める Prompt にする）。
  - Planner は自分の Reviewer の Node（途中の確認など）を足してよい（候補は `claude` / `codex`）。それらは追加で、Backend の 2 つの代わりにはならない。
  - 読み取りだけの Task（Decision 0094）には足さない（Worker がなく、変更がない）。
  - これは「Node が Node を足す」（0021 の 1 が禁じた動的な分解）ではなく、Plan を受け入れる 1 回の時に Backend が決まった形で足すだけである。
- **推奨（独立の規則）:** Backend が足した 2 つの Review の Node は、Escalation で **互いの Agent、および上流の Worker を実際に走らせた Agent**（Placement の `placement_agent`）の段へ移らない。今の表では Claude の Review は `codex` へ移れず、Codex の Review は上の段がないので、どちらも Escalation しない（段の試行の 6 回と Loop の規則で失敗すれば Node が失敗し、必須なので DAG は `failed`。0021 の 6 の Retry で開き直せる）。Worker が `codex` に Escalation して走ったときは、Codex の Review は独立でなくなるので、その Node は固定の Class（`ReviewNotIndependent`）で失敗にし、Task は `failed` で Human が判断する（Claude の Review は走る）。
- **推奨（Reviewer が差分を読めるようにする）:** 上の「背景」の隙間を埋める。Worker の Node に依存する Reviewer の Node には、**上流の Worker の Branch を合わせた読み取り専用の Worktree** を用意する（`GitWorktreeCoordinator.prepare_node` の上流の Merge と同じ手順で作り、Sandbox は読み取り専用で Mount、Commit はしない）。ただし Worker の `refs/paw/base/<Branch>` は上流の Merge の **後** の Commit なので、そのまま使うと起点と `HEAD` が同じになり差分が空になる。そこで Review の Worktree には、上流の Merge の **前** の Commit を指す別の Ref `refs/paw/review-base/<Branch>` を作り、一度作ったら動かさない。その Commit は **Review の Branch 自身の、最初の上流の Merge の直前の `HEAD`** である（Coordinator の `_base_commit` は既定の Branch の今の先端を返し、Review の Branch の祖先にないことがあるので使わない）。Coordinator は Branch を作った直後、上流を Merge する **前** にこの Ref を書く。Ref を書く前に Process が落ちて、Ref のない Branch に上流の Merge がすでにあるときは、`HEAD` から First-parent で辿った最初の上流の Merge の First parent を使い、決められなければ推測せず `WorktreeUnavailable` にする（0083 の 10 の起点の作り直しと同じ考え方）。Git の Shim は Review の Worktree に限って、この Review の起点から `HEAD` までの `diff` / `log -p` / `show`（上流の Worker の Commit と Merge を含む）を許す（0083 の 2 の Worktree のある Node の規則を、起点だけ差し替えて当てる）。こうして Claude と Codex は Worker の差分を Tool で読む。除外 Path と保護された File は今までどおり Mask と Pathspec で外す。
- 代替: (a) Planner が Review の Node を書くことを Backend が検査だけする（無ければ Plan を拒否）。足りなければ Local の Planner へ移り、Local の Planner も書き忘れれば Task が失敗する。Prompt Injection で Review を抜く Plan を作らせる余地も残る。(b) 0021 のまま Ladder の並べ方に任せる（Human の回答 2 の「Claude と Codex」を保証できない）。

### 6. Security: Prompt Injection・権限・Credential（(e)）

- **Repository の内容から Planner への Prompt Injection**: Planner は Repository を読む（README・Code・Issue の写しなど）。悪意のある文が Planner に「Worker を Cloud に」「権限を足せ」「Review を省け」と書かせても、Backend の検査で次のどれもできない。
  - `agent` は Role ごとの候補（Admin の File）の中だけ。Worker は `local` だけ。候補にない値は Plan ごと拒否（4）。
  - Node の Grant は「Role の上限 ∩ Plan が求めたもの ∩ 親の Grant」のまま（0021 の 2）。`agent` は Capability ではなく、`claude` / `codex` を選んでも Grant も Scope も広がらない。CLI の組み込みの Tool は止めたまま（0087 の 4）。
  - `repositories` は Working Set の部分集合のまま（`ScopeEscalation`）。
  - 必須の 2 つの Review は Backend が足し、Planner は消せない（5）。`paw-` の `key` は使えない。
  - Plan の作成は 1 回だけで、実行中の再計画はない（0021 の 1、Human の回答 1）。
  - 残るのは「Plan が下手」（Budget と Node の上限が抑える）と「Researcher を `claude` にして Claude に送る量が増える」（Human の回答 3 で許した範囲）。どちらも記録に残る。
- **Planner は権限を広げられない**: Planner 自身は読み取りだけの Role の上限（0021 の 2）で、`submit_plan` は Broker を通らないが Worktree にも外部にも何もしない（0083 の 1）。`agent.use` / `project.agent.use` は委任不可のまま（0021 の 9）。Planner が「どの Agent を起動するか」を書いても、起動するのは Backend の Orchestrator だけ。
- **Credential**: 新しい Credential は作らない。Claude の Planner と Reviewer は同じ Claude の Connection（`setup-token`、0087 の 4。Refresh がないので同時の Session に制限なし）、Codex の Reviewer は Codex の Connection（ChatGPT の Login なら Connection ごとに 1 Session の DB Lease: 0083 の 6・0096）。Credential は Log・DB・Error・`NodeResult`・Plan に出さない（Plan の `key` / `title` は 0098 の 8 で Credential の形を拒否）。**`goal` / `input` は Redact の検査をしない**（0098 の 8 は `key` / `title` だけ）ので、Repository の非除外の File に平文の Secret があれば、Tool の出力の Redact（形の分かる Credential だけ）をすり抜けて Provider に送られうる（下の「リスク」）。
- 代替: Planner の出力を別の Model で検査する（費用が増え、決定的でない。Backend の検査で十分と判断）。

### 7. `runtime.toml` の形（Decision 0093 の 3 を満たす）

- **推奨:** 次の Section を足す（Key の正確な名前は実装の PR で、今の `ClaudeCliRuntime` / `CodexCliRuntime` の引数に合わせる。Secret は書かない: 0093 の 1）。

  ```toml
  [cloud]
  enabled = true                 # 全体の Switch（既定 false）
  single_user_only = true        # 「判断点」5 の A

  [agents.claude]                # ClaudeCliRuntime（model・session_base・relay_command）
  [agents.codex]                 # CodexCliRuntime

  [orchestrator.ladders]
  planner    = ["claude", "local"]
  worker     = ["local", "codex"]
  researcher = ["local", "claude"]
  reviewer   = ["claude", "codex"]

  [orchestrator.assignment]
  worker     = { allowed = ["local"], default = "local" }
  researcher = { allowed = ["local", "claude"], default = "local" }
  reviewer   = { allowed = ["claude", "codex"] }
  planner    = { allowed = ["local"], default = "local" }
  ```

- 起動の検査（0093 の 2 の Fail closed に足す）: CLI の Label が Ladder にあるのに `[cloud].enabled` か `[agents.<name>]` がない、Planner の Ladder に `codex`、`allowed` が Ladder の外、`worker` の `allowed` に CLI の Label、`reviewer` の `allowed` が `claude` と `codex` の両方を含まない、のどれかなら起動しない。起動の Log の 1 行（0093 の 2）に `cloud: enabled single_user` と `assignment: yes` を足す。
- 本番の File を変えるのは Human（0093 の 5）。CLI の段は Smoke（下の「PR の分け方」の 6）の後に有効にする。

## 判断点（承認の時の説明。決定は上の「決定」と Approval の行）

1. **「Review は Claude と Codex」の形**（5）。
   - **A（推奨）**: Backend が 2 つの独立した Reviewer の Node（Claude・Codex）を **並列に** 足し、どちらも必須。互いの結果を見ない（独立性が最も高い。待ち時間は長い方の 1 回分）。
   - B: Claude の後に Codex（Codex の Node が Claude の Node に依存し、Claude の Review を読む）。悪い点: Codex が Claude の見方に引きずられ、独立でなくなる。時間が 2 回分かかる。
   - C: Reviewer の Node は 1 つで、Ladder の段として Claude → Codex（Escalation のときだけ Codex）。悪い点: 普段は Claude だけが見るので「Claude と Codex」にならない。
2. **Researcher の割り当て**（4）。
   - **A（推奨）**: 候補 `local`・`claude`、既定 `local`。Planner が広い調査（Repository 全体の構造・複数の Module にまたがる調べ物）に `claude` を割り当てられる。Ladder `["local", "claude"]` で、Local が Context の上限などで失敗したら Claude へ Escalation する。
   - B: `local` だけ。悪い点: Planner が割り当てを選べるのが実質 Reviewer の追加の Node だけになり、「Node ごとに割り当てる」意味が薄い。
   - C: `claude` だけ。悪い点: Claude の Quota と送る量が増え、Local-first から離れる。
3. **Claude の Planner が使えないとき（Quota の超過・Connection の無効・Gate の拒否・Timeout・受け入れられない Plan）**（3）。
   - **A（推奨）**: 2 回目の Planner を Local が行う（Planner の Ladder `["claude", "local"]`。0021 の 3 の今の規則で足りる）。Local の Plan も同じ検査を受け、`agent` は Role の既定。Task の Log に「Local の Planner で計画した」と残す。
   - B: Claude が戻るまで Task を待たせる（`waiting`）。悪い点: Claude の Subscription の Window（数時間）の間 Task が止まる。`resets_at` まで待つ仕組みはまだない（0083 の 4 が後続の Decision とした）。
   - C: Task を `failed` にする。悪い点: Human が Retry するまで何もしない。
4. **Worker の Escalation の行き先**（4 の (2)）。
   - **A（推奨）**: Worker の Ladder を `["local", "codex"]` にする。Planner は Worker を `local` にしか割り当てられないが、今の Escalation の規則（Loop 検知・`escalate=True` の Context / Step の上限など）が働いたときだけ Codex が続きを実装する（AGENTS.md の「品質を犠牲にしてまで Local に固執しない」）。このとき 5 の規則で Codex の Review は独立でなくなり Task は `failed` になるので、Human が結果を見て判断する。
   - B: Worker の Ladder を `["local"]` だけにする（Human の回答 2 を文字どおりに読む）。悪い点: Local で解けない Node は段の試行の上限で失敗し、Human が Restart するまで進まない。
   - C: `["local", "claude"]`。悪い点: Claude が Worker と Review の両方を担い、Claude の Review が独立でなくなる。
5. **「すべての Project を許す」の範囲**（2 の 3）。
   - **A（推奨）**: 配備の有効な User が 1 人のときだけ許す（`single_user_only = true`）。2 人目を有効にしたら、Admin が File を変えるまで Cloud へは送らない（Planner は Local に、Reviewer は Gate で断られる）。Human の回答 3 の前提（1 人で使う配備）を Policy が確かめる。
   - B: User の数を見ず、すべての User のすべての Project を許す。悪い点: 後で User を足したとき、その User の Project も黙って送られる。
6. **Reviewer に上流の Worker の差分を読ませる**（5 の 3 つ目。今回見つけた既存の隙間）。
   - **A（推奨）**: Worker に依存する Reviewer の Node に、上流の Worker の Branch を合わせた読み取り専用の Worktree を用意し、Git の Shim で起点から `HEAD` までの差分を読ませる。Worker の差分が Claude と Codex に送られる（Human の回答 3 の範囲）。
   - B: 今のまま（Reviewer は User の Checkout と上流の `NodeResult` の要約だけを読む）。悪い点: Claude と Codex は変更そのものを見ずに Review することになり、Review の意味がほとんどない。

その他（2 の Kill Switch の 2 つの形、3 の記録の Table、4 の `agent_not_allowed` での Plan 全体の拒否と後方互換、6 の Security、7 の File の形）は、上の推奨のとおりでよいかを一括で確かめる。

## Test の方針

- CI では本物の CLI・GPU・Network を使わない（0083 の 9 と同じ: Fake の CLI・Fake の OpenAI 互換 Server・Fake の Sandbox）。
- 固定するもの:
  - `ConfiguredCloudPolicy`: `enabled = false`・Worker の Role・候補にない Agent・User が 2 人・削除待ちの Project で `False`、分からないときも `False`、すべて満たして `True`。Connection を Admin が無効にした後の Session が Admission で断られること（既存の Test を Planner でも）。Admission の後・記録の前に 2 人目の User が有効になったとき、記録の Transaction が数え直して何も書かず送らないこと（Node の試行と Planner の両方）、User の有効化が記録の Transaction と直列になること。
  - Planner: Claude の Fake の CLI が `submit_plan` で返した Plan が受け入れられること。Gate の拒否・Quota の超過・Timeout・不正な Plan・`tokens` のない Budget の各々で、2 回目が Local の Planner になり、Log の固定の 1 行が出ること。**記録の前に何も送らないこと**、`agent_plan_calls` の行と `audit_events` の行が同じ Transaction で Commit されること、記録が失敗したら送らないこと、Run が替わった・Lease を失った呼び出しの行の Placement の列が NULL のまま残ること、Retry の Run（`retry_count` だけが違う）と、同じ Run の中で Lease を引き継いだ Worker の呼び出しが、前の行とぶつからず `seq` が続くこと、Lease を失った古い Worker の `record` が Queue の Lease の確認で拒まれ何も送らないこと（Admission の後の Gate の時点を含む）、指紋が Runtime ではなく Orchestrator の計算であること。
  - Plan の `agent`: 候補の外（未知の Label、Worker に `claude`、Planner の Role に `codex`）・形の誤り・`paw-` の `key` は `InvalidPlanError`。書かなければ既定が入り、保存された行が非 NULL。Node の最初の試行の `agent_index` が割り当ての位置。Escalation が上の段へ移ること。NULL の古い行が 1 段目から今の動きで走ること（Migration の前に作った DAG の Retry を含む）。割り当てが Ladder から消えたら `AssignmentUnavailable`。
  - 独立した Review: 書き込む Task に 2 つの Node が足され、読み取りだけの Task には足されないこと。足した後に 32 を超える Plan の拒否。Claude の Review が `codex` へ Escalation しないこと。Worker が `codex` で走ったら Codex の Review が `ReviewNotIndependent` で失敗すること。Reviewer の読み取り専用の Worktree で `refs/paw/review-base/<Branch>` から `HEAD` の `diff` に上流の Worker の Commit が出て（空でない。既定の Branch の先端が Review の Branch より進んでいても）、除外 Path が出ず、書き込みの Tool がないこと。
  - **Prompt Injection の回帰**: Fake の Planner が「Worker を `codex` に」「`capabilities` に `project.pr.create`」「Working Set の外の Repository」「Review の Node を消す / `paw-review-*` を自分で書く」を返したとき、それぞれ拒否・`GrantEscalation`・`ScopeEscalation`・Backend の Node が残ることを確かめる。
  - `runtime.toml`: 7 の拒否の各項目（0093 の 6 と同じく 1 つ以上ずつ）と、Error の文に値が出ないこと。
  - Migration: `agent_plan_calls` と `agent_dag_nodes.assigned_agent` の Upgrade / Downgrade、「1 回だけ」の Trigger、CHECK、既存の行が NULL のまま制約を満たすこと。
- **本物の CLI の Smoke は Human の許可の後**（0083 の 9）: Claude の Planner で小さな書き込む Task を 1 つ、Claude と Codex の Review の Node が走り、`agent_plan_calls`・試行の Placement・`audit_events`・`connection_usage` が残ることを見る。

## PR の分け方

どれも「Refs #297」（最後の PR で Closes）。この Decision の承認の後に始める。

1. この Decision（docs だけ）。
2. `ConfiguredCloudPolicy` と `runtime.toml` の `[cloud]`・`[agents.claude]`・`[agents.codex]`・`[orchestrator.assignment]` の Parser と起動の検査（2・7）。`[agents.*]` を書いても、組み立ては CLI の段を 6 まで有効にしない（今の「組み立てでは無効」を保つ Flag）。
3. Planner の呼び出しの記録: Migration（`agent_plan_calls`）・`PlanCallPlacementHandle`・Planner の `NodeConnections`・Audit の行（3）。Local の Planner の記録もここから始まる。
4. Plan の `agent`: `plan.py`・`OrchestratorConfig` の候補・Migration（`agent_dag_nodes.assigned_agent`）・`_spec_of` の始まりの段・`AssignmentUnavailable`・Local と Claude の Planner の Prompt（4）。
5. 独立した Review: Backend が足す 2 つの Node・独立の規則・`ReviewNotIndependent`・Reviewer の読み取り専用の Worktree と Git の Shim の範囲（5）。`ClaudeCliRuntime` の Planner の受け入れ（3 の Runtime）もここか 4 に入れる。
6. 有効にする: CLI の Smoke（Human の許可の後。Provider の規約の確認を含む）、`runtime.example.toml` と Deploy の文書、組み立ての有効化。本番の `/etc/paw/runtime.toml` の変更は Human（0093 の 5）。

2〜5 は順に依存する（2 → 3 → 4 → 5）。Migration は今の鎖の頭（`0280`）の後に、Merge の順で鎖を組み直す。

## 未決（この Decision では決めない）

- Quota の `resets_at` まで Task を待たせる仕組み（0083 の 4 が後続とした）。3 の A では Local の Planner で進むので、この Decision では要らない。
- Reviewer の結果（`NodeResult` の要約・`unresolved_questions`）を Merge Ready の判定にどう使うか（Evaluator と Review の層の関係。0021 の 6 は DAG の成功で `evaluating` にするだけ）。今は Review の Node が成功すれば DAG は成功し、Review の指摘の中身は判定に使わない。Review の結果で `failed` にする規則は後続の Decision。
- Codex の ChatGPT の Login の配備（Decision 0095 / 0096 の続き。Human の Login と CLI の Smoke）が終わるまで、Codex の Review は Gate で断られる。その間、書き込む Task は 5 の必須の Codex の Review で `failed` になる。Codex が使えるまで Worker のある Task を始めないか、Codex の Review を必須から外す一時の設定を持つかは、6 の PR で Human に確かめる。
- Planner の Prompt（Claude と Local の文）の中身。Prompt は実装の PR で示し、Human が読む。

## Human に確かめてほしいこと（Decision の外の前提）

- **Provider の規約**: 0083 の「Human に確かめてほしいこと」の 1 つ目（Subscription の CLI を Headless で、組み込みの Tool を止めて MCP の Tool だけで動かすこと）。1 人の配備なので「複数の User のために」の懸念は小さいが、確認は 6 の PR の Smoke の前に要る。
- **Claude と Codex の Subscription の使用量**: 1 つの書き込む Task で、Claude は Planner と Review（と Researcher）、Codex は Review を使う。Subscription の Window の消費が増える。
- **Task の Budget の `tokens`**: Task の作成の既定の Budget に `tokens` がない、または 50,000 未満だと、Claude の Planner も Review も始められない（3）。作成の既定を確かめる。

## リスク

- Repository の除外していない File に平文の Secret があると、Tool の出力の Redact（形の分かる Credential だけ）をすり抜けて Anthropic / OpenAI に送られうる。除外 Path（`excluded_paths`）と保護された File を Project ごとに見直すことを勧める。送ったことは指紋と Audit で後から分かるが、送った内容は残さない（0048 の 3）。
- Claude の Planner が Local の Planner より大きな Plan を作り、Node の数・Budget の消費が増えうる。Node の上限（32）と Budget が抑える。
- 5 の規則で、Worker が `codex` に Escalation した Task と、Codex が使えない間の書き込む Task は `failed` になる。失敗が増えたら、規則を新しい Decision で見直す。
- Reviewer の読み取り専用の Worktree（5）は Node ごとに Worktree を 1 つ増やす（Disk と準備の時間）。
- `[cloud].enabled` の変更は再起動が要る。すぐ止めたいときは Connection の無効化（2）を使う。
- 1 人の配備の確認（5 の A）は「有効な User の数」で行うので、Owner が一時的に 2 人目を作るだけで Cloud が止まる。

## 承認後の扱い

2026-10-23 に承認され、Status を Approved に改め、承認の内容を Approval の行と「承認時の決定」に記録した。Supersede する Decision（0021・0083・0048・0093）の本文は書き換えず、この Decision が一部を Supersede することを README の表に記す。
実装は「PR の分け方」の 2〜6 の PR で行う（どれも「Refs #297」）。CLI の段を有効にするのは 6 の Smoke（Human の許可の後）の後で、本番の `/etc/paw/runtime.toml` の変更は Human が行う。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-10-23）

Human は、作業 Session で判断点ごとの説明（推奨つき）を受け、「推奨通りで」と回答して承認した（すべての判断点を推奨どおり。設計の変更はない）。

1. **Review の形**: A。Backend が書き込む Task に `paw-review-claude` と `paw-review-codex` の 2 つの Reviewer の Node を並列に足し、どちらも必須（5）。
2. **Researcher**: A。候補は `local`・`claude`、既定は `local`、Ladder は `["local", "claude"]`（4）。
3. **Claude の Planner が使えないとき**: A。Planner の Ladder は `["claude", "local"]` で、2 回目を Local の Planner が行い、Task の Log に固定の 1 行を残す（3）。
4. **Worker の Escalation**: A。Worker の Ladder は `["local", "codex"]`。Planner は Worker を `local` でしか始めさせられず、Codex が走るのは今の Escalation の規則が働いたときだけ。そのときは Codex の Review が `ReviewNotIndependent` で失敗し、Task は `failed` で Human が判断する（4・5）。
5. **すべての Project を許す範囲**: A。配備の有効な User がちょうど 1 人の間だけ許す（`single_user_only = true`。2）。
6. **Reviewer に Worker の差分を読ませる**: A。上流の Worker の Branch を合わせた読み取り専用の Worktree と、上流の Merge の前の Commit を指す `refs/paw/review-base/<Branch>` を起点にした Git の Shim の範囲（5）。

承認の前に反映した Codex review（5ef5116）の指摘: (P1) Planner の呼び出しの行の識別と Fencing に Task の `retry_count` を含める（Retry は `attempt` を変えない）。(P1) Worker の `refs/paw/base/<Branch>` は上流の Merge の後の Commit なので、Review の Worktree は Merge の前の Commit を指す別の Ref を起点にする。(P2) Placement の前に Fence された Planner の呼び出しは、Placement の列が NULL の行として残る（消さない）。

承認の後に反映した Codex review（dd0c472）の指摘（承認した 6 つの答えは変えない、実装の詳しさの補い）: (P1) 同じ Run の中で Lease を引き継いだ Worker の呼び出しもぶつからないよう、行の識別を DB が Run ごとに振る通し番号 `seq` にし、Ladder の段は Claim ごとの `claim_call` で今までどおり決める。(P1) `PlanCallPlacementHandle.record` は Queue の Entry の Lock と Lease の確認（`heartbeat_in`）・行の `owner`・最新の `seq` を同じ Transaction で確かめる。(P2) `refs/paw/review-base/<Branch>` は既定の Branch の先端ではなく、Review の Branch 自身の最初の上流の Merge の直前の `HEAD`。

承認の後に反映した Codex review（d2f8ce5）の指摘（承認した 6 つの答えは変えない）: (P1) 有効な User が 1 人だけという条件を、送る前の記録の Transaction の中で、User の有効化と同じ Advisory Lock の下で確かめ直す（Node の試行の記録と Planner の呼び出しの記録の両方）。
