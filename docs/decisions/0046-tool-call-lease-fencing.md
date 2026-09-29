# Tool 呼び出しを Queue の Lease で Fencing し、Broker の `tool_calls` を Run に計上する（Decision 0021 への追補）

- Status: Proposed（決定 1 は Human が 2026-09-28 に直接決定した Approved の方針。実装で選んだ「判断が必要な点」1〜7 は承認待ち）
- Date: 2026-09-28
- Scope: Issue [#126](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/126)（PAW-034 の残リスク B10 / B11）。Tool Broker（PAW-031、`paw_backend/tools/`）、Task Queue（PAW-033、`TaskQueue`）、DAG Orchestrator（PAW-034、`paw_backend/orchestrator/`）
- Supersedes: なし。[Decision 0021](0021-dag-orchestrator-policy.md)（Approved）の 7 節（Sub-Agent の予算）と 8 節（DAG の永続化と Fencing）への**追補**（Amends）で、0021 は書き換えない。[Decision 0006](0006-tool-broker-policy.md) の Broker の Interface（`TaskContext`、`BudgetProvider`）を拡張する
- Approval: 決定 1 は 2026-09-28、Human が作業 Session の中で直接決定（「Gap を閉じる」）。判断が必要な点 1〜7 は未承認

## 背景

PAW-034（PR #106）の並行性の監査は、次の 2 点を「許容し、後続に回す」とした（監査の表は PR #106 の第 9〜11 回の対応のコメント）。

- **B10:** Lease を失った古い Worker の Node が、次の Heartbeat（間隔以内）まで Tool を呼べる。`NodeToolGateway` の Guard は Task の Run を確かめるが、Lease は確かめない。引き継ぎ（別の Worker の Claim、同じ Worker ID の再 Claim）では Run が同じままなので、Run の確認では古い Worker と新しい Worker を区別できない。
- **B11:** 最後の Heartbeat から `start_runtime` までの間に隙間がある。
- あわせて、Broker が記録する `tool_calls` の Budget の計上は Run に結び付いていなかった（PAW-031 の `BudgetProvider.charge(task_id, tool)` が Run を渡さないため。#106 第 8 回で ACCEPTED）。

Decision 0021 の 8 節は DAG の書き込みを `epoch` で、Decision 0007 の 7 節は Queue の操作を `claim_count` で Fencing するが、Tool の呼び出し（DB への書き込みではない）はどちらの対象でもなかった。

## 決定

### 1. Gap を閉じる（Approved: Human が 2026-09-28 に直接決定）

- **Queue の Lease の Fencing Token（`claim_count`）を Tool Broker まで渡し、全ての Tool 呼び出しで確かめる。** Lease を失った Worker の呼び出しは拒否する（Fail closed）。
- **Broker の `tool_calls` の Budget の計上を Run に結び付ける。**
- Decision 0021 を書き換えず、この Decision を追補とする。

以下は、この決定を実装するときに選んだ点である。選択は実装済みで、承認されなければ実装を変える。

### 2. 実装の形（下の「判断が必要な点」の対象）

- **Fencing Token の形。** `QueueLease(entry_id, worker_id, claim_count)`（`paw_backend.tasks.queueing`。値は Queue と同じ規則で検証する）。`claim_count` だけでは Entry が分からず、Worker ID だけでは同じ ID の再 Claim を区別できないため、3 つで 1 つの Claim を指す。Orchestrator は `QueueLease.of(entry, worker_id)` を作る。
- **`TaskContext.lease`（必須）。** `TaskContext` に `lease: QueueLease` を足した。`run` と同じく必須で、省略も別の型も `TypeError`。Orchestrator の `_context` が、その Worker が持つ Claim を入れる。
- **Broker の検査（5a）。** Broker に `LeaseVerifier` の差し込み口を足した（`check(task_id, lease) -> LeaseStatus`。`HELD` / `LOST` / `UNKNOWN`）。既定の `FailClosedLeaseVerifier` は `UNKNOWN` を返し、全ての呼び出しを拒否する（`BudgetProvider` / `TaskActivityProvider` と同じ Fail closed）。本番は `QueueLeaseVerifier(queue)` で、`TaskQueue.holds_lease(task_id, lease)` を呼ぶ。
  - 判定の順序では、Budget（5）の後、Working Set の Repository の利用の Admission（#85 の 6。予約と変更の記録）、`AUTO` / `SCOPED_AUTO` の許可、Approval を開く・使う処理（7）の前に置く。Lease を失った Worker の呼び出しは、Repository の予約も変更の記録も残さない。Budget が不要な Tool の呼び出しも、承認を要する呼び出しも、全て確かめる。
  - `LOST` は `lease_lost`。`UNKNOWN`、例外、Timeout（Broker の `timeout_seconds`）、`LeaseStatus` でない答えは `lease_unavailable`。どちらも Audit に固定の理由として残る（例外の文言は Log に出さない）。
- **`TaskQueue.holds_lease`。** 1 つの `SELECT` で、Entry が `task_id` のもの、`claimed`、同じ Worker、同じ `claim_count`、`lease_expires_at > clock_timestamp()` かを返す。`heartbeat` と同じ規則を Database の時計で判定する**読み取り**で、行を Lock せず、Lease を延長しない。Application の Role は `queue_entries` の `SELECT` を既に持つ（Migration も権限の追加もない）。
- **Gateway の反応。** `NodeToolGateway` は `lease_lost` の拒否を受けたら、Run の Guard を `StopReason.LEASE_LOST` で止めて `NodeStopped` を投げる。同じ Run の他の Node の呼び出しも渡さず、Orchestrator は Heartbeat が Lease を失ったときと同じく `LEASE_LOST` で Run を終える（Entry は完了も返却もしない）。`lease_unavailable` は、その呼び出しだけを拒否する。
  - Runtime が Tool の呼び出しの `NodeStopped` を捕まえて結果（`NodeOutcome`）を返しても、Orchestrator は Run の Guard が止まっていれば（`LEASE_LOST`、`SUPERSEDED`、`TASK_ENDED`）その結果を DAG に書かない（`_settle`）。Lease が期限切れになっただけで誰も引き継いでいないときは DAG の `epoch` がまだこの Worker のもので、Store の Fencing では拒否されないため。
- **`tool_calls` の計上。** `BudgetProvider.charge(task_id, run, tool)` とし、Broker は `TaskContext.run` を渡す。`TrackerBudgetProvider` は `BudgetTracker.record(..., run=run)` で記録する（`NodeBudgetHandle.charge` と同じ Fencing: Task の行を `FOR SHARE` で読み、同じ Transaction で現在の Run と終わっていないことを確かめる）。実行中に Run が置き換えられた、または Task が終わった呼び出しは記録せず（`StaleRunError` を Log に 1 行）、Audit には実行として残る。`check(task_id, tool)` は変えない（読み取りで、Run と Lease は別の検査が確かめる）。

### 3. B11 の再確認

#106 の 11 回目の対応の後の Code を読み直した結果、**B11 は残っていない**。

| `start_runtime` を呼ぶ経路 | Lease の証明 |
| --- | --- |
| `_run_entry`（Run の Timer） | `_start_runtime_with_lease`: `start_runtime_in(run=)` と `heartbeat_in` が 1 つの Transaction。Entry の行は Commit まで `FOR UPDATE` で Lock され、他の Worker の Claim（`SKIP LOCKED`）は判定と Commit の間に入れない。Commit の後、Lease は丸ごと `lease_seconds` 残り、Heartbeat は証明の開始（`proved_at`）から数える |
| `_settle_runtime`（止まった Task の Timer の精算） | 同じ `_start_runtime_with_lease`（Run なし） |

`start_runtime` を Lease の証明の外で呼ぶ経路はない（`BudgetTracker.start_runtime` 単独の呼び出しは Orchestrator にない）。既存の `test_orchestrator_lease` の `TimerStartTest`（`test_the_timer_is_started_only_together_with_a_valid_lease` など）がこれを確かめている。`_begin_task` の Start は Lease の Transaction の外だが、`expected_version` で Fencing され、Lease を失った Worker の Start は `start_runtime_with_lease` の `LeaseLostError` で止まる（仕事は始まらない）。そのため、B11 のための追加の変更はしない。

## 選定理由

- **Broker で確かめる**のは Human の決定（1）による。Broker は全ての Tool 呼び出しが必ず通る唯一の場所で、Orchestrator 以外の呼び出し元が将来できても同じ規則が効く。
- **`TaskContext` の必須の Field にした**のは、`run` と同じく、渡し忘れた呼び出しが保護されない形を作らないため（Decision 0007 の 7 で `claim_count` を必須の引数にしたのと同じ考え方）。
- **読み取りにした**のは、Tool の実行は DB の書き込みではなく、Executor の副作用（File、Network）は Lock でも Fencing できないため。Lock を実行の間持つと、Heartbeat（`FOR UPDATE`）が待たされて Lease を延長できなくなる。
- **`lease_unavailable` で Run を止めない**のは、一時的な DB の失敗で Run を止めると、Heartbeat の失敗 3 回の規則（Decision 0021 の 8 節の運用）より厳しくなるため。呼び出しは拒否するので Fail closed は保たれる。
- **`tool_calls` を Run で Fencing した**のは、`NodeBudgetHandle.charge`・`steps`・`retries` と同じ規則にそろえ、置き換えられた Run の呼び出しが引き継いだ Run の予算を使わないため。記録されないのは、Run が置き換えられたときに実行中だった呼び出しだけ（新しい呼び出しは Gateway と Broker が拒否する）。

## 代替案

- **Gateway（Orchestrator）だけで確かめる**: Broker の Interface を変えずに済むが、Human の決定（1）に反する。Orchestrator を通らない呼び出し元に効かない。
- **`TaskContext.lease` を任意（既定 `None`）にし、`None` を Broker が拒否する**: 既存の `TaskContext` の作成箇所を変えずに済むが、「省略できる Fencing」になる。Test の Fixture を 1 か所変えるだけで済むため、必須にした。
- **Approval の消費の Transaction の中で Entry を Lock して確かめる**: 承認を要する呼び出しだけは確認と消費を原子的にできるが、確認の後の実行は同じく Fencing できず、Heartbeat と Lock を競う。
- **確認で Lease を延長する（`heartbeat` を使う）**: Tool 呼び出しが Lease を延ばすと、Heartbeat の失敗で Run を止める規則が働かなくなる。延長は Heartbeat だけにした。
- **`tool_calls` を Fencing しない**（#106 第 8 回の扱い）: 実行した呼び出しは全て数えるが、Retry / Restart の後の Run の予算を、置き換えられた Run の呼び出しが使う。

## リスク

- **確認の後の実行は Fencing されない。** Broker の確認は判定の時点の読み取りで、確認から Executor の開始まで（Audit の書き込みなど）と、実行の間に Lease が切れても、その呼び出しは止まらない。Heartbeat は Lease の期限の前に Run を失ったとして Node を Cancel するが、Executor の副作用は At-least-once のまま（Decision 0021 のリスクと同じ）。
- 呼び出しごとに Queue の 1 行の読み取りが増える（主キーまたは Task の active な Entry の一意 Index。`IndexPlanTest` で確かめた）。
- `tool_calls` の記録が Run で拒否された呼び出しは Budget に数えられない（Audit には残る）。数え落としは、置き換えの時点で実行中だった呼び出しの数まで。

## 判断が必要な点（未承認。推奨つき）

1. **Broker の Interface の変更（PAW-031）**: `TaskContext` に必須の `lease: QueueLease` を足し、`ToolBroker(lease=LeaseVerifier)` を足し、`BudgetProvider.charge` を `(task_id, run, tool)` に変えた。既存の Adapter（`charge(task_id, tool)`）は構築時に `TypeError` になる。推奨: この形で承認する。
2. **検査の場所と順序**: Broker の 5a（Budget の後、Working Set の Admission・`ALLOW`・Approval の前）。単独で拒否できる検査（形、Scope、認可、Budget）は Lease を尋ねずに拒否する。推奨: この位置（受け渡しに最も近い）。代わりに最初に置けば、Lease を失った Worker の呼び出しは早く拒否されるが、確認から実行までの隙間が広がる。
3. **Transaction の境界**: 確認は Lock しない 1 つの読み取りで、Approval の消費の Transaction には入れず、Lease を延長しない。推奨: この形。確認の後の実行は Fencing できないことを残リスクとして受け入れる。
4. **失敗の扱い**: `lease_lost` は Run 全体を止める（`LEASE_LOST`）。`lease_unavailable`（読めない、Timeout）はその呼び出しだけを拒否し、Run の継続は Heartbeat が決める。推奨: この形。代わりに `lease_unavailable` でも Run を止める案は、一時的な DB の失敗に厳しすぎる。
5. **`tool_calls` の Fencing**: 実行中に Run が置き換えられた・Task が終わった呼び出しは記録しない（Log と Audit だけ）。推奨: 記録しない（`NodeBudgetHandle.charge` と同じ規則）。#106 第 8 回の「Fencing しない」を、この Decision で改める。
6. **範囲外にしたもの**: `ConnectionService.execute`（Shared Connection の呼び出し。PAW-022 系）も `TaskContext` を受け取るが、Tool Broker を通らないため Lease を確かめない。`NodeBudgetHandle.charge`（Node が報告する `tokens` / `gpu_seconds`）も Run でだけ Fencing し、Lease は確かめない（行われた仕事の記録のため）。推奨: 両方ともこの Issue では変えず、Shared Connection の Lease の確認が要るかは別の Issue で決める。別の Issue の候補として [#153](https://github.com/TomokiAkiyama06/personal-ai-workspace/issues/153) を作成した。
7. **B11 を閉じる**: 3 節のとおり、`start_runtime` は全ての経路で Lease の証明と 1 つの Transaction にあり、B11 に残りはない。推奨: Issue #126 の B11 の項目を「残りなし」として閉じる（追加の変更なし）。

## 承認後の扱い

判断が必要な点が承認されたら、`Approval` に記録し、Status を Approved に改める。承認されない点は実装を変え、新しい Decision を作らずにこの Decision を承認前に改める（承認後に方針を変えるときは、この Decision を書き換えず、新しい Decision から `Supersedes` する）。
[REQUIREMENTS.md](../../REQUIREMENTS.md) の原文は書き換えない。
