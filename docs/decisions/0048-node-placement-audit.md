# Node の実行 Placement（local / cloud・Agent / Model）を Orchestrator の記録と Audit に残す方式（Decision 0037 の 14 の実装）

- Status: Approved
- Date: 2026-09-28
- Scope: Issue [#133](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/133)（PAW-036 の後続）。DAG Orchestrator（PAW-034、`paw_backend/orchestrator/`）の Node の試行の記録と、Compute Resource Scheduler（PAW-036、`paw_backend/compute/runtimes.py`）の `HybridRuntime`
- Supersedes: なし。[Decision 0037](0037-gpu-compute-scheduler.md)（Approved）の 14「Cloud へ回した Node の Placement を Orchestrator の記録（Audit）に残す（Orchestrator の変更として別の Issue）。その Issue で Placement が Audit に記録されるまでは `CloudPolicy` を注入しない」を**実装する方式**の提案で、0037 は書き換えない。[Decision 0021](0021-dag-orchestrator-policy.md)（Node の試行の記録）と [Decision 0023](0023-audit-events-details-for-external-send.md)（外部送信の Audit）への追補
- Approval: 2026-09-29、Humanが作業Session内で、判断点ごとの説明（推奨つき）を受けたうえで「推奨どおり」と回答して承認（決めてほしいこと 1〜6 の全点。末尾の「承認時の決定」）

## 背景

Decision 0037 の 8 は、Local の GPU が混んでいて、注入した `CloudPolicy` が許すとき、`HybridRuntime` が Node を Cloud の Agent（Codex / Claude）で走らせることを決めた。ところが Orchestrator の Node の試行の記録（`agent_dag_node_attempts`）は Ladder の段（`agent_index`、つまり Local の Agent の Label）しか持たず、Cloud で走った Node も Local で走ったように見える。これは [AGENTS.md](../../AGENTS.md) 13 の Audit（agent / model）と、Node の内容の外部送信の Audit（Decision 0010 / 0023 の考え方: 送る前に記録し、記録できなければ送らない）に合わない。

Human は 2026-09-28、0037 の 14 を「残す（別の Issue）。それまでは `CloudPolicy` を注入しない」として承認した。Issue #133 の受け入れ条件は次の 4 つである。

- Node の Attempt ごとに Placement と Agent / Model を記録する（Migration が要る）
- Cloud に送ったときは、外部送信の Audit を残す
- この Issue が終わってから `CloudPolicy` を注入できるようにする（Config で明示的に有効にする）
- Test

0037 も Issue も、**どこに・何を・どの粒度で**記録するか、外部送信の Audit の行の形、記録に失敗したときの扱い、Planner の呼び出しの扱い、「明示的に有効にする」の意味は決めていない。実装は動かすために次を選んだ。[AGENTS.md](../../AGENTS.md) の「仕様変更」に従い、その選択を一覧にして承認を求める。

実装は [Backend README](../../apps/backend/README.md) の「DAG Agent Orchestrator」の「Placement」と、「GPU / Compute Resource Scheduler」の「Local / Cloud の振り分け」に書いている。

## 提案

### 1. 記録の置き場所: Node の試行の行（`agent_dag_node_attempts`）に列を足す

- Migration `0133`（`down_revision` は `0085`。鎖の並びは統合時に確認する）が、`agent_dag_node_attempts` に NULL 可の 7 列を足す: `placement`（`local_gpu` / `local_cpu` / `cloud`）、`placement_agent`、`placement_model`、`placed_at`（DB の時計）、Cloud のときだけ `content_fingerprint`、`content_bytes`、`placement_audit_id`。
- 試行は Node の起動ごとの記録（Decision 0021）で、Placement は試行ごとに違いうる（1 回目は Local、Retry は Cloud）。別の Table にしないのは、試行と 1 対 1 で、既存の Fencing（DAG の行の Lock、`epoch`、Task の Run、試行の番号）をそのまま使えるため。
- CHECK 制約: 4 列（placement・agent・model・時刻）は揃って NULL か揃って非 NULL。Agent は `[a-z][a-z0-9._-]{0,63}`（Ladder の Label と同じ）、Model は `[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}` の識別子で、文章は入らない。`cloud` は `placement_audit_id`・`content_fingerprint`（`sha256:` + 64 桁の 16 進）・`content_bytes` を必ず持ち、Local はどれも持たない。
- **1 回だけ**: Application の Role に 7 列の列単位の `UPDATE` を与え（他の列の権限は 0034 のまま）、Trigger `tr_agent_dag_node_attempts_placement_once`（`ENABLE ALWAYS`）が、`placement` が入った後の 7 列の変更を、どの Role が書いても拒否する。Service も 2 回目を `InvalidOrchestratorArgumentError("placement")` で拒否する。
- 既存の行は 7 列とも NULL（記録なし）で、制約はそのまま満たす（`NOT VALID` にしない）。Placement を報告しない Runtime の試行も NULL のまま。

### 2. 何を記録するか: 場所・Agent・Model

- Local（`local_gpu` / `local_cpu`、Scheduler の Lease の Placement）: Agent は Ladder の Label（`assignment.agent`）、Model は `HybridRuntime(local_model=...)`（既定は Deployment の名前。`DeploymentSpec` は Model ID を持たないため）。
- Cloud: Agent は `HybridRuntime(cloud_agent=...)`（`codex` / `claude` など）、Model は `cloud_model=...`。`cloud=` を渡すときはどちらも必須（無ければ構築時に `TypeError`）。

### 3. Cloud の外部送信の Audit: `audit_events` の既存の列だけで 1 行、Placement と同じ Transaction

- `DagStore.record_placement` が、試行の行の更新と同じ Transaction で `audit_events` に 1 行を INSERT する（Shared Memory の完了の行（Decision 0009 の 13）と同じ形。Authorizer の `AuditSink` は別 Transaction なので使わない）。Placement と Audit の行は一緒に Commit されるか、どちらも残らない。
- 行: `action` `orchestrator.cloud_send`、`reason` `cloud_placement`、`decision` `allow`、`resource_kind` `task`、`resource_id` Task、`project_id` Task の Project、`actor_id` Task の委任者（Agent が代わりに動く User）、`actor_role` `system`（Backend が置き場所を決めた）、`agent_id` Node の Agent ID（`agent_id_of`。その Node の Tool 呼び出しと同じ ID）、`id` と `correlation_id` は新しい UUID で、試行の行の `placement_audit_id` に残す。
- **`details` は使わない**（NULL）。送った内容の指紋と大きさは試行の行（`content_fingerprint` / `content_bytes`）に置き、`placement_audit_id` で行き来する。Decision 0023 の 2 の規則（`details` を使う `action` は登録簿と閉じた Schema の CHECK を新しい Migration で足す）には触れない。理由は代替案を参照。
- 内容の指紋: Orchestrator が Node に渡した内容（Key・Role・Title・Goal・Input・上流の結果）を、Key を整列した Compact な JSON（UTF-8）にした SHA-256 と Byte 数。**Runtime ではなく Orchestrator が計算する**（Runtime は偽れない）。内容そのものはどこにも残さない。
- Audit の対象は「Node の内容を Cloud の Agent に渡したこと」だけである。Cloud の Agent が走る間に行う Tool 呼び出しは、Local の Agent と同じく Tool Broker を通り、その Audit に残る。

### 4. 送る前に記録し、記録できなければ送らない（Fail closed）

- Runtime は Node を走らせる**前に** `assignment.placement.record(...)` を 1 回呼ぶ（`NodePlacement` の Protocol）。`HybridRuntime` は、記録が例外で終わったら Cloud に送らず、Node を `ComputeUnavailable`（Retry 可）で終える（Decision 0010 の `audit_failed` と同じ向き）。`NodeStopped`（Task の終了、Run の置き換え、見捨てた試行）はそのまま通す。
- **Local の試行も同じにする**: 記録できなければ Local でも走らせない（`ComputeUnavailable`、Retry 可）。Local は外部送信ではないが、Issue の受け入れ条件（Attempt ごとに記録）を満たさない試行を作らないため。
- 記録は結果と同じく `epoch`・Task の Run・試行で Fencing する（`StaleDagEpochError` / `StaleRunError` / `StaleNodeAttemptError`）。`AttemptFence` が閉じた（Orchestrator が待つのをやめた）試行は何も記録しない。

### 5. Planner の呼び出しは Cloud へ回さない

- Planner の呼び出しには試行の行がない（DAG ができる前）ので、`placement` を渡さない（`None`）。`HybridRuntime` は `placement` の無い Assignment を Cloud へ回さず（`CloudPolicy` も尋ねない）、Local では記録なしで走らせる。
- Planner を Cloud で走らせるには、Planner の呼び出しの記録（別の Table か `audit_events` だけの行）が要る。今回は含めない。

### 6. 「Config で明示的に有効にする」の意味: 注入そのものが明示で、Cloud の身元が必須

- `HybridRuntime` に `cloud` と `cloud_policy` を渡し、`cloud_agent` と `cloud_model` を必ず与えることを「明示的に有効にする」とする。環境変数（`PAW_*`）の Flag は足さない。Orchestrator と `HybridRuntime` を組み立てる本番の Wiring がまだ無く（Application の Lifespan に組み込んでいない。Decision 0037）、読まれない設定値になるため。本番の Wiring を作る Issue で、`Settings` の Flag（既定は無効）として足すことを推奨する。
- **この Decision が承認されたら、Decision 0037 の 14 の条件（Placement が Audit に記録される）は満たされたとみなす。** ただし実際に `CloudPolicy` を注入するには、なお (a) Codex / Claude の Cloud Runtime（`AgentRuntime`）、(b) Task の Permission・外部送信の許可・Quota・依存を判断する `CloudPolicy` の実装、(c) それらと `HybridRuntime` を Orchestrator に渡す本番の Wiring、が要る。どれも別の Issue である。

## 代替案

- **`audit_events.details` に登録する（Decision 0023 の 2 の規則どおり、`orchestrator.cloud_send` の閉じた Schema を足す）**: Audit の 1 行に Agent・Model・指紋が揃う。一方、`audit_events` は Migration `0086`（Decision 0027）で月ごとの Partition になり、`details` の 2 つの CHECK は親・`audit_events_archive`・各 Partition で**同じ名前・同じ定義**でなければ `ATTACH PARTITION`（退避と戻し）が失敗する。登録簿の制約を置き換えるには 2 つの親と既存の Partition の全てで揃えて置き換える Migration と、`AuditRetentionService` が作る Partition の定義の追随が要り、Audit の保存の仕組みを危うくする。Audit の行を ID と Enum だけにし、詳細は Fencing された試行の行（Trigger で 1 回だけ）に置く方が小さい。**採らない**（Human が 1 行に揃えることを望むなら、この点だけ変えて Migration を足す）。
- **別の Table（`agent_dag_node_placements`）に記録する**: 試行と 1 対 1 で、Fencing と権限を複製するだけになる。採らない。
- **Scheduler の Log だけで追う**（0037 の 14 の代替案）: Human が 0037 で採らなかった。
- **記録に失敗しても Local では走らせる**: 可用性は上がるが、記録のない試行ができる。Local は外部送信ではないので、Human が望むならこちらに変えてよい（決めてほしいことの 4）。
- **指紋を Runtime が計算する（Cloud に実際に送った Byte 列の SHA-256）**: 送った内容に厳密に一致するが、Runtime が偽れる。Orchestrator が渡した内容の指紋にした。

## リスク

- 指紋は「Orchestrator が Node に渡した内容」のもので、Cloud Runtime が実際に送る Prompt（System Prompt や整形を含む）の Byte 列とは一致しない。
- Cloud の Runtime が Ladder に直接入っている構成（`HybridRuntime` を通さない Codex / Claude の段）は、その Runtime が `placement.record(CLOUD, ...)` を呼ばない限り記録されない。Runtime の契約として「Cloud で走らせる Runtime は必ず記録する」を課す（`NodePlacement` の説明）が、DB は強制できない。
- 記録の Transaction が失敗すると Node は `ComputeUnavailable` で Retry に回る。DB の一時的な障害が続くと Node が進まない（安全側）。
- `audit_events` の行は `details` を持たないので、Audit だけを読む人は、Agent・Model・指紋を試行の行と突き合わせる必要がある（`placement_audit_id`）。

## 決めてほしいこと（2026-09-29 に推奨どおり承認）

1. **記録を `agent_dag_node_attempts` の列（7 列）にし、Trigger で 1 回だけにする**（1）でよいか。推奨: はい。
2. **Local の Model は `local_model`（既定は Deployment 名）、Cloud は `cloud_agent` / `cloud_model` を必須にする**（2）でよいか。推奨: はい。
3. **Cloud の外部送信の Audit を、`audit_events` の既存の列だけの 1 行（`orchestrator.cloud_send`、`details` は NULL）として Placement と同じ Transaction で書き、指紋と大きさは試行の行に置く**（3）でよいか。推奨: はい。`details` に登録する案（代替案の 1 つ目）もある。
4. **記録できない試行は Local でも走らせない（Fail closed）**（4）でよいか。推奨: はい。Local だけは記録なしで走らせる案もある。
5. **Planner の呼び出しは Cloud へ回さない**（5）でよいか。推奨: はい。
6. **「明示的に有効にする」は注入と Cloud の身元の必須化とし、環境変数の Flag は本番の Wiring の Issue で足す。この Decision の承認で 0037 の 14 の条件は満たされたとみなす**（6）でよいか。推奨: はい。

## 承認後の扱い

承認されたら `Approval` に記録し、Status を Approved に改める。方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する。承認されても、`CloudPolicy` の注入には 6 の (a)〜(c) が要る。

## 承認時の決定（2026-09-29）

Human は、作業 Session で判断が必要な点について推奨つきの説明を受け、「推奨どおり」と回答して承認した（決めてほしいこと 1〜6 の全点）。**すべて推奨どおり**で、個別の変更はない。6 の承認で Decision 0037 の 14 の条件（Cloud の Placement を記録すること）を満たしたとみなす。実際に Cloud へ送るには、Cloud Runtime・`CloudPolicy`・Wiring が別に必要である。
