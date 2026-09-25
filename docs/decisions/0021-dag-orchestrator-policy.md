# DAG Agent Orchestrator の方針（Plan の形、Role、Escalation、並列数、結果の受け渡し、Sub-Agent の権限と予算）

- Status: Proposed
- Date: 2026-09-25
- Scope: PAW-034（[#30](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/30)）の DAG Agent Orchestrator（`paw_backend/orchestrator/`、Migration `0034`）と、Sub-Agent を起動する以降の Issue（PAW-035 Worktree / Integration、PAW-036 GPU Scheduler、各 Agent Runtime）
- Supersedes: なし
- Approval: 未承認

## 背景

[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Agent Orchestration / Parallel-first execution」は、Task を依存関係付きの DAG で管理し、独立した Subtask を同時に実行し、Role（Planner / Worker / Researcher / Reviewer）を持ち、失敗した Node だけを Retry / Escalate し、Sub-Agent の間では構造化された結果を渡し、親 Task の Budget / Permission / ACL を Sub-Agent が超えないことを定める。
一方で、**次のことは要件も Backlog（PAW-034 の受け入れ条件は 6 項目）も定めていない。** Planner が返す Plan の形、Role ごとに何を許すか、Node の上限、Escalation の段の作り方、並列数の上限、結果の大きさ、Node が失敗し続けたときと Budget が尽きたときの Task の行き先、DAG の寿命、Sub-Agent の予算の分け方、Project 削除の Sweep の周期。
[AGENTS.md](../../AGENTS.md) の「仕様変更」は、重要判断を `docs/decisions/` に提案して人間 / Admin の承認を得ると定める。実装はこれらを最も単純な選択で置いた。この Decision はそれを一覧にし、Human が承認または変更できるようにする。

すでに**承認された Decision が Orchestrator に課した条件**は、選択ではなく守るべき条件として、この Decision の対象外である（実装した場所は README の「DAG Agent Orchestrator」に書いた）。

- Decision 0006（Tool Broker）: 終了した Task へ Tool 呼び出しを渡さない（承認を要する呼び出しだけが Broker で状態を確認する）、`TaskContext.run` を Orchestrator が `TaskSnapshot.run` / `TaskEvent.run` から組み立てる、書き込む Repository を `TaskScope.repositories` として解決し Remote を登録する。
- Decision 0007（Queue・Budget・Loop）: 失敗の文字列は Worker が整形してから `record_failure` へ渡す、Lease を持つ Worker だけが `BudgetTracker.start_runtime` を呼ぶ。Budget を Loop より優先し、`retries` の超過は `FAIL`、それ以外は `WAIT_FOR_USER`。
- Decision 0008（Project）: 削除待ちの Project にも通常の周期で `stop_project_tasks` を呼ぶ。
- Decision 0014（Working Set）: Working Set の永続化（#85）が終わるまで、書き込む Repository は呼び出し側が入力として渡す（Seam を Documentation に書く）。
- Decision 0004（RBAC）: `agent.use` / `project.agent.use` は、子 Agent の Grant を親の部分集合として導く仕組みができるまで委任不可。その仕組みは `authz/delegation.py`（`derive_child_grant`）として、この Issue が実装した。**委任可否そのものは変えていない**（9 節）。

実装は [Backend README](../../apps/backend/README.md) の「DAG Agent Orchestrator」に書いている。

## 提案

### 1. Plan の形と上限

Planner（Role `planner` の Runtime）は次の形の JSON Object を返す。Backend が受け入れるかを決める（`Plan` を作った時点で全て検査され、Cycle、未知の依存、上限超過などは `InvalidPlanError` で、何も保存しない）。**未知の Field は拒否する**（`depend_on` の綴りの誤りが「依存なし」にならないため）。

```json
{"nodes": [
  {"key": "impl", "role": "worker", "title": "...", "goal": "...",
   "depends_on": ["research"], "required": true, "input": {},
   "capabilities": ["project.read"], "repositories": ["<uuid>"]}
]}
```

| 項目 | 値（暫定） |
| --- | --- |
| Node の数 | 1〜32 |
| 1 Node の依存の数 | 8 まで。全体の Edge は 96 まで |
| 依存の鎖の長さ（Node 数） | 16 まで |
| `key` | `[a-z][a-z0-9_-]{0,31}`（Plan の中で一意） |
| `title` | 1〜100 文字（1 行）。`goal` は 1〜4,000 文字 |
| 1 Node の `input` | JSON Object。16 KiB（UTF-8 の Byte）、入れ子 8 段まで |
| Plan 全体 | Plan を Compact な UTF-8 の JSON にした大きさで 128 KiB まで。**Byte で数える**（`key`、`title`、`goal`、`input`、依存、Capability 名、Repository の ID を全て含む。Code Point の数ではないので、4 Byte 文字は 4 Byte）。`agent_dags.plan_bytes` に記録し、DB が範囲を CHECK する（行をまたぐ合計は CHECK で測れないため、Service が宣言し DB が上限を強制する） |
| `required`（既定 `true`） | Task が成功するために必要な Node。全てが `false` の Plan は拒否 |
| `capabilities` / `repositories`（任意） | Role の上限より狭い Capability / Working Set の一部。省略は「Role の上限」「Working Set 全体」 |

Node の順序は決定的で（Kahn 法。同時に進める Node は Plan に書かれた順）、その位置（`ordinal`）を Scheduler と DB が使う。Planner は分解・並列化・依存を**提案**でき、Agent の起動・Permission の付与・Worktree の割当・並列数は Backend が決める（要件）。

**Plan の作成は 1 回だけで、Node が新しい Node を足すことはできない**（動的な分解の拡張はしない）。Planner の最初の呼び出しだけが Plan を返せる。Plan の中の `planner` Role の Node は、読み取り専用の分析 Node にすぎない。

### 2. Role と、Role ごとの Capability の上限

| Role | 上限（`domain.ROLE_CEILING`） | Credential Handle |
| --- | --- | --- |
| `planner` / `researcher` / `reviewer` | `project.read`、`project.memory.use`、`shared_memory.read` | なし |
| `worker` | 上記に加えて `project.task.run`、`project.repo.write` | Task の Handle |

- 読み取り専用の Role が書き込めないことは、**Grant に書き込みの Capability を入れない**ことで Backend が保証する（要件の「Read-only … Writeが発生しないことをBackendが保証できる場合」）。
- どの Role にも `project.pr.create`（PR の作成は統合の PAW-035）、`agent.use` / `project.agent.use`（9 節）、`memory.use`（User の Private Memory）は与えない。
- Node の Grant は「Role の上限 ∩ Plan が求めたもの ∩ 親の Grant」。Plan が親の持たない Capability を求めたら**黙って削らず、その Node を失敗**にする（再試行しない。`GrantEscalation`）。Scope も同じで、Working Set にない Repository を求めた Node は失敗する（`ScopeEscalation`）。
- Sub-Agent の ID は Task・Node・試行から導く（`uuid5`）。Audit の行は Agent を区別できる。

### 3. Escalation の段（Ladder）と Node ごとの Retry

- 設定（`OrchestratorConfig.ladders`）が、Role ごとに Agent の Label を弱い順に並べる（最大 4 段）。Node は 1 段目で始まり、Escalation で次の段へ移る。Review の独立性（実装した Agent と別の Agent が見る）は、Reviewer の Ladder の並べ方で表す（Orchestrator は強制しない）。
- Node の失敗ごとに、Loop 検知（Decision 0007。`step` は Node の `key`）と Budget の判定（`retries` を 1 つ足した予定で）を `decide_next_action` に渡す。
  - `CONTINUE`: 同じ Agent・同じ方法で再試行する。`TRY_ALTERNATIVE`: `approach` を 1 増やして再試行する。`ESCALATE_AGENT`: 次の段へ移る。**`approach` も 1 増やす**（古い方法の履歴が、新しい Agent の最初の失敗を Loop と判定しないように）。
  - `WAIT_FOR_USER`: Node をそのまま残して Run を止め、Task を `waiting`（理由 `user`）にする。`FAIL`（`retries` の超過）: Node を失敗にして Run を止め、Task を `failed` にする。
- Loop でない失敗（毎回違う Message）は Escalation しない。1 つの段で 1 Node が使える試行は **6 回**（暫定）で、超えたら Node は失敗する（Escalation の代わりではない: 下の「決めてほしいこと」5）。Escalation で段が変わると試行の数え直しから始まる。
- 再試行の前に待つ（Back-off）: 1 回目の失敗の後 1 秒、以降は倍で最大 60 秒（暫定。0 で無効）。
- `retryable=False` と答えた失敗は、再試行も Escalation もせず Node を失敗にする。
- 1 つの Node の 1 回の試行は 30 分（暫定）で打ち切る（`NodeTimeout`。失敗として扱う）。
- Planner の呼び出しも同じ仕組みで、2 回まで（暫定）。2 回目は Planner の Ladder の次の段が行う。受け入れられる Plan が得られなければ Task を `failed` にする。

### 4. 並列数

Node は Plan が許す限り同時に実行する（Parallel-first）。同時に走る Node の数の上限は `max_parallel_nodes`（既定 4、最大 16。暫定）で、**静的な上限**である。要件は並列数を GPU の状態などから動的に決めると定める（PAW-036 の Resource Scheduler の担当）。それまでは、同時実行の上限を 1 つの数で置く。起動する Node の順は、準備のできた Node の `ordinal` の小さい順で、同じ DAG は同じ順に起動する。

### 5. 構造化された結果の受け渡し

Node の結果は `NodeResult`（要件の「Subtask output例」）: `summary`（必須、2,000 文字）、`changed_files`、`commit`、`test_result`（JSON Object。4 KiB）、`discovered_facts`、`dependency_notes`、`unresolved_questions`、`confidence`（0〜1）、`artifacts`。未知の Field は拒否する。1 つの結果は JSON で **32 KiB** まで（DB の CHECK は 64 KiB の予備）。Node は**直接の依存**の結果だけを受け取り（最大 8 × 32 KiB = 256 KiB）、会話履歴は渡さない。

**失敗の文字列は、どこにも保存しない。** Node の試行は失敗の Class 名（固定の名前。Adapter の独自の Class は `AdapterError`）と Loop 検知の Signature（Hash）だけを持つ。Task Log に書くのは固定形式の 1 行（Node の key、試行の番号、Class、次の行動）だけである。

### 6. 失敗の伝播と、Task の行き先

- 失敗した Node に依存する Node（推移的）だけが `blocked` になり、起動しない。独立した Node は最後まで実行する（要件: 独立した他 Subtask は継続、全 Task を即 Failed にしない）。
- 実行できる Node がなくなったとき、`required` な Node が全て成功していれば DAG は `succeeded`、そうでなければ `failed`。`required: false` の Node の失敗は Task を失敗にしない（それに依存する `required` な Node は `blocked` なので、その場合は失敗）。
- DAG が `succeeded` なら Task を `evaluating` にする（`begin_evaluation`）。**`complete` は Evaluator の責務**で、Orchestrator は完了にしない（Evaluator と Review は別の層。AGENTS.md）。DAG が `failed` なら Task を `failed` にする。
- Task の Retry（同じ試行のやり直し）は同じ DAG の失敗・Blocked の Node を開き直して続きから実行する。Restart は新しい試行で新しい DAG（古い DAG は履歴）。
- **すでに `succeeded` の DAG は開き直さない。** DAG が成功して Task が `evaluating` になった後、Evaluator が Task を `failed` にして人が Retry したとき、Orchestrator は同じ DAG の結果をそのまま使い、Task を `evaluating` へ戻す（Node は再実行せず、Budget も使わない）。理由: 1 試行に 1 つの DAG で、結果は確定している。開き直すと、失敗した Node がないので何を再実行するかを決められず、成功済みの Node を全て再実行して Budget を使い、結果が変わり得る。Retry は「DAG の後の層（Evaluator / Review）の失敗のやり直し」になり、仕事そのものをやり直したいときは Restart（新しい試行、新しい DAG）である。同じ Run のまま DAG だけが閉じていた場合（DAG を閉じた後、Task の Command の前に Worker が死んだ）も、DAG の結果のとおりに Task を進める。
- **Task の Command は、その Run に限って発行する。** `begin_evaluation` / `fail` / `wait` / Start は、Task を読み直し、Run が違えば型付きの Stale Run のエラーで何も書かず、同じなら読んだ Version を `expected_version` に付けて 1 つの Transaction で比較と遷移を行う。最後の確認の後に `fail` → Retry → Start された Task に、古い Run の Command が届かない。
- **Run が終わるとき、`running` の Task に仕事が残らない状態を作らない。** 予期しない Error や状態は Task を固定の Reason で `failed` にし、それも書けないときは Queue の Entry を Claim されたまま残す（Lease の失効で次の Worker が引き継ぐ）。

### 7. Sub-Agent の予算

**予算の分け方は「親 Task の予算を 1 つの共有の Pool として使う」**。Node ごとの予算は持たない。Node の消費は全て親 Task の `budget_usages` へ記録する（`NodeBudgetHandle.charge`。Tool 呼び出しは Broker の `BudgetProvider`（`TrackerBudgetProvider`）が `tool_calls` として記録する）。Node の起動ごとに `steps` を 1、再試行ごとに `retries` を 1 消費する。Runtime は Lease を持つ Worker の Timer が 1 つ数える。

- 起動の前に `steps` の予定を確認し、超過なら新しい Node を起動しない（Decision 0007 の規則で `WAIT_FOR_USER` か `FAIL`）。
- **Budget は Loop が起きるたび（Node の終了ごと、Poll ごと）に全て確認する。** `runtime_seconds` は Tracker の中で増えて Node は報告しないため、これがなければ上限を超えて走り続ける Node（終わらない Node を含む）を止められない。上限を超えたら、終わったばかりの Node の結果を先に保存し、走っている Node を Cancel して（試行は `interrupted`、Node は `ready`）、新しい Node を起動せず、Decision 0007 の規則で Task を止める。
- Node が報告した消費で Budget が尽きたら、その Node に `NodeStopped` を投げ、同じ Run の他の Node の Tool 呼び出しと報告も拒否する。実行中の Node が使った分は超過として記録される（`check` が予約でないため、超えるのは実行中の呼び出しの分まで）。

### 8. DAG の永続化と Fencing

- DAG は Task の**試行ごと**（`UNIQUE (task_id, attempt)`）。Table は `agent_dags`、`agent_dag_nodes`、`agent_dag_edges`、`agent_dag_node_attempts`（Migration `0034`。DELETE を誰にも与えない）。
- **`epoch`（Fencing Token）:** DAG を引き継ぐ Worker（Lease を持つ Worker）が `acquire` で 1 増やし、全ての書き込みは自分の `epoch` を示す。書き込みは DAG の行を `FOR NO KEY UPDATE` で Lock してから `epoch` を比べるため、引き継ぎの Commit の前でも Lock 待ちの間でも、古い Worker の書き込みは何も変えずに `StaleDagEpochError` になる（同じ Lock が 1 つの DAG の書き込みを直列にする）。Node の試行も Fencing する（`attempt_count`）。
- Worker が死んだときは、次の Lease 保持者が `acquire` で、走っていた Node を `ready` に戻し、その試行を `interrupted` にする。**中断された試行も、その段の試行数に数える**（Worker を落とす Node が無限に再開されないように）。
- Run は Task の状態を、Node の終了ごとと周期（既定 2 秒）に読む。`cancelled`（Cancel と Stop Now）は走っている Node を即座に止めて DAG を `cancelled` にする（Graceful と Immediate を Node では区別しない。成果物は Worktree に残る）。`failed`（外部の Fail）は Node を `ready` に戻す。`paused` は新しい Node を起動せず、走っている Node は終わらせてから止める。Retry / Restart で Run が替わったら、何も書かずに止まる。
- Pause の後の Resume は、Task を `running` に戻すだけで**Queue へは戻さない**。Resume と Enqueue は呼び出し側（API 層）が続けて行う（Task が `waiting` の後の `unblock` も同じ）。

### 9. `agent.use` / `project.agent.use` は委任不可のままにする

Decision 0004 は、この 2 つを「子 Agent の Grant を親の部分集合として導く仕組みができるまで」委任不可にした。その仕組み（`derive_child_grant`。Capability も Project も広げられず、委任不可の Capability を含めず、親自身の ID を拒否する）はこの Issue で実装した。**それでも 2 つを委任可能にはしない**（推奨）。Sub-Agent を起動するのは Backend の Orchestrator だけで、Agent が自分で別の Agent を起動する経路（Tool）はないため、Agent がこの Capability を行使する場面がない。Agent が起動を要求できる Tool を作るときに、新しい Decision（Decision 0004 を `Supersedes`）で委任可能にし、`authz/capabilities.py` の 2 行と `tests/test_authz_policy.py` の表を変える。

### 10. Project 削除の Sweep の周期

`ProjectTaskStopLoop`（Application の Lifespan が、DB があるとき起動する）。

| 項目 | 値（暫定） |
| --- | --- |
| 周期（`PAW_PROJECT_TASK_STOP_INTERVAL_SECONDS`） | 既定 60 秒。0 で無効、それ以外は 10〜3,600 秒 |
| 1 周期で見る Project | 50 まで（最大 500）。未処理の要求の Project を先に、次に削除待ちの Project を id の順に、前の周期の続きから（Cursor） |
| 1 Project への呼び出し | 1 周期に 5 回まで。終わらなければ次の周期が続ける |
| 未完了があるとき | 次の周期まで 5 秒 |
| 周期全体の失敗 | 10 秒から倍で、周期を上限に待つ。1 Project の失敗は他の Project を止めない |
| 最初の周期 | 起動の 5 秒後（周期が短ければ周期） |

**削除待ちの Project 全てを見る**（未処理の要求だけではない）。要求の処理の後で作られた Task や Queue の Entry は、Project の状態から動く `stop_project_tasks` の再実行でしか止められないため（Decision 0008 の 4）。停止は、`TaskService` に承認の取り消し Listener（`revoke_on_task_end`）を付けて行う。

## 選定理由

- Plan の上限は、桁を合わせるための暫定値で、実測に基づかない（1 Task で 32 Node、8 並列以下は、1 台の GPU Server で個人〜小規模 Team が使う想定）。値は `orchestrator/limits.py` のデータで、DB に書いた形の制約（Key の形、JSON の大きさ）以外は Migration なしで変えられる。
- Role の上限を最小にしたのは、既定拒否の方針（Decision 0004）による。緩める必要（Reviewer が Test を走らせる、など）が実際に出たときに広げる。
- 予算を共有の Pool にしたのは、Node ごとの予算を分けると、並列 Node の消費の見積もりと配分（Preemption、余りの返却）という、要件にない設計が要るため。共有ならば「親を超えない」は構造で保証される。
- 中断された試行を試行数に数えるのは、Worker を落とし続ける Node が、Loop 検知にも Budget にも数えられずに再開され続けるのを防ぐため（Loop 検知は失敗だけを数える）。
- 保存しないのは失敗の文字列で、Model や Tool が出した文字列が Secret を含み得るため（AGENTS.md の Secret の規則。Loop の Signature は Hash）。

## 代替案

- **Node ごとに予算を割り当てる**: 上のとおり、要件にない配分の設計が要る。Node の上限（1 Node が使える `tokens` など）が必要になったら、Plan の Field として足す。
- **Plan を Node が拡張できる**（動的な分解）: Node が無制限に Node を増やせないことを保証するため、上限と承認の設計が要る。最初は Plan を 1 回だけにした。
- **DAG を Task ごと（試行をまたいで 1 つ）にする**: Restart は「最初からやり直す」ので、古い DAG の結果が新しい試行へ混ざる。試行ごとにした。
- **Escalation を Loop でない失敗の上限でも行う**: 実装は上限に達したら Node を失敗にする。上位の Agent がより成功しやすいなら、上限で Escalation するほうがよい（下の「決めてほしいこと」5）。
- **Fencing を Queue の Lease の期限の判定で行う**（書き込みの文で Lease を確認）: Lease は Database の時計で判定され、期限内でも次の Claim との間に隙間があり、`UPDATE` は Lock を待つ前に条件を判定するため、原子的にならない。Lock を先に取り `epoch` を比べる方式にした（Decision 0007 の 6 と同じ考え方）。
- **Task の完了まで Orchestrator が行う**: Evaluator（機械的な検証）と独立 Review は別の層で、Orchestrator が完了にすると自分の成功を自分で判定することになる（AGENTS.md の基本原則 3）。
- **Sweep を Task Service の中で行う**: Delete 開始の Transaction の中で外部の Worker は止められない（Decision 0008 の 8）。

## リスク

- Role の上限が厳しすぎると、Reviewer / Researcher が Test を実行できず、Worker が結果に書いた `test_result` を信じることになる。
- 静的な並列数（既定 4）は、Local Model の同時実行数や VRAM を見ない。PAW-036 ができるまでは、設定で人が決める。
- Cancel を Graceful と Immediate で区別しない（Node は即座に止まる）。途中の成果は Worktree に残るが、Node が閉じる処理は走らない。
- 削除待ちの Project の Sweep は周期ごとに Project の Table を読む（削除待ちは部分 Index を使う）。Project の数が非常に多い環境では周期の設定を見直す。
- Pause / `waiting` の後の再開に、呼び出し側が Queue へ戻すことが要る（8 節）。API 層（PAW-022 以降）が実装するまで、この経路は人手（Test と同じ呼び出し）になる。

## 承認後の扱い

承認されたら、Status を Approved に改め、`Approval` に日付と承認の様子を記録する。数値は暫定値で、`orchestrator/limits.py` のデータと Test の期待値を変えれば変えられる（DB に書いた 3 項目を除く: Key の形、JSON の大きさの予備、Node と Ladder の数の CHECK。これらは新しい Migration と Decision が要る）。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 決めてほしいこと

1. **Role の上限**（2 節）を承認するか。特に Reviewer / Researcher に `project.task.run`（Test の実行）を与えるか。推奨: 与えない（読み取り専用を保証しやすい）。Test の結果は Worker / Evaluator が `test_result` に載せる。
2. **予算は親 Task の共有 Pool とし、Node ごとの予算は持たない**（7 節）でよいか。推奨: はい。
3. **必須の Node が解決できなかったとき、Task を `failed` にする**（6 節。Retry が失敗した Node を開き直す）か、`waiting`（理由 `user`）にするか。推奨: `failed`（既存の Retry / Restart の経路がある）。
4. **DAG が成功したら `evaluating` までで、完了は Evaluator**（6 節）でよいか。推奨: はい。
5. **試行の上限に達した Node は、Escalation せず失敗にする**（3 節）のか、次の段へ Escalation するのか。推奨: 今の実装（Loop のときだけ Escalation）で始める。実運用で上位の Agent の成功率が高ければ変える。
6. **`agent.use` / `project.agent.use` は委任不可のまま**（9 節）でよいか。推奨: はい。
7. **数値の暫定値**（1、3、4、5、10 節: Node 32、依存 8、結果 32 KiB、試行 6、Back-off 1〜60 秒、Node の Timeout 30 分、並列 4、Sweep の周期 60 秒など）を暫定値として承認するか。
8. **Cancel の扱い**（8 節: Graceful と Immediate を Node では区別せず即停止）でよいか。
9. **Pause / `waiting` の後の再開は、呼び出し側（API 層）が Queue へ戻す**（8 節）ことを、PAW-022 以降の受け入れ条件に加えてよいか。
10. **`succeeded` の DAG を Retry したときは、DAG を開き直さず `evaluating` へ戻す**（6 節）でよいか。推奨: はい（仕事をやり直すのは Restart）。代わりに、Retry が成功済みの Node も含めて DAG を開き直す案もあるが、何を再実行するかの規則と Budget の消費が要件にない。
