# Local の使用量・GPU 時間・Escalation の記録と使用状況の集計への反映（記録の単位と場所、トークンと GPU 時間の数え方、Escalation の User への帰属と原因の分け方、集計の合計への Local の加え方）

- Status: Approved
- Approval: 2026-10-08、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで直接回答して承認（すべての判断点を推奨どおり承認。末尾の「承認時の決定」）
- Date: 2026-10-07
- Scope: Issue [#187](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/187) の 5（Local の使用量・GPU 時間・Escalation の記録）。`apps/backend/paw_backend/compute/usage.py`（`LocalUsageSink`・`PostgresLocalUsage`・`LocalUsageRow`）、`compute/runtimes.py`（`HybridRuntime` の `usage`）、`orchestrator/composition.py`（`_with_local_runtimes`）、`orchestrator/store.py`（`fail_node`・`record_incident` の `task_id`）、`orchestrator/models.py`（`AgentIncidentRow.task_id`）、`connections/store.py`・`connections/report.py`（集計）、`api/v1/usage.py`（`GET /api/v1/usage`）、Migration `0189`
- Supersedes: [Decision 0069](0069-usage-quota-http-api.md)（Approved）の 1 のうち「Local は記録なし（`tokens.local`・`gpu_seconds`・`escalations` は `null`、日別の `local` は 0）」と 8（Item 5 は後続）を、下の 5 で置き換える。[Decision 0071](0071-agent-oom-and-escalation-records.md)（Approved）の 3 のうち「Task は持たない」を、下の 4 で置き換える（種類・時刻・保存期間・権限・System Health の数え方は変えない）。それ以外の 0069・0071 と [Decision 0016](0016-shared-connection-adapter-policy.md) の Quota の規則はそのまま

## 背景

Decision 0069（Approved）は使用状況の HTTP API を Codex / Claude の呼び出し（`connection_usage`）だけから作り、Local の Agent の使用量・GPU 時間・Escalation を「記録がないので `null`（UI は「—」「記録なし」）」とした。Issue #187 の 5 はその記録を求める。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の「Admin Usage Analytics」（FIXED）は、管理画面のグラフに「Local / Codex / Claude の利用比率」「GPU time / Agent runtime」「Task 成功/失敗/エスカレーション件数」を求めている。

今の実装では次のとおりで、どれも期間・User ごとに数えられない。

- Local の Model での実行は `HybridRuntime`（`compute/runtimes.py`、PAW-036）を通る。Lease を持った壁時計の時間は Task の Budget の `GPU_SECONDS` に加算され、Local の Runtime が報告するトークンは `TOKENS` に加算されるが、`budget_usages` は Task ごとの累計で、時刻も Local / Cloud の区別もない。
- Escalation は `agent_incidents`（Decision 0071）に種類と時刻だけで残り、どの Task（どの User）のものか分からない。

**次のことは要件も既存の Decision も決めていない。** Local の使用量をどの単位でどこに残すか。トークン・Request・GPU 時間を何で数えるか。Escalation をどう User に帰属させ、UI の「失敗 / ループ検知」をどう分けるか。集計の合計・日別・Agent 別・用途別・User 別に Local をどう加えるか。
[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、実装はこれらを下の推奨で置き、この Decision で承認を求める。

## 提案

### 1. 記録の単位と場所（新しい Table `local_usage`、Migration `0189`）

- **推奨:** Local の Model での 1 回の実行（`HybridRuntime` が Local の Lease を取って Local の Runtime を呼んだ 1 回。Node の試行と Planner の呼び出し）ごとに 1 行を、実行が終わった時に `local_usage` に書く。
  - 列: `id`、`user_id`（Task の作成者。書く時に `tasks.created_by` から取る）、`task_id`（`tasks.id` への外部 Key）、`placement`（`local_gpu` / `local_cpu`、Scheduler が置いた場所）、`calls`（1。下の「遅れた時間」の行は 0）、`tokens`（0 以上）、`seconds`（Lease を持った秒数、0 以上）、`started_at`（Database の `now()` から `seconds` を引いた時刻）。
  - Model 名・Agent 名・Prompt・応答・失敗の文は持たない（Decision 0016 の 6 と同じ）。
  - Node が取り消されても止まらなかった Local の呼び出し（Lease を終わるまで持ち続ける、`HybridRuntime` の「遅れた GPU 時間」）は、終わった時にその分を `calls = 0` の行で足す（Budget の遅れた計上と同じ時間）。
  - 書けなかったら Log に例外の Class 名だけを残し、Node の結果は変えない（使用量の記録の失敗で Agent を止めない）。
  - Application の Role には SELECT / INSERT だけを与える（更新・削除はしない）。保存期間は `connection_usage` と同じく決めない（消さない）。
  - 記録するのは `HybridRuntime` で包んだ Local の Runtime（`LocalRuntime`、Decision 0058）だけ。`runtimes` に直接渡した Runtime は Local か Cloud か分からないので記録しない。
- 代替: (a) `connection_usage` に `kind = 'local'` を足す。あの表は共有 Connection の Admission・Quota・Reaper の対象で、Local の実行はそのどれも通らないので推奨しない。(b) `budget_usages` から読む。Task ごとの累計で時刻も Local / Cloud の区別もないので、期間・日別に数えられない。(c) Local の Runtime の Adapter に書かせる。Adapter ごとに同じ計上を実装することになり、遅れた時間も数えられないので推奨しない。

### 2. トークンと Request の数え方

- **推奨:** トークンは、その実行の間に Local の Runtime が `NodeBudget.charge(TOKENS, n)` で Task の Budget に報告した量の合計（`HybridRuntime` が Runtime に渡す Budget を包んで数える。報告しない Runtime は 0。`connection_usage` の「不明は 0」と同じ）。Budget が受け付けなかった報告（不正な値）は数えない。Budget の超過で Node が止まる報告は、使ったものなので数える。
- Request は Local の実行の回数（`calls` の合計）とする。Model Server への HTTP の Request の数ではない（Backend からは見えない）。今の UI と API には Local の Request の欄がないので、記録だけして出さない。
- 代替: Runtime の戻り値（`NodeOutcome`）にトークンの欄を足す。Runtime の Protocol（Decision 0021）が変わり、失敗・取り消しの時に数えられないので推奨しない。

### 3. GPU 時間

- **推奨:** GPU 時間は、`placement = local_gpu` の行の `seconds` の合計（Lease を持った壁時計の時間を秒に切り上げたもの。Budget の `GPU_SECONDS` に加算する値と同じ。遅れた時間も含む）。`local_cpu`（CPU への Fallback）の行は GPU 時間に数えない。
- Budget の `GPU_SECONDS` は今までどおり CPU の実行も数える（Budget の挙動は変えない）。そのため Task の Budget の消費と使用状況の GPU 時間は、CPU の実行がある時だけ一致しない。
- 代替: CPU の実行も GPU 時間に数える（Budget と一致するが、GPU を使っていない時間が GPU 時間として表示される）。

### 4. Escalation の記録（`agent_incidents` に `task_id` を足す）

- **推奨:** 新しい表は作らず、`agent_incidents`（Decision 0071）に `task_id`（`tasks.id` への外部 Key、Null 可）を足す。`fail_node` は DAG の Task を、Planner の OOM の `record_incident` は呼んだ Run の Task を書く。Migration より前の行は `NULL` のまま（Workspace の集計には数え、User の集計には数えない）。User は `tasks.created_by` から辿る（Task は削除されない）。
- 種類・時刻・保存期間（`PAW_HEALTH_RETENTION_DAYS`）・権限・System Health の数え方（Decision 0071 の 3・4）は変えない。
- 代替: `user_id` を足す（Task が分からず、UI の「エスカレーションしたタスク数」を数えられない）。Escalation の別の表を作る（同じ事実を 2 か所に書くので推奨しない）。

### 5. 使用状況の集計（`GET /api/v1/usage`）

- **推奨:**
  - `tasks`（合計）と `previous_tasks`: 期間内に Codex / Claude の呼び出し **または** Local の実行が 1 回以上ある Task の数（両方ある Task は 1）。User 別の `tasks` も同じ。
  - `tokens.local`: 期間内に始まった Local の実行のトークンの合計（`null` ではなく数）。`tokens.external` は今までどおり。User 別の `tokens` は両方の合計。
  - `daily[].local`: その日に Local の実行がある Task の数（今まで 0 固定）。
  - `agents`: Local の実行がある時は `local` の行（Task 数・トークン）を Codex / Claude の前に置く。
  - `purposes`: 今までどおり Codex / Claude の呼び出しだけ（Local の実行には Decision 0016 の 6 の用途の Category がない）。
  - `gpu_seconds`: 期間内に始まった `local_gpu` の行の `seconds` の合計（Self は本人の Task、Workspace は全体）。
  - `escalations`: 期間内に Escalation の記録がある Task の数。`loop_detected` は今の Orchestrator の Escalation のすべて（Decision 0007: Escalation は Loop 検知の `ESCALATE` からだけ起きる）、`failed` は 0（Loop 検知を経ない失敗からの Escalation は今の Orchestrator にない。できたら原因を記録に足す）。
  - 記録がない期間も `null` ではなく 0 を返す（記録の仕組みはあるので「0 件」）。
  - 1 回の集計は今までどおり 1 つの Snapshot（Decision 0069 の 3）。
- 代替: (a) `tasks` を Codex / Claude だけのままにして Local を別に出す（Agent 別の内訳と合計が合わない）。(b) Escalation を Task ではなく件数で数える（同じ Task の 2 回の Escalation は 2。UI は「タスク数」として出している）。(c) UI の「失敗 / ループ検知」の分け方を 1 つの数にする（UI の変更が要る）。

### 6. Migration

- **推奨:** Revision `0189`（`down_revision = 0188`）で `local_usage` を作り、`agent_incidents` に `task_id` と Index を足す。Merge の時に振り直してよい。

## 決めてほしいこと

1. **Local の Model での 1 回の実行ごとに 1 行を新しい表 `local_usage`（User・Task・置き場所・回数・トークン・秒数・開始の時刻だけ）に、`HybridRuntime` が実行の後で書く。止まらなかった呼び出しの遅れた時間は回数 0 の行で足す。書けなくても Node は止めない。保存期間は決めない**（1）でよいか。推奨: はい。
2. **トークンは Local の Runtime が実行中に Budget へ報告した `TOKENS` の合計（報告しなければ 0）、Request は Local の実行の回数とし、Request は今は API に出さない**（2）でよいか。推奨: はい。
3. **GPU 時間は `local_gpu` に置かれた実行の Lease の秒数（Budget の `GPU_SECONDS` と同じ切り上げ、遅れた時間を含む）の合計とし、CPU への Fallback は数えない（Budget の数え方は変えない）**（3）でよいか。推奨: はい。
4. **Escalation は `agent_incidents` に `task_id` を足して Task（と作成者）に結び付け、新しい表は作らない。Decision 0071 の「Task を持たない」をこれで置き換える**（4）でよいか。推奨: はい。
5. **集計の `tasks`・`previous_tasks`・User 別の数に Local の実行の Task を加え、`tokens.local`・`daily[].local`・`agents` の `local`・`gpu_seconds`・`escalations` に値を入れる（用途別は Codex / Claude だけのまま）。`escalations` は Escalation した Task の数で、今はすべて `loop_detected`（`failed` は 0）**（5）でよいか。推奨: はい。
6. **Migration の Revision を `0189`（`down_revision = 0188`）とする**（6）でよいか。推奨: はい（Merge の時に振り直してよい）。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。
方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。

## 承認時の決定（2026-10-08）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、直接回答して承認した（すべての判断点を推奨どおり承認）。
